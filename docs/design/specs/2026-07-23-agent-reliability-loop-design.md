# Boucle de fiabilité de l'agent — design

Date : 2026-07-23
Statut : design validé, en attente de relecture opérateur avant plan d'implémentation.

## Problème

Autopsie de la session `c_d2a43fd5` (qwen3.6:35b, mode auto, toolset `base`,
tâche = génération d'image via ComfyUI) : 283 messages, 150 appels d'outils
dont **89 `run_command` / 27 `write_file`, 50/150 résultats en erreur**, aucune
image produite, fin bloquée sur un fichier gated HuggingFace.

Pathologies mesurées :

- **Retry aveugle** : `download_missing_models.py` ×3 d'affilée, `find_vae.py`
  ×5, `check_repos`/`check_flux_encoders` ×2.
- **Sprawl de scripts jetables** : `list_repo.py` → `list_repo2.py` → … →
  `list_repo12.py`, `download_remaining/rest/missing` — un script neuf réécrit à
  chaque échec au lieu de corriger.
- **Redécouverte du même état** : `dir models/` ~8 fois.
- **Zéro mémoire process** : `taskkill /F /PID 20352` deux fois, `netstat` deux
  fois.
- **Le vrai blocage (fichier gated) a pris ~80 commandes à diagnostiquer** au
  lieu de 2-3.

### Pourquoi les garde-fous existants n'ont rien attrapé

- `_LoopGuard` (`factory_mcp.py:193`) compte les répétitions d'un
  `(outil, args) → sortie` **strictement identiques** dans un tour (nudge à 3,
  stop à 5). Sa clé inclut la **sortie** : un run dont la sortie varie d'un
  octet (timestamp, download partiel) remet le compteur à 1 → `find_vae.py` ×5
  n'atteint jamais 5. Sa clé est l'**args exact** : `list_repo1..12` sont vus
  comme des appels distincts → boucle sémantique invisible.
- `workspace_memory.py` (MEMORY.md) et `lessons.py` **fonctionnent** — ils ont
  capturé cette session (MEMORY.md liste les 3 commandes qui échouent,
  lessons.json note `wasted_tool_calls: 17`, `tool_errors: 8`) — **mais ils sont
  cross-session** : écrits en fin de session (`_learn_from`), injectés au début
  de la suivante. Pendant les 80 commandes de thrash, ils n'ont rien fait.
- L'éviction (`chat_context.evict_tool_outputs`, garde 12 sorties) **efface la
  preuve** : après 12 appels, le modèle ne voit plus qu'il a déjà tout listé.

### Le diagnostic de fond

Claude Code ne boucle pas grâce à (1) un modèle fort en méta-cognition + (2) un
historique d'échec **visible en permanence**. La factory a un modèle faible ET
un historique amputé par l'éviction. On ne peut pas rendre le modèle fort ; on
peut **rendre explicite ce qu'un modèle frontier fait implicitement** : tracer
en direct, rendre la trace toujours visible, forcer le pivot.

## Principes

1. **Live, jamais post-session.** Tout ce qui est opérationnel (mémoire,
   anti-boucle, réflexion) s'écrit et se lit *pendant* la session, dès le 1ᵉʳ
   échec. Écrire en fin de session = payer une session de thrash avant
   d'apprendre = le pansement qu'on refuse. Seule exception : l'analyse
   `agent.md` (rétrospective, hors périmètre, cf. § Hors périmètre).
2. **Aucun bloc dur.** Le harness ne refuse jamais une action pour cause de
   répétition. Il **fait comprendre** (voici ce que tu as tenté, voici pourquoi
   ça casse) et **force le pivot** (ta prochaine action sur ce but doit être
   différente). Miroir, pas mur.
3. **Une seule source de vérité.** Pas de 4ᵉ système redondant : le « carnet »
   est la tranche chaude et live de MEMORY.md, pas un fichier de plus.
4. **Autonomie maximale.** L'agent épuise tout le soluble et propose des
   solutions même difficiles. Il ne sollicite l'humain que pour un arbitrage
   ou une ressource externe prouvée.
5. **Honnêteté sur la limite.** La garantie « jamais reproduit » est *ferme*
   pour les répétitions mécaniques (même cible/commande) et *forte mais
   bornée par le modèle* pour les erreurs conceptuelles — celles-ci dépendent
   que le modèle lise et tienne compte de sa mémoire. On maximise, on ne
   promet pas zéro.
6. **Contrainte KV.** Muter la tête système à chaque tour casse le préfixe KV
   (~1 s/tour de TTFT, mesuré le 22/07). Tout ce qui change chaque tour (la
   tranche chaude, le plan) est injecté **en queue de prompt**, jamais dans la
   tête. La tête reste octet-stable.

---

## B. Mémoire de travail live (source unique)

### B.1 Le store

MEMORY.md par workspace, **rendu live**. Trois natures d'entrées dans le bloc
`<!-- factory:auto -->` :

- **Issues d'appels** (auto) : `cible → ok | échec N× : raison`, écrit à
  l'instant où l'outil rend son résultat.
- **Notes sémantiques** (délibéré, via l'outil `remember`) : découvertes,
  décisions, positifs — « le VAE est gated → il faut un token HF », « ComfyUI
  doit tourner depuis son propre dossier », « python du PATH n'a pas pytest →
  `py -3.9` ».
- **Murs** (structurés, cf. § C.3).

### B.2 Fusion par cible

Une « cible » normalise l'action au-delà de son libellé exact :

- `run_command` → racine de la commande + argument principal
  (`python <script>.py` → cible `python:<script>` ; `dir <path>` → cible
  `dir:<path>`). Les scripts renommés qui font la même chose
  (`list_repo1..12.py`) ne collapsent pas s'ils ont des noms distincts — on
  normalise donc sur **l'intention déclarée** : au moment où l'agent enchaîne
  des `write_file` de scripts jetables suivis de `run_command`, la cible est le
  *verbe* que l'agent nomme dans sa note `remember` (« je liste le contenu du
  repo »). À défaut de note, on retombe sur (racine commande, 1er arg de
  chemin). Décision : commencer par la normalisation mécanique (racine + 1er
  arg de chemin), mesurer le taux de collapse sur les sessions archivées, et
  n'ajouter la normalisation par intention que si le mécanique laisse passer le
  sprawl.
- `edit_file` / `write_file` / `read_file` / `list_dir` → cible = chemin résolu.

Le carnet affiche **une ligne par cible** : `cible : N tentatives, dernière
issue = …`. Le sprawl devient visible comme *une* boucle.

### B.3 Injection

- **Tranche chaude** (toujours visible) : dérivée fraîche du store à chaque
  tour, injectée comme **dernier message avant la génération** (queue de
  `wire`, jamais dans `system_msgs`). Elle N'EST PAS persistée dans
  `session["messages"]` — sinon elle s'accumulerait et serait évincée ;
  transitoire, toujours à jour, toujours en queue. Contenu : les cibles
  répétées et/ou en échec, les murs ouverts, les N dernières notes sémantiques.
  Bornée (cible ~800 tokens ; on préfère un peu de contexte à 5 appels perdus,
  cf. principe 4).
- **Archive complète** : lue à la demande via l'outil `recall`.

### B.4 Outils natifs ajoutés

Ajoutés à `agent_tools.TOOLS` (donc au toolset `base`, présents dans tous les
toolsets) :

- `remember(note)` — écrit une note sémantique dans MEMORY.md, tout de suite.
  Read-tool (pas de gate d'approbation).
- `recall(query?)` — renvoie l'archive MEMORY.md (filtrée si `query`).
  Read-tool.
- `log_wall(mur, cause, alternatives)` — enregistre un mur structuré
  (cf. § C.3). Read-tool.

### B.5 Persistance / promotion

MEMORY.md est déjà le fichier persistant : le rendre live supprime l'étape
`_learn_from` de fin de session pour la partie mémoire. `lessons.json`
(niveau agent, cross-projet) est mis à jour **live** de la même façon (append
au 1ᵉʳ échec, plus d'attente de fin de session). La *promotion* des leçons
durables (dédup, compteurs) reste, mais s'exécute sur l'écriture live, pas en
post-session.

---

## C. Anti-boucle (zéro bloc dur, tout dynamique)

### C.1 Réflexion au 1ᵉʳ échec

Dès qu'un résultat d'outil est un échec (préfixe `error:` ou `exit` non-nul,
détecté dans `_run_call`), le harness **annote le résultat** rendu au modèle :

> `[noté au carnet] Avant de retenter sur cette cible, dis en une ligne ton
> hypothèse et en quoi ta prochaine action diffère de celle qui vient
> d'échouer.`

Pas de série à accumuler : chaque échec, dès le premier, déclenche le micro-
raisonnement. C'est le remplacement du `nudge à 3` par un `nudge à 1`, mais en
*annotation du résultat* (que le modèle lit forcément) plutôt qu'en message
système séparé.

### C.2 Détection de stagnation (contre les boucles longues)

Adossée au plan (§ A). L'avancement = nombre de **jalons dont la condition de
fin bascule non-atteint → atteint**. Si `K` actions passent sans qu'aucun jalon
ne bascule (K réglable, défaut à mesurer, ordre de grandeur 6-10), le harness
injecte en queue :

> `Tu as fait K actions sans avancer le plan. Relis ton carnet : soit tu
> changes d'approche sur l'étape bloquée, soit tu révises le plan.`

Signal, jamais blocage. Le `_LoopGuard` actuel (compteur `output`-sensible) est
**remplacé** par cette détection carnet + stagnation.

### C.3 Journal des murs

Un « mur » est un objet de première classe dans MEMORY.md, rempli par l'agent
quand il bute via un outil dédié `log_wall(mur, cause, alternatives)` (natif,
read-tool) :

```
MUR: <ce qui bloque>
  cause racine: <pourquoi>
  alternatives tentées: [<a> ✗, <b> ✗]
  alternatives restantes: [<c>, <d>]
```

Propriété clé : une alternative **barrée ne peut pas être retentée** — la
tranche chaude la montre barrée, et l'annotation C.1 force une alternative
*restante*. C'est l'anti-boucle du raisonnement conceptuel (celui qu'aucun
garde mécanique ne peut garantir).

Le mur ne se résout en handback que quand `alternatives restantes` est vide ET
que toutes celles épuisées pointaient vers une **ressource externe** (cf. § E).

### C.4 Escalade casse-mur

Coincé sur un mur → l'agent **escalade** vers le toolset `web`/`git` à la
demande pour *chercher* l'alternative (miroir non-gated, doc, autre outil). La
recherche web est la principale machine à générer des alternatives quand il est
bloqué. **Le mécanisme d'escalade à la demande fait l'objet d'une spec séparée**
(cf. § Hors périmètre) ; cette section n'en pose que le point d'accroche : le
journal des murs est le déclencheur naturel d'une escalade.

---

## A. Plan en amont (anticipation)

### A.1 Plan-first en mode auto

Le 1ᵉʳ tour d'une session agent en mode auto produit un **plan** avant tout
appel d'outil (hors lecture) :

- Étapes ordonnées, chacune avec une **condition de fin vérifiable** (« le
  fichier `ae.safetensors` existe dans `models/vae/` », « `generate.py` produit
  un PNG »).
- Chaque étape liste ses **murs potentiels** (« télécharger les modèles →
  certains peuvent être gated ») = pré-mortem, détection des murs *avant* de se
  jeter sur le code.

Le plan est un objet du store, **épinglé** (toujours injecté en queue, jamais
évincé).

### A.2 Coche des jalons

Quand la condition de fin est **mécaniquement vérifiable** (fichier existe,
test passe, port répond), c'est le **harness** qui coche — pas l'agent (contre
l'auto-délusion « étape 2 faite ! »). Quand elle ne l'est pas, l'agent la coche
via un outil, et la coche est visible/révisable.

### A.3 Mur vs plan

- Sur mur : **adapter l'étape bloquée** (but gardé, moyen changé).
- Réviser le plan seulement si le mur **invalide l'approche entière**.
- **Abandonner le but n'est jamais la décision de l'agent** → question à
  l'opérateur (§ E).

---

## E. Contrat d'autonomie

- Épuiser tout le soluble ; proposer des solutions même difficiles a priori.
- **Deux façons distinctes de solliciter l'humain, à ne pas confondre :**
  - **Question d'arbitrage** — un choix qui appartient à l'opérateur (« VAE
    fp16 12 Go ou fp8 6 Go ? »). L'agent pose la question et attend. Pour tout
    le reste, il tranche seul avec une **hypothèse énoncée**.
  - **Demande de ressource** — seulement quand un mur externe est prouvé
    (`alternatives restantes` vide + toutes externes). « Il me faut ton token
    HF. » Immédiat, précis, une seule chose demandée. Ce n'est pas un abandon :
    l'agent a résolu tout le résoluble et isolé le seul input externe.
- **Cible réaliste** : handback quasi nul ; quand il arrive, immédiat + précis.
  On ne garantit pas « zéro handback » (un token que l'opérateur est seul à
  avoir reste un token que l'opérateur est seul à avoir).
- **Pré-provisionnement des credentials** (token HF en env, etc.) = solution
  différée, dernier recours. On maximise d'abord la robustesse pour minimiser
  le besoin d'aide externe.

---

## Hors périmètre (specs / chantiers séparés)

- **Escalade à la demande des toolsets** (§ C.4) — sa propre spec : défaut
  `base`, l'agent réclame `web`/`git` quand il bute, on paie le coût des
  schémas MCP (2692 tokens pour `web`) une seule fois au besoin, pas à chaque
  tour. Ferme aussi les 2 bugs MCP ouverts (serveur qui verrouille le
  workspace via son CWD ; serveur mort qui raccourcit le préfixe KV en
  silence).
- **Analyse post-session + optimisation `agent.md`** — la seule chose qui
  reste légitimement rétrospective. Réécrire `agent.md` est un levier
  *falsifié* à cette échelle (A/B du 23/07, 35/35 des deux côtés) ; à rouvrir
  sur des sessions longues multi-étapes.
- **Pré-provisionnement des credentials** (§ E) — dernier recours.
- **Débats multi-personas** (ROADMAP `backlog:68`) — générateur d'alternatives
  plus lourd, à évaluer après que le journal des murs ait prouvé sa valeur.

## Ordre d'implémentation

Suivant la priorité opérateur (fiabilité d'abord) :

1. **B + C** — mémoire live + anti-boucle (carnet, fusion par cible, réflexion
   au 1ᵉʳ échec, journal des murs, `remember`/`recall`). Le gain de fiabilité
   immédiat.
2. **A** — plan / anticipation (jalons falsifiables, détection de stagnation,
   murs potentiels).
3. **E** — contrat d'autonomie (question vs demande de ressource) — en partie
   porté par le prompt système, en partie par le mécanisme de handback.

## Tests

- Purs et pilotables sans GPU, sur le modèle des modules `lessons.py` /
  `workspace_memory.py` existants (arithmétique / round-trip markdown testés
  via node quand du JS est concerné).
- **Replay des sessions archivées** (`jobs/chats/`, 44+ sessions) : rejouer
  `c_d2a43fd5` doit montrer le carnet qui collapse `find_vae` ×5 en une cible
  répétée et l'annotation de réflexion au 1ᵉʳ échec — sans GPU, comme la preuve
  hors-ligne du 22/07.
- Garde-fou KV : un test octet-stable confirme que la tête système ne change
  pas quand le carnet/plan change (ils sont en queue).
- Non-régression : la suite reste verte sous `py -3.9` (standard) et 3.12.

## Risques / limites assumés

- **Répétition conceptuelle** : bornée par la capacité du modèle à lire et
  suivre son carnet (principe 5). Le journal des murs la réduit fort, ne
  l'annule pas.
- **Normalisation de cible** : le collapse mécanique (racine + 1er arg) peut
  laisser passer un sprawl de scripts finement renommés → mesuré sur les
  archives avant d'ajouter la normalisation par intention (B.2).
- **Bruit du carnet** : un carnet trop verbeux coûte du contexte à chaque tour.
  Borné en taille ; on mesure le coût token réel vs le coût des appels évités.
