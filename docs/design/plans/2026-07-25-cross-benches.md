# Plan — bancs croisés codeur & reviewer

**Créé 2026-07-25.** Protocole exécutable des deux bancs décidés après
`audits/20260725-delegation-economics.md`. Rien ne s'implémente dans le harnais
(routage `start_stage` inclus) avant que ces mesures soient tombées — décision
Martin 25/07 : *« faisons les tests avant, pour éviter qu'on force un système
dysfonctionnel »*.

**Contrainte permanente : matériel grand public.** 12 Go de VRAM est un
invariant produit, pas une variable d'ajustement.

---

## Banc A — codeur : taxe de harnais × écart modèle

### Tâche commune

**Une seule tâche pour tous les modèles**, choisie complexe pour trier les
faibles — un banc où tout le monde rate à 0 ne trie rien, un banc trivial non
plus.

**Retenu : « Saisons ranked » (BlitzVolley, SCOPE 2)** — reset périodique de
l'ELO + récompenses de fin de saison par palier.

Pourquoi celle-là :
- logique **pure, testable sans réseau** → score objectif, pas une appréciation ;
- vraiment complexe : bornes de saison par date, compression douce de l'ELO vers
  une cible, mapping palier→récompense, et les cas tordus qui séparent les
  modèles (joueur non classé, placement inachevé, joueur au plancher, saison à
  cheval sur un fuseau) ;
- ~2-3 fichiers → bande « dur mais pas impossible » (le corpus dit que >2
  fichiers / >600 lignes = échec général) ;
- vrai item produit : si une branche est bonne, le travail n'est pas perdu.

*Repli si trop gros : « Streak de connexion » (courbe croissante, rupture sans
punition), même nature, un cran plus simple.*

### Protocole

1. J'écris **une** spec et **une** suite de tests, identiques pour tous.
   La suite est **cachée** aux modèles (elle est le juge, cf. ADR-010/014).
2. Chaque cellule part du même `base_sha`, sur **sa propre branche**
   `bench/a-<modele>-<regime>`.
3. **Score identique pour tous** : suite cachée + régression BlitzVolley
   (206 tests, `factory.toml`). Aucune subjectivité.
4. Métriques par cellule : verte O/N, nombre de tentatives, temps, tokens,
   cause d'échec (`no_code` / `spec_tests` / autre), taille du diff.

### Matrice — 10 cellules, complète (arbitrage Martin 25/07 : « il faut des données complètes »)

| Modèle | BRIDÉ | LIBRE |
|---|---|---|
| 7b local | ✔ | ✔ *(risque : ne route pas d'outils, `tools = false`)* |
| 30b local | ✔ | ✔ |
| 35b local | ✔ | ✔ |
| **Haiku** | ✔ | ✔ |
| **Sonnet** | ✔ | ✔ |

**BRIDÉ** = lane jobs telle quelle : aucun accès dépôt, n'exécute pas les tests,
prompt reconstruit à chaque tentative, clean slate à l'escalade, 3 essais,
32k ctx, 16k partagés réflexion+code.

**LIBRE** = conditions agent : lecture du dépôt, exécute les tests lui-même,
multi-tours, itère jusqu'au vert ou cap.

### Lectures

- **horizontal** (même modèle, bridé → libre) = **la taxe de harnais** ;
- **vertical** (même régime, local → cloud) = **l'écart de modèle** ;
- **local + libre** = *le local sait-il exploiter la liberté, ou se noie-t-il ?* ;
- **Haiku bridé** = la cellule la plus informative du banc : petit modèle mais
  post-entraîné agentique, donc elle sépare « savoir coder » de « savoir rendre
  sa copie » — les 54 % de `no_code`.

### Limite méthodologique à consigner dans les résultats

Les cellules cloud passent par des sous-agents dispatchés depuis Claude Code.
**Un sous-agent est nativement en conditions libres** (il a les outils). Le
régime BRIDÉ est donc émulé : spec + contenu des fichiers fournis en ligne,
interdiction d'utiliser les outils. **C'est une consigne, pas une contrainte
dure** — à mentionner dans le rapport, et à contrôler en relisant la trace.

### Coût

Local : GPU seulement. Cloud : 4 runs de sous-agent. Runs longs **détachés**
(`Start-Process` + logs fichiers), jamais via le runner de tâches (incident 18/07).

---

## Banc B — reviewer : précision × rappel

Zéro token Claude en régime permanent : tout tourne en local via llama-server.
Le seul coût Claude est l'écriture des scripts, une fois.

### Corpus

- **Précision** — les **17 diffs verts** déjà sur disque (`jobs/j_*/result.json`,
  statut `succeeded`, `review` renseignée) avec leur `goal`. Vérité terrain :
  spec verte **et** régression verte. **Tout REJECT = faux positif.**
- **Rappel** — **~10 mutants** : un diff vert dans lequel on injecte
  mécaniquement un défaut d'une classe connue. **Tout ACCEPT = faux négatif.**

### Génération des mutants — déterministe, sans LLM

Opérateurs de mutation classiques appliqués aux lignes `+` du diff (le nombre de
lignes ne change pas, donc les en-têtes de hunk restent valides) :

| Opérateur | Exemple |
|---|---|
| inversion arithmétique | `a + b` → `a - b` |
| inversion de comparaison | `>` → `<=`, `===` → `!==` |
| off-by-one | `N` → `N ± 1` |
| inversion booléenne | `true` → `false` |
| inversion logique | `&&` → `\|\|` |

Reproductible, auditable, gratuit.

**Faille méthodologique à traiter, pas à masquer** : un mutant qui ne casse
réellement rien rendrait le test de rappel injuste (le reviewer serait pénalisé
pour avoir accepté du code inoffensif). Deux garde-fous :
1. relecture humaine du lot généré (~10 items, coût trivial) ;
2. **rigoureux** : rejouer les tests de spec du job d'origine contre le mutant
   et ne garder que ceux qui virent au rouge. À faire si le lot relu laisse un
   doute.

### Matrice

| Axe | Modalités |
|---|---|
| Modèle reviewer | 7b · 30b · 35b |
| Provenance du diff | **auto-review** (le modèle relit son propre code — l'actuel) vs **review croisée** (un autre modèle) |
| Contexte donné | **diff seul** (l'actuel) vs **diff + fichiers complets** |

La review croisée est **locale → locale** : elle ne coûte aucun token Claude.
C'est le correctif le plus probable, puisque **3 des 5 faux positifs sont le 7b
relisant son propre code**.

### Prompt mesuré

Le banc importe `build_review_prompt` et `_system_prompt("reviewer")` depuis
`harness/loop_job.py`. **On mesure le reviewer qui tourne en prod, pas un
nouveau** — et le banc reste en phase si le prompt change.

### Sortie

Précision et rappel par cellule. **Critère de décision fixé d'avance** : la
boucle review → correction → vert ne s'active que si précision **et** rappel
dépassent le seuil. En dessous, on la remplace par des checks mécaniques
(scope creep, stubs — `count_stub_violations` existe déjà dans `harness/loop.py`).

### Hypothèse de Martin à tester explicitement

*« Le 35b devrait être plus précis. »* Plausible, **mais non acquis** : dans le
corpus, l'unique REJECT du 35b (`j_af40da82`, le quote fantôme) était lui aussi
faux. Le banc doit départager « quel modèle » de « pas le sien » et de
« avec quel contexte ».

---

## Ordre d'exécution

1. **Banc B** (aucune dépendance, aucun coût Claude récurrent).
2. **Banc A** — cellules locales d'abord (gratuites), puis les 4 cellules cloud.
3. Décisions au registre `docs/backlog-optimisations.md`, puis seulement ensuite
   les implémentations gelées (routage `start_stage`, boucle review→correction,
   « donner des yeux », mémoire intra-barreau).
