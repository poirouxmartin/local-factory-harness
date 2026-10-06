# Spec — Prévention des boucles, client MCP, pilote blitzvolley

Date : 2026-07-15. Validé en discussion (chantiers A → B → C).

## Contexte

Post-mortem 14/07 (`audits/20260714-agent-chat-postmortem.md`) : la chaîne
« contexte saturé → sampling quasi-greedy → n-gramme répété » a coûté des
minutes de GPU par occurrence. Le `RepetitionGuard` **détecte** ; ce chantier
**prévient**. En parallèle, le chat agent n'a que 6 outils natifs : on ajoute
un client MCP avec des toolsets curés par persona, puis on valide la
délégation sur un vrai projet (blitzvolley).

Principe directeur : Claude Code ne boucle pas d'abord parce que le RL
d'entraînement a éliminé ce mode de défaillance — aucun harness ne remplace ça
à 100 % sur un modèle local. Mais chaque occurrence observée venait de la
saturation de contexte et du sampling : les deux sont corrigeables.

## Chantier A — Prévention des boucles

### A1. `num_predict` : plafond de génération par tour

Équivalent du `max_tokens` de l'API Claude — la raison mécanique pour laquelle
un tour Claude Code ne peut pas produire 134 k chars de thinking.

- `ollama_client.chat()` / `chat_stream()` : paramètre `num_predict`
  (None = illimité, comportement actuel), passé dans `options`.
- Chat/agent Studio : `num_predict = chat_context.OUTPUT_RESERVE` (8192) —
  source unique de vérité ; la réserve prompt devient une garantie, plus une
  espérance.
- Ladder (`loop.py` / `loop_job.py`) : `num_predict = 8192` aussi (un patch
  de plus de ~24 k chars est hors périmètre d'un job délégué).
- Fin par limite : `done_reason == "length"` → `metrics.stopped = "length"`.
  Même traitement que `"repetition"` : le partiel est sauvegardé, le tour
  agent s'arrête proprement avec la raison affichée, l'opérateur relance.
  Pas de relance automatique en v1 (risque de boucle de tours plafonnés).

### A2. Profils de sampling par modèle

Le quasi-greedy (temp 0.1) est documenté par Qwen comme cause de répétition
infinie sur les modèles thinking ; les cartes modèle donnent les valeurs.

- `factory.toml`, table `[models."<name>"]` : `temperature`, `top_p`,
  `top_k`, `min_p`, `repeat_penalty`, `presence_penalty` (tous optionnels).
- Résolution : profil du modèle ← override explicite (stage ladder, UI chat).
  Sans profil, comportement actuel inchangé.
- Valeurs initiales (sources : model cards Qwen, à recopier dans
  `docs/model-configs.md`) :
  - `qwen3.6:35b` (thinking) : temp 0.6, top_p 0.95, top_k 20,
    presence_penalty 1.0
  - `qwen3-coder:30b` (instruct) : temp 0.7, top_p 0.8, top_k 20,
    repeat_penalty 1.05
  - `qwen2.5-coder:7b/14b` : inchangés (temps de stage existants)
- Ladder : les stages 30b/35b adoptent le profil (le 0.1 du 30b disparaît) ;
  l'override de température par stage reste possible si le prochain run de
  délégation régresse.

### A3. Marge de compaction exacte

Avec A1, un tour tient toujours dans `OUTPUT_RESERVE` : le débordement
mid-turn (éviction silencieuse du system prompt par Ollama — le déclencheur
racine du 14/07) devient impossible tant que l'estimation prompt est bonne.
La compaction forcée sur compteurs réels (`_overflowed`) reste le filet.
Aucun changement de logique attendu dans `chat_context.py` ; vérifier
seulement que le budget (`num_ctx − OUTPUT_RESERVE`, plancher `num_ctx//4`)
et le seuil 0.7 restent cohérents avec `num_predict`.

### A4. Backstops conservés

`RepetitionGuard` (fenêtre 40 lignes / ≤ 8 uniques) et le cap 120 k chars de
thinking restent en place. Critère de succès : ils ne déclenchent plus.

### Tests A

- Payload Ollama : `num_predict` et les options de profil présents/absents
  selon la config.
- `done_reason "length"` → `stopped: "length"` propagé jusqu'à l'UI/agent.
- Résolution profil vs override (stage ladder, défauts).

## Chantier B — Client MCP + toolsets par persona (chat agent seulement)

### B1. `harness/mcp_client.py`

Client MCP stdio en stdlib pur (subprocess + JSON-RPC 2.0, messages délimités
par ligne), cohérent avec le style zéro-dépendance du harness.

- Handshake `initialize` (protocolVersion récente avec fallback sur celle du
  serveur) + `notifications/initialized`, puis `tools/list`, `tools/call`.
- Spawn paresseux au premier usage, clé (serveur, workspace) — `cwd` = le
  workspace de la session (git et playwright opèrent dedans).
- Timeout par appel (défaut 60 s) ; crash → un restart, puis erreur.
- Toute défaillance devient une string d'erreur lue par le modèle, jamais une
  exception (même contrat que `agent_tools`).

### B2. Configuration

```toml
[mcp.servers.git]
command = ["uvx", "mcp-server-git"]
tools = ["git_status", "git_diff", "git_log", "git_add", "git_commit"]
readonly = ["git_status", "git_diff", "git_log"]
```

`tools` est une allowlist (le reste du serveur n'est pas exposé) ;
`readonly` exempte de l'approbation ; `env` optionnel.

### B3. Namespacing, dispatch, approbation

- Nom exposé au modèle : `mcp__git__git_status`.
- `factory_mcp._drain_calls` route le préfixe `mcp__` vers le client ;
  `needs_approval` : True sauf si l'outil est dans `readonly`.

### B4. Toolsets par persona

- `factory.toml`, table `[toolsets]` : chaque toolset liste outils natifs +
  outils MCP. Défaut `base` = les 6 natifs (comportement actuel préservé).
- La session chat choisit son toolset à la création ; warning au-delà de
  ~15 outils exposés (les modèles ≤ 35b se dégradent au-delà).
- La section « Outils » d'`agent.md`/du system prompt est générée depuis le
  toolset réel — pas de doc statique qui ment.

### B5. Serveurs v1

| Serveur | Commande | Outils exposés |
|---|---|---|
| fetch | `uvx mcp-server-fetch` | fetch |
| search | serveur DuckDuckGo (npx, sans clé API) | search, fetch_content |
| git | `uvx mcp-server-git` | ~6 (status, diff, log, add, commit) |
| playwright | `npx @playwright/mcp` | ~8 essentiels (navigate, snapshot, click, type, screenshot, console) |

Prérequis Windows : `npx` et `uvx` disponibles — à vérifier en début
d'implémentation ; fallback `python -m` pour les serveurs Python installés
via pip.

### Tests B

Faux serveur MCP stdio (script Python de fixture) : handshake, list, call,
timeout, crash/restart, allowlist, readonly/approbation, namespacing,
warning de budget. Suite existante (449 tests) au vert.

## Chantier C — Pilote blitzvolley

- Stack réelle : Node pur (express + geckos.io), tests `node --test
  backend/test/*.test.js` — **pas** vitest.
- Le runner `vitest` est en réalité « commande complète exécutée dans le
  worktree, node_modules junctionné » : réutilisable tel quel avec
  `regression_cmd = ["node", "--test", "backend/test/"]`. Vérifier à
  l'implémentation (dossier vs glob selon la version de node) ; renommer le
  kind en générique (« node ») seulement si friction.
- `factory.toml` : `[projects.blitzvolley]`,
  `path = "C:/Users/me/Documents/Info/VibeCoding/Blobby/BlobVolley"`.
- Tâche pilote : une tâche taille-module du TODO blitzvolley, démarrage
  direct barreau 3 (30b, profils A2 actifs), 35b en closer — conformément au
  signal de routage du 15/07. Rapport d'audit en fin de run.

## Non-objectifs

- Outils MCP dans le ladder (extension après preuve en chat).
- Context7, « tous » les serveurs MCP, adaptation systématique des plugins
  claude-code (les skills/commands sont des prompts : transposition
  ponctuelle en personas si un besoin concret apparaît).
- Qwen3.6:35b « partout » : il reste le barreau final + point d'entrée des
  contrats taille-module ; le 30b garde les petits fixes (704 s/tâche au 35b).

## Risques

- Sélection d'outils dégradée sur petits modèles malgré le cap : mesurer via
  session-audit, réduire les toolsets au besoin.
- Disponibilité/latence de `npx`/`uvx` sous Windows au spawn des serveurs.
- Le passage du 30b de temp 0.1 → 0.7 peut changer le comportement de
  délégation : surveiller le premier run, override par stage en secours.
- `num_predict` 8192 peut tronquer un très gros patch ladder : visible en
  `stopped: "length"` dans le log du job, l'escalade de barreau existante
  prend le relais.
