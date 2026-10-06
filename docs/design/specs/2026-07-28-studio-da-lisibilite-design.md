# Studio : direction artistique et principe de lisibilité

Design validé avec Martin le 2026-07-28. Couvre les sections 1 et 2 de
`docs/ui-backlog.md` — la DA transverse et le principe « ne pas tout afficher
par défaut ». Les sections 3 à 8 du backlog restent ouvertes et hériteront de
la DA sans être retouchées.

## Pourquoi maintenant

Le backlog dicté le 2026-07-28 compte soixante items. Deux d'entre eux
conditionnent tous les autres : sans DA arrêtée, chaque écran livré ensuite
serait à refaire ; sans règle de lisibilité, la conversation continue de
déverser toute la réflexion de l'agent à l'écran, ce que le backlog résume par
« beaucoup trop de pollution dans la conversation ».

## Décisions verrouillées

| Sujet | Décision |
|---|---|
| Registre | Sombre, pictogrammes duotone arrondis, coins adoucis |
| Accent | `#d2b672` (doré, milieu champagne/laiton clair) |
| Texte sur accent | `#1a1608` — l'accent est plus clair que le fond |
| Police d'interface | Instrument Sans (400/500/600) |
| Police de code et de chiffres | JetBrains Mono (400/500) |
| Pictogrammes | Sous-ensemble Phosphor duotone (MIT), sprite SVG local |
| Lisibilité pendant le tour | Une seule ligne d'activité vivante, réécrite en place |
| Lisibilité après le tour | Une ligne récapitulative repliée, dépliable d'un clic |

Écartés en connaissance de cause : l'indigo-violet (jugé insuffisamment
distinctif), l'ambre (confusion avec l'état « rejeté »), la sarcelle (confusion
avec le vert de succès), Inter (« vu partout »), IBM Plex, Manrope, Satoshi,
Schibsted Grotesk, Geist, et la piste éditoriale serif (trois familles à
tenir).

## Contraintes de fabrication

Elles ne sont pas négociables et découlent du code existant.

- **Pas de build.** Le studio sert des fichiers statiques ; il n'a ni bundler
  ni dépendance npm (`harness/tests/test_web_markdown.py:8-13`). Polices et
  sprite sont donc des fichiers posés sur disque.
- **Hors-ligne.** Aucun appel réseau au runtime. Les polices sont
  auto-hébergées en woff2, jamais chargées depuis un CDN.
- **Pas d'`innerHTML`.** Le texte affiché vient d'un modèle et de fichiers sur
  disque. Le rendu passe par la construction de nœuds, comme aujourd'hui.
- **Logique pure isolée.** Ce qui se teste sans DOM vit dans son propre
  fichier et se teste via `node` depuis pytest, sur le modèle de
  `markdown.js` / `test_web_markdown.py`.
- **Aucun changement serveur.** Ni endpoints, ni format de transcript, ni
  lanes GPU/cloud. Une seule exception, purement déclarative : deux entrées
  MIME (`.woff2`, `.svg`) dans `factory_web.py:30-32`, sans quoi les fichiers
  partent en `application/octet-stream`.
- **Fichiers statiques à plat.** La route `/static/([\w.-]+)`
  (`factory_web.py:436`) interdit les séparateurs. Élargir la regex à un
  sous-dossier ouvrirait une traversée de chemin, puisque `..` passe déjà le
  filtre de caractères. Polices et sprite vivent donc directement dans
  `harness/web/`.

## 1. Les jetons de design

Nouveau fichier `harness/web/tokens.css`, chargé avant `style.css`, qui déclare
tout ce qui relève du goût. `style.css` ne contient plus de valeur littérale de
couleur, de police ou de rayon : il consomme les variables. C'est ce qui fait
que les vues Jobs, Dashboard, Modèles et Projets changent d'apparence sans
qu'on touche à leur code.

### Palette

```
--bg: #121219        fond de page
--panel: #1a1a24     panneaux, bulles de l'agent, blocs de code
--line: #262634      séparateurs, bordures
--fg: #d7d8e2        texte courant
--dim: #9092a6       texte secondaire, libellés
--accent: #d2b672    accent doré
--accent-deep: #bd9c58   fin de dégradé (bulle utilisateur, boutons)
--on-accent: #1a1608     texte posé sur l'accent
```

L'accent est réservé à ce qui est **actif** : onglet courant, agent au travail,
code en ligne, boutons d'action primaires, ligne d'activité.

### États

L'accent doré occupe le territoire chromatique de l'ancien orange `#c98a3f` :
les deux se confondent, ce qui a été constaté sur maquette. Les états sont donc
redistribués.

```
--ok: #57c99a        succeeded
--err: #d4695f       failed, error
--rejected: #b98ad9  rejected  (était orange)
--dead: #7a8194      dead      (était violet)
```

### Typographie

Instrument Sans et JetBrains Mono, sous-ensemble latin, en woff2 posées à plat
dans `harness/web/` (cf. contrainte de route ci-dessus), déclarées en
`@font-face` avec `font-display: swap` et un repli `system-ui` / `monospace`.
Budget visé : environ 120 Ko au total.

Trois réglages s'appliquent partout, et pèsent autant que le choix de la police :

- interlignage `1.6` sur la prose ;
- `font-variant-numeric: tabular-nums` sur toute mesure affichée — durées,
  tok/s, compteurs — pour que les chiffres ne dansent pas quand ils
  s'actualisent ;
- libellés en petites capitales espacées (`letter-spacing: .11em`,
  `text-transform: uppercase`, ~10 px).

### Pictogrammes

Un sprite `harness/web/icons.svg` regroupant les symboles Phosphor duotone
nécessaires, et une fonction `icon(nom)` dans `app.js` qui rend un `<svg><use>`.
Une seule source, un seul style ; ajouter une icône plus tard coûte une ligne.

Jeu initial, dérivé des besoins réels du studio : réflexion, fichier, écriture,
terminal, succès, erreur, avertissement, information, attente, approbation,
plan, mémoire, session, jobs, modèles, projets, git, diff, horloge, GPU, cloud,
arrêt, envoi, repli, copie, suppression, renommage, recherche.

### Ménage inclus

- Le bouton `🔔 TEST TOAST` codé en dur (`harness/web/index.html:32`) est
  supprimé : c'est un reste de mise au point.
- Les prompts utilisateur sont élargis (item de la section 1 du backlog,
  gratuit une fois dans le CSS).

## 2. Le flux de conversation

### Pendant le tour

Un seul élément vivant en bas du fil, réécrit en place, jamais empilé :
pictogramme + verbe + cible.

```
⥁  réfléchit…  12 s
▣  lit  harness/regress.py
▶  py -3 -m pytest harness/tests/test_regress.py   3,1 s  ✓
```

Un clic déplie le détail complet — réflexion intégrale, arguments de l'appel,
sortie de la commande. Rien n'est supprimé, tout est à un clic.

Les sources existent déjà côté client : `app.js:845-870` reçoit
`thinking`, `chunk`, `tool_call`, `tool_result`. Il s'agit de les rendre
autrement, pas d'en produire de nouveaux.

### Dictionnaire des outils

La traduction « appel d'outil → pictogramme, verbe, cible » vit dans une table
unique. Outils couverts :

| Outil | Rendu |
|---|---|
| `read_file` | ▣ lit `<path>` |
| `list_dir` | ▣ liste `<path>` |
| `search` | ⌕ cherche `<pattern>` |
| `write_file` | ✎ écrit `<path>` |
| `edit_file` | ✎ modifie `<path>` |
| `run_command` | ▶ `<command>` |
| `remember` / `recall` | ◈ note / relit `<clé>` |
| `log_wall` | ▲ mur : `<résumé>` |
| `set_plan` / `step_done` | ☰ plan / étape `<n>` |
| `ask_operator` / `request_resource` | ✋ demande à l'opérateur |
| `delegate`, `job_*` | ⚙ délègue / suit `<job>` |

Un outil absent de la table **n'est pas une erreur** : il retombe sur son nom
brut et un pictogramme générique. Les toolsets évoluent ; la vue ne doit pas
casser quand ils évoluent.

### À la fin du tour

La ligne se fige en récapitulatif replié :

```
›  4 outils · 18 s · 2 lus, 1 test ✓
```

Un clic la redéroule intégralement. Au rechargement de la page, le même
récapitulatif est reconstruit côté client en groupant les messages par tour :
un tour va d'un message `user` au suivant. Les messages portent déjà `role`,
`ts` et `tool_name` — le format du transcript ne change pas.

### Ce qui n'est jamais replié

Les messages de l'utilisateur, les réponses de l'agent, les demandes
d'approbation et les erreurs. Le repli range le bavardage ; il ne cache jamais
ce qui exige une décision.

### Deux corrections de comportement

- La réflexion cesse d'être forcée ouverte pendant le streaming
  (`app.js:851`). C'est la cause directe du « toutes les réflexions ouvertes »
  du backlog.
- La ligne d'activité se rafraîchit au plus dix fois par seconde. Le rendu par
  delta est le motif de coût déjà mesuré à 11,5 ms/token
  (`app.js:1018-1021`). La fin de tour force un dernier affichage, pour qu'une
  mise à jour tardive ne soit pas avalée par la fenêtre.

### Découpage des fichiers

- `harness/web/activity.js` — **pur, sans DOM** : table des outils, groupement
  des messages en tours, formatage du récapitulatif. Testé via `node`.
- `harness/web/app.js` — rendu DOM de la ligne et du récapitulatif, sans
  `innerHTML`.
- `harness/web/tokens.css` — jetons ; `style.css` les consomme.

## Vérification

1. **Fonctions pures** (`node` depuis pytest, comme `test_web_markdown.py`) :
   table des outils, outil inconnu, groupement en tours, session commençant
   sans message `user`, formatage du récapitulatif (pluriels, durées,
   comptages).
2. **Hors-ligne** (pytest, pas de navigateur) : aucun `http://` ou `https://`
   dans le HTML et le CSS servis. C'est la seule garantie mécanique qu'un
   `@import` Google Fonts ne se glissera pas un jour dans le fichier.
3. **Preuve visuelle** : capture Playwright montrée à Martin, exécutée hors
   gate — un test navigateur ne doit pas bloquer un push
   (`.githooks/pre-push:15-18`).

## Risques identifiés

- **Tour sans message `user` en tête** — session reprise, message système
  initial. Le groupement doit produire un tour « avant-propos » plutôt que de
  perdre les messages. Testé.
- **Fenêtre de rafraîchissement** — un tour qui se termine à l'intérieur de la
  fenêtre de 100 ms perdrait sa dernière ligne. La fin de tour force
  l'affichage. Testé.
- **Poids du sprite** — environ 30 Ko servis en local, une fois, sans requête
  réseau. Accepté.

## Hors périmètre

Restent ouverts au backlog, et hériteront de la DA sans refonte : gestion de
projets, groupement et renommage des sessions, bannières pliables, vue git,
affichage du plan et de son avancement, paramétrage des modèles, rendu `.md`
enrichi.
