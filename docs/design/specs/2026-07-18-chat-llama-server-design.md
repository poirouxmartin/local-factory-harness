# Chat/studio -> llama-server — design

Date: 2026-07-18 · Statut: validé par Martin (approche A, périmètre full)
Registre: docs/backlog-optimisations.md (migration APPROUVÉE 18/07, byte-stable groupé)

## Objectif

Faire tourner le chat et le mode agent du studio sur llama-server (comme les
jobs depuis `f6cbe6f`), supprimer Ollama du code, et récupérer les gains
mesurés (+25-40 % gen, MTP) plus le TTFT via un prompt byte-stable.

## Décisions de cadrage (validées)

1. **Périmètre full, zéro Ollama** : inférence (chat, agent, compaction,
   draft_job) ET panneau modèles de l'UI. Ollama disparaît du code du studio.
2. **Lock = VRAM** : le chat garde le GPU lock tant que son serveur est
   chargé ; idle-timeout -> shutdown + release. Les jobs font la queue sur le
   lock (mécanique existante). Conséquence : port 8091 unique, jamais deux
   serveurs vivants.
3. **Pull supprimé** : téléchargement GGUF manuel (HF) + entrée factory.toml,
   comme le workflow réel actuel. L'UI liste/charge/décharge seulement.
4. **Style de migration : swap direct** (pas de flag double-runtime).
   Rollback = git revert ; filet = tests unitaires + suite de régression.

## 1. Client streaming — `llama_client.chat_stream`

Générateur SSE sur `POST /v1/chat/completions` (`stream: true`) qui reproduit
exactement le contrat de `ollama_client.chat_stream` :

- Yields `("thinking", text)` (depuis `delta.reasoning_content`),
  `("content", text)` (`delta.content`), `("tool_call", {name, arguments})`
  (`delta.tool_calls`, arguments JSON-décodés).
- Retour via StopIteration.value : même dict métriques qu'aujourd'hui —
  `content`, `done_reason`, `eval_count`, `tokens_per_s`, `prefill_count`,
  `prefill_tokens_per_s`, `ttft_s`, `thinking_chars`/`content_chars`,
  `thinking_s`/`writing_s`, `stopped` ("repetition" | "length"), mappé depuis
  l'objet `timings` du dernier chunk SSE.
- `RepetitionGuard` déménage dans un module neutre (`repetition_guard.py`) :
  il doit survivre à la suppression d'`ollama_client`. Même seuils.
- Multi-messages + `tools` (format OpenAI) ; sampling forwardé comme dans le
  `chat()` non-stream existant (top_p, top_k, min_p, presence/repeat penalty,
  `num_predict` -> `max_tokens`).
- Coupure connexion = cancel de la génération côté serveur (même convention
  que le drop Ollama).
- `_compact` utilise le `llama_client.chat` non-stream existant, même serveur.

Argv serveur (LlamaServerManager) gagne : `--jinja`, `--reasoning-format`
(thinking Qwen3 dans `reasoning_content`), `--cache-reuse 256` (voir §4).

## 2. Cycle de vie serveur (factory_mcp)

- `factory_mcp` possède un `LlamaServerManager` (config `[llama_server]`
  existante, port 8091).
- Nouveau flux par message chat/agent : si serveur pas chargé -> acquérir le
  GPU lock (timeout 0, erreur lisible si un job le tient) puis `ensure(model)`
  ; le lock **reste détenu tant que le serveur est chargé**.
- Idle-timeout (défaut 600 s, clé `[llama_server] chat_idle_s`) : timer armé
  à la fin de chaque génération ; à expiration -> `shutdown()` + release du
  lock. Toute génération/approbation en cours désarme le timer.
- Décharger manuellement (bouton UI) = shutdown + release immédiats.
- Changement de modèle en session = `ensure(new)` (swap géré par le manager).
- Boot web : plus de `ollama_start` ; démarrage paresseux au premier message.
  Health timeout long (cold load minutes) -> event "loading" côté UI pour ne
  pas laisser un spinner muet.
- Arrêt du serveur web -> shutdown du manager (finally), zéro leak (invariant
  déjà validé côté jobs).

## 3. UI modèles + runners

- Endpoints `/api/ollama/*` remplacés par `/api/runtime` :
  - `GET /api/runtime` : serveur up/down, modèle chargé, état du lock (le
    lock fichier est anonyme : "tenu par le chat" se déduit du manager local,
    sinon "tenu ailleurs" = job).
  - `GET /api/runtime/models` : entrées `[llama_server.models]` — nom, chemin
    GGUF, taille sur disque, args ; marquage "chargé".
  - `POST /api/runtime/load` / `unload` : ensure/shutdown via le manager
    (mêmes gardes lock que le chat).
- Supprimés : `ollama_pull.py`, runner `pull` dans RUNNERS, UI pull,
  `ollama_client.py` (en fin de chantier, une fois plus aucun import).
- `draft_job.py` : même switch runtime que `loop_job.py:496-505` (manager si
  `[llama_server]` présent, chat_fn injectable pour les tests).
- Sélecteur de modèle des sessions chat : alimenté par le catalogue
  factory.toml (plus de `/api/show` Ollama ; family/quant/ctx viennent de la
  config ou du nom de fichier).

## 4. Byte-stable / TTFT (groupé)

- System prompt agent stable au byte près entre les tours d'une session :
  vérifier que `_agent_system(workspace, tool_names)` est déterministe (ordre
  des tools trié) et qu'aucun contenu horodaté n'entre dans le wire.
- `--cache-reuse 256` sur l'argv serveur : réutilisation partielle du KV
  après retouche de préfixe (compaction, notices système).
- Mesure (règle registre, une variable à la fois) : TTFT au tour N>1 d'une
  session longue, avant/après migration puis avec/sans cache-reuse. Résultats
  au registre.

## 5. Erreurs

- Serveur mort mi-stream -> event `error` + sauvegarde du partiel (convention
  `_partial` actuelle, inchangée).
- Health timeout au premier message -> erreur lisible ("le serveur n'est pas
  devenu healthy, voir le log").
- Modèle sans entrée `[llama_server.models]` -> message qui dit quoi ajouter
  dans factory.toml (contrat UnknownServerModel existant).
- GPU tenu par un job -> même erreur qu'aujourd'hui ("GPU is busy").

## 6. Tests

- Unitaires : `chat_stream` contre un faux serveur SSE (deltas thinking/
  content/tool_calls, métriques, RepetitionGuard, stopped length/repetition) ;
  idle-timeout avec horloge/timer injectés ; endpoints `/api/runtime` ;
  draft_job switch runtime.
- Suite de régression jobs : doit rester verte (manager/client partagés).
- Smoke réel : session chat + session agent avec tools sur le 30b ; bench
  tok/s visible + TTFT avant/après (registre).

## 7. À vérifier au banc (falsifiables, pas des acquis)

Sur notre build llama.cpp : streaming des `tool_calls` en SSE OpenAI,
`reasoning_content` en delta, présence des `timings` dans le dernier chunk en
mode stream, effet réel de `--cache-reuse` sur le TTFT. Tout échec -> autopsie
avant conclusion (règle projet).

## Hors périmètre

- Refactor jobs vers un serveur partagé (écarté en cadrage).
- Téléchargement HF dans l'UI.
- Lot UX/bugs studio (ROADMAP chat lane, chantier séparé).
