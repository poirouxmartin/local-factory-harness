# Backlog UI du studio — liste brute, non priorisée

Dictée par Martin le 2026-07-28. **Rien n'est planifié ici** : c'est le dépôt
fidèle de la liste, regroupée par thème pour qu'on puisse en faire des lots
ensuite. Un item n'est retiré que quand il est livré (avec le commit en face),
jamais parce qu'il paraît redondant — deux formulations proches disent souvent
deux gênes différentes.

Convention : `[ ]` à faire · `[~]` en cours · `[x]` livré (commit) ·
`[?]` à clarifier avant de chiffrer.

---

## 1. Direction artistique (transverse — conditionne le reste)

- [ ] Faire une DA propre et homogène.
- [ ] Tout doit être plus plaisant visuellement.
- [x] Plus de contraste, deuxième passe. (Redit le 29/07.)
      → le vrai coupable n'était pas l'écart entre les gris mais leur
      ABSENCE : `class="muted"` est posé à plus de vingt endroits d'app.js et
      n'avait aucune règle CSS, donc tout le texte secondaire se rendait comme
      du corps de texte. Règle ajoutée, plus cinq marches de surface
      (`--panel-2`, `--dim-2`) au lieu de trois. Dix couleurs littérales qui
      traînaient dans style.css sont passées aux jetons — un test le garantit
      maintenant, la promesse « aucune couleur littérale » était fausse.
- [x] Plus de contraste dans l'UI. (Dicté le 29/07.)
      → le grief n'était pas le texte principal : `--line` sur `--bg` donnait
      1,5:1, donc panneau, tableau et fond se lisaient comme une seule nappe.
      Fond descendu, panneau monté, ligne rendue visible. Et surtout le blanc
      sur l'or (1,9:1) remplacé par `--on-accent` partout où il traînait :
      bouton primaire, bulles utilisateur, badge « en cours », bouton de
      retour en bas.
- [x] Boutons trop gros. (Dicté le 29/07.)
      → 8/16 px et la taille du corps de texte faisaient des boutons plus
      hauts que les champs qu'ils accompagnent : 5/12 px et 13 px. Les champs
      suivent, avec un anneau de focus qui n'existait pas.
- [~] Beaucoup de pictogrammes plutôt que du texte brut ou des chiffres nus —
      SVG propres et homogènes sur tout le site. (Redit le 29/07.)
      → onze symboles ajoutés au sprite (même grammaire duotone) et les
      boutons qui portaient une phrase sont devenus des verbes dessinés :
      « Rafraîchir depuis OpenRouter », « Enregistrer »/« Effacer » chez les
      fournisseurs, Charger/Décharger, la fiche modèle, le mode. Le libellé
      n'est pas perdu, il passe en `title`/`aria-label`. **Reste** : les vues
      Jobs, Déléguer et Dashboard n'ont pas été traversées.
- [ ] Police d'écriture uniforme et plus belle (à décider avec la DA).
- [ ] Police à changer **absolument**.
- [ ] Couleur d'accent différente.
- [ ] Affichage des `.md` beaucoup plus beau.
- [ ] Prompts utilisateurs : plus larges.

## 2. Le principe de lisibilité à trancher d'abord

- [x] **Ne pas tout afficher par défaut** : montrer l'action en cours ou un
      micro-extrait de la réflexion, plus une icône adaptée. Idem pour l'usage
      de la mémoire — ça rend la chose plus ludique.
      → une ligne vivante pendant le tour (`activityLine`), un récapitulatif
      replié après (`.recap`). Lot DA du 28-29/07.
- [x] Beaucoup trop de pollution dans la conversation, toutes les réflexions
      ouvertes.
      → `think.open = true` par delta supprimé : la réflexion reste comme
      l'opérateur l'a laissée.
- [x] Affichage beaucoup plus esthétique et clair de ce qu'il se passe.
      → jetons de design, polices auto-hébergées, sprite de pictogrammes.
- [ ] Indicateurs plus visuels : contexte, réflexion, commande en cours
      (pictogrammes animés correspondants), processus de réflexion, plan.
      → pictogrammes faits, **animation non faite**, et le plan n'a toujours
      pas de rendu dédié.

## 3. Projets et sessions

- [ ] Gestion de projets.
- [ ] TODO list par projet.
- [x] Grouper les sessions par projet.
      → un en-tête par projet **déclaré dans factory.toml**, compte à droite.
      Le repli sur le nom de dossier a été essayé puis retiré : il ouvrait
      vingt groupes d'une session nommés d'après des bacs à sable de banc.
      Tout le reste tient dans « Hors projet ». Lot quick wins du 29/07.
- [~] Améliorer la création de session et son paramétrage.
      → le modèle se choisit enfin à la création (il partait sur le premier de
      la liste, alphabétiquement) et le projet aussi, par son NOM — le serveur
      le résout en chemin, le champ workspace libre ne sert plus qu'au
      hors-périmètre. **La refonte du flot (bouton d'abord, paramètres
      ensuite) reste à maquetter.**
- [x] Noms de sessions plus explicites.
      → le titre auto lit la première ligne qui dit quelque chose (au lieu des
      60 premiers caractères bruts), coupe sur un mot et le signale par une
      ellipse.
- [~] Pouvoir renommer les sessions, et faire du tri.
      → renommage fait (bouton ✎, `POST /api/chats/<id>/title`, verrouillé
      pour qu'un tour suivant ne le défasse pas ; champ vidé = retour au nom
      auto). **Tri manuel toujours à faire** — l'ordre reste la date.
- [ ] Sessions à placer **sous** l'autre barre de gauche existante : elles
      bouffent trop de place.
- [x] Le bloc « nouvelle session » doit rester visible.
      → il est sorti de la zone qui défile : la colonne ne défile plus, sa
      liste défile. (Dicté le 29/07, pas dans la liste du 28.)
- [ ] Bannières de gauche pliables.
- [ ] Vue git.
- [~] Paramétrage des modèles et du reste — voir comment LM Studio le fait.
      → **choix en deux temps** (fournisseur, puis modèle) dans la session ET
      à la création : à dix-huit modèles un menu plat obligeait à connaître son
      id par cœur. La route vient du serveur (`route`/`route_label`), elle
      n'est pas re-parsée en JS. **Fiche technique** derrière le ⚙ du bandeau :
      id, fournisseur, fenêtre, fenêtre de session, outils, prix, source (ou
      quant/poids/args en local). **Lecture seule** — cf. §13.

## 4. Plan et avancement

- [ ] Affichage de l'avancement du plan en cours.
- [ ] Affichage du plan et de son avancement : **il n'y a rien pour le
      moment** (doublon assumé avec l'item précédent — la gêne est double :
      exister, puis avancer).
- [ ] Partie spécifique pour les infos de session : contexte actuel,
      avancement du plan, etc. ?
- [ ] Quand une longue commande est lancée, avoir des infos sur son
      avancement.
- [ ] Temps passé sur la dernière commande.

## 5. Métriques, coûts, efficience

- [ ] Stats globales de session : nombre d'appels d'outils, tokens in, out,
      coût équivalent estimé, temps passé, efficience.
- [ ] Affichage du nombre total de tokens de la session.
- [ ] Affichage d'un pourcentage d'efficience de la session.
- [ ] Améliorer l'affichage live des tokens/s.
- [ ] Affichage des coûts dans la bannière de droite : passer en minutes ou
      heures si ça dépasse ; afficher aussi le temps total pour voir le gain
      potentiel.
- [ ] Attention : certains coûts sont jugés inutiles alors qu'ils ne le sont
      pas forcément.
- [ ] Barre latérale d'infos, contexte : afficher `x / y` pour avoir l'info
      globale ; le pic en plus petit et en dessous ; en nombre **et**
      pourcentage.
- [ ] Incohérence entre l'affichage du contexte à droite et en bas.
- [ ] Affichage plus clair quand une compaction est en cours.

## 6. Machine (ressources)

- [ ] Affichage live de l'utilisation RAM.
- [ ] Affichage de l'utilisation VRAM, RAM, GPU, CPU.
- [ ] Bouton dans la session pour charger / décharger le modèle.

## 7. Mémoire et apprentissage visibles

- [ ] Affichage de ce qui est ajouté en mémoire ou dans les lessons.
- [ ] Affichage de l'utilisation de `MEMORY.md`, `lessons.json` ou autre.
- [ ] Bouton d'analyse des logs de session (bannière de droite).

## 8. Saisie de prompt

- [ ] À l'envoi : message sur plusieurs lignes pour rien, et ça ne scrolle pas
      tout en bas après l'envoi.
- [ ] Envoi de prompt qui décolle le scroll.
- [ ] Pouvoir mettre un message en file, ou interrompre.
- [ ] Sauvegarde des drafts de prompts.
- [ ] Pouvoir faire des listes dans les prompts (au moins quand on colle une
      liste).
- [ ] `\n` supprimés des prompts ?
- [ ] Ajout de screenshot / fichier dans le prompt.

## 9. Scroll et navigation

- [ ] Petite flèche quand on n'est pas scrollé tout en bas de la session.
- [ ] Scroll qui s'interrompt quand l'outil n'est plus en avant-plan.
- [ ] Quand on entre dans une session, aller directement tout en bas.

## 10. Bugs identifiés

- [x] Un « null » s'affichait sous « Choisis une session ou crées-en une
      nouvelle ».
      → `h()` faisait `append(null)` sur un enfant conditionnel (le bouton de
      scroll n'existe pas hors session). Les enfants absents sont filtrés.
- [ ] Au refresh, tous les timestamps sont cassés (remis à l'heure actuelle).
- [ ] Au refresh, le timer de session se remet à zéro.
- [ ] Le test toast ne fonctionne pas.
- [ ] `ask_operator` n'affiche rien.

## 11. Dialogue avec l'agent

- [ ] L'agent doit poser beaucoup plus de questions.
- [ ] Interface pour poser des questions et y répondre en un clic.
- [ ] À chaque appel rejoué ou en erreur, notifier l'agent pour qu'il en ait
      conscience.

## 12. Notifications et pilotage à distance

- [ ] Notifications quand ça finit.
- [ ] Remote control ?
- [ ] Reprendre `claude-remote` ?

## 13. Modèles : réglages et repères (dicté le 29/07)

- [ ] **Réglages poussés par modèle / par session** : température,
      échantillonnage, et le reste. Aujourd'hui `factory.toml` porte des
      profils `[models."nom"]` GLOBAUX ; une session ne peut régler que
      `num_ctx`. Il manque un endroit où ranger un réglage de session — sans
      ça, une commande dans la fiche mentirait (elle ne persisterait pas).
      La fiche technique existe et est prête à les accueillir.
- [ ] **Repères de perf par modèle** : « sur quoi ce modèle est bon ».
      À décider avant de coder : **la source**. On en a une vraie et à nous —
      `experiments/coder_bench/runs/*/report.json` (banc A/B : 35b 10/12 sans
      aucun échec de compréhension, 30b 10/12 sans aucun échec de protocole,
      7b inapte au diff) — mais elle ne couvre que 3 modèles locaux, pas les
      15 cloud. Trois options : n'afficher que ce qu'on a mesuré ; importer
      des scores publics (invérifiables ici) ; ou n'afficher que des faits
      observés en session (taux d'échec d'outil, tours/tâche).
- [ ] Vue dédiée aux benchmarks des modèles (l'onglet « Benchmarks » existe
      déjà en « à venir » dans la nav).

---

## Notes de cadrage (à valider avant de planifier)

- **La DA passe devant.** Police, couleur d'accent et jeu de pictogrammes
  conditionnent la moitié des items : les faire après, c'est repeindre deux
  fois.
- **Trois items sont déjà à moitié en place** : le panneau de plan existe dans
  le DOM (`#plan-slot-container`, travail du 27/07), la barre de métriques de
  session porte déjà tokens/coût, et le ledger de coût par appel existe côté
  banc (`experiments/coder_bench/cloud_client.py`) — il n'est pas branché sur
  la lane chat.
- **`ask_operator` n'affiche rien** est fonctionnel, pas cosmétique : le
  handback existe côté harnais depuis le 24/07. Sans surface UI, le contrat
  d'autonomie est muet — candidat au premier lot avec le test toast.
- **Deux items dépendent d'un choix produit, pas d'un écran** : « poser
  beaucoup plus de questions » et « ne pas tout afficher par défaut ».

---

## 14. Lot dicté le 29/07 (adaptateur ChatGPT + passe UI)

Livré :

- [x] Favicon dans la DA (silhouette d'usine or sur panneau, plus le bleu
      d'origine) et **icône dans le bandeau, à gauche de « local-factory »**.
- [x] **Prompts sur deux lignes même très courts.** Cause trouvée : une bulle
      prend une largeur « au plus juste », donc l'horodatage en `float: right`
      ne tenait JAMAIS à côté d'un texte court et retombait à la ligne. Il est
      posé sur la même ligne de base que le texte. Mesuré dans le studio :
      « Salut ! » passe de 44 px à 22 px de haut.
- [x] **Création de session : le bouton d'abord, les réglages ensuite.**
      Sept contrôles à traverser précédaient « + Nouvelle session ».
- [x] **Bouton de chargement du modèle dans le bandeau de session**, pour un
      modèle local absent de la VRAM seulement. Au passage : « ○ non chargé »
      s'affichait aussi sur un modèle cloud, qui n'a rien à charger.
- [x] Pastilles plus foncées et plus proches de la DA, un peu plus hautes,
      rayon plus court (2,5/9 px, 7 px au lieu de 1/8 px, 10 px). Jeu
      `--*-solid` séparé : un mot d'état sur le fond et une pastille remplie
      n'ont pas le même problème de lisibilité.
- [x] **Pastille « running » en vert** — l'or reste ce qui attend l'opérateur.
- [x] Marge entre boutons voisins (ils étaient collés), et anneau de focus.
- [x] **Liens « console » en bleu illisible sur noir** → pictogramme de lien
      sortant.
- [x] **Catalogue de modèles groupé par fournisseur**, avec le compte à
      droite. Le regroupement lit `route_label`, qui vient du serveur.
- [x] Alignement du ⚙ de la fiche modèle (il pendait 10 px sous les listes :
      `button { margin-top: 10px }` est juste sous un label, faux en rangée).
- [x] **Mode auto / approbation en pictogramme** : bouclier ↔ éclair, l'or
      marquant le mode qui engage l'agent seul.
- [x] **Projet affiché par son NOM** dans le bandeau, chemin en infobulle.
- [x] Vue Projets : chaque projet s'ouvre sur une fiche (dépôt/git,
      instructions, régression, TODO) — **placeholders assumés**, aucune route
      serveur ne rend encore ces données. La fiche survit au rafraîchissement
      automatique de dix secondes, qui la refermait.
- [x] Onglet **Benchmarks** ouvert (il était « à venir ») avec trois sections
      placeholder, et **réglages poussés + repères de perf** en placeholder
      dans la fiche modèle. Cf. §13 : ce qui bloque n'est pas l'écran mais la
      source, et elle reste à trancher.

Trouvés en chemin, pas dictés :

- [x] « distant (OpenRouter) » était écrit en dur dans le catalogue : faux
      pour toutes les routes directes, et ChatGPT s'annonçait servi par un
      agrégateur qu'il ne traverse jamais.
- [x] Les deux bandes latérales gardaient leurs 584 px hors session, alors
      qu'elles n'ont rien à afficher : le tableau des modèles se serrait au
      milieu de deux colonnes vides.
- [x] La clé d'un fournisseur configuré s'annonçait par le mot « succeeded »
      (un nom d'état de job).

Garde-fous ajoutés (`test_web_offline.py`) : aucune couleur littérale dans
style.css, pas de `float` sur l'horodatage, tout pictogramme demandé existe
dans le sprite, et tous les assets portent la même marque de cache.

## Lot du 29/07 (soir) — pictogrammes hors chat, et ce qu'ils ont découvert

- [x] **Passe pictogrammes sur Jobs / Déléguer / Dashboard / Modèles /
      Projets / Benchmarks** — le reste de la dictée du 29/07. Un titre, une
      mesure et une carte portent leur marque à gauche du libellé
      (`heading()`, `stat()`, `card()`), et les actions du diff (appliquer,
      copier, télécharger) deviennent des boutons dessinés.
- [x] **La barre de chiffres des Jobs n'avait jamais rien affiché** : le
      handler `/api/stats` `return`ait son dict au lieu de l'écrire
      (ERR_EMPTY_RESPONSE) et comptait des états inventés (« pending »,
      « done ») absents de `job_store`.
- [x] **Et derrière, aucun chiffre n'était juste** : `_compute_job_metrics`
      lisait `result["result"]["attempts"]` et `duration_s`, deux clés qui
      n'existent pas — la forme réelle est `attempts` et `total_seconds` à la
      racine. Tokens, coût, durée et nombre d'essais valaient zéro sur les 34
      jobs de la machine, en page Jobs comme dans la barre. Mesuré après
      correction : 362,9 k tokens, 194 min de calcul.
- [x] **Les liens n'avaient aucune règle CSS** : bleu d'usine du navigateur
      sur fond sombre (1,2:1), les identifiants de job du Dashboard étaient
      illisibles. Un lien porte maintenant la couleur du texte et son
      souligné ; l'or reste réservé à ce qui est actif.
- [x] **Le bouton « retour en bas » du chat était un rond vide** (aucun
      enfant, aucune règle `content`), et le saut initial en bas de session
      partait avant l'insertion dans le DOM — donc sur un élément de hauteur
      nulle. Corrigé, puis re-corrigé : un seul passage tombait encore 90 px
      trop haut (mise en page pas finie), d'où un second saut en
      `requestAnimationFrame`.
- [x] **Les colonnes du chat survivaient au changement d'onglet** : elles
      vivent hors de `#main`, que `MAIN.replaceChildren` ne touche pas — la
      liste des sessions restait plantée à côté des jobs.

Vérifié dans un vrai navigateur (Playwright, studio local) : barre de chiffres
peuplée, liens lisibles, bouton de retour en bas caché à l'ouverture puis
visible après un scroll, colonnes vidées en quittant le chat.

Piège rencontré : **trois studios écoutaient le même port 8787** (SO_REUSEADDR
laisse plusieurs processus se poser dessus). Les requêtes tombaient au hasard
sur un serveur au code périmé, et le correctif « ne marchait pas » une fois sur
deux. Vérifier `netstat -ano | findstr :8787` avant de conclure.
