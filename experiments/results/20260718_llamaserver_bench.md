# llama-server (b10066, CUDA 13.3) vs Ollama — 2026-07-18

Rig: RTX 5070 12 GB + 32 GB RAM, FA=1, KV q8_0 partout, `-ngl 99`,
`-ub 2048`, pp2048/tg128, r=2. Baselines Ollama = mesures réelles des jobs
du jour (attempt records) et chat.

## 30b (Qwen3-Coder-30B-A3B, UD-Q4_K_XL 16,45 GiB — blob ollama)

| runtime | config | prefill t/s | gen tok/s |
|---|---|---|---|
| Ollama (Q4_K_M, jobs du jour) | défaut | ~1614 | 56-58 |
| llama.cpp | ncmoe 28 | 1692 | 64,5 |
| llama.cpp | ncmoe 24 | 1729 | 72,2 |
| **llama.cpp** | **ncmoe 20** | **2385** | **75,5** |
| llama.cpp | ncmoe 18 | 1479 | 79,5 ± 6 (instable) |
| llama.cpp | ncmoe 16 | 117 | 36,7 (falaise VRAM) |

**Config retenue 30b : `--n-cpu-moe 20 -ub 2048` → +30 % gen, +48 % prefill vs Ollama.**

## 35b (Qwen3.6-35B-A3B, MTP UD-Q4_K_XL 21,27 GiB)

| runtime | config | prefill t/s | gen tok/s |
|---|---|---|---|
| Ollama (Q4_K_M officiel, job j_e2a040ff) | défaut | 1245 | 63,9 |
| llama.cpp autoregressif | ncmoe 99 | 580 | 47,6 |
| llama.cpp autoregressif | ncmoe 32 | 687 | 54,1 |
| llama.cpp autoregressif | ncmoe 26 | 1138 | 69,4 |
| llama.cpp autoregressif | ncmoe 24 | 827 ± 137 | 71,6 |
| llama.cpp autoregressif | ncmoe 22 | 200 | 28,3 (falaise VRAM) |
| **llama-server + MTP** (`--spec-type draft-mtp`) | **ncmoe 26** | 1138 (bench) | **90,5** (prompt code, draft accepté 253/288 = 88 %) |

**Config retenue 35b : `--n-cpu-moe 26 -ub 2048 --spec-type draft-mtp` →
+42 % gen vs Ollama, prefill comparable.** L'acceptation MTP à 88 % est
mesurée sur un prompt de code (cas d'usage réel des jobs) ; elle baissera
sur de la prose.

## Falaise VRAM

Le paramètre est brutal : un cran de trop bas sur `--n-cpu-moe` et les
poids débordent en managed memory → ÷2,5 sur tout. Les configs retenues
gardent une marge d'un cran.

## Qwen3-Coder-Next 80B-A3B (UD-Q2_K_XL, 24,92 GiB)

| config | prefill t/s | gen tok/s |
|---|---|---|
| ncmoe 99 (tout CPU) | 155 | 42,4 |
| **ncmoe 32** | **603** | **54,7** |
| ncmoe 30 | 332 | 57,0 |
| ncmoe 28 | 141 | 22,5 (falaise) |

## Qualité sur le rig (spec crgpd, harnais `d96beed`, via `llama_client`)

Deux erreurs de banc trouvées par autopsie avant les runs valides :
`--parallel 4` par défaut divise le contexte par 4 (prompt 10,7k tronqué à
8k → misses garantis, j_979ff9a2 annulé) ; et sans passthrough du profil de
sampling, les défauts llama.cpp (top_p .95/top_k 40/min_p .05) re-noient
les replies diff (j_575efd4c : 0/8, SEARCH de 618 lignes). Fix `b066c1b`.

| run | modèle | verdict | wall |
|---|---|---|---|
| j_7e0084b0 | 30b UD (ncmoe 24, np 1) | **8/8** | **128,4 s — record de la campagne** (best Ollama : 134 s) |
| j_37098e74 | Coder-Next UD-Q2_K_XL (ncmoe 32) | **8/8** | 355,8 s (a2 = 11,9k tokens appliqués, verbeux mais juste) |

## Verdicts

1. **Ollama vs llama-server : llama-server gagne** — parité qualité (vert sur
   le rig) + gen +25-40 % + record wall clock. Adoption recommandée pour la
   ladder ; chantier = cycle de vie des serveurs dans le harnais (un serveur
   = un modèle, swap par rung sous gpu_lock).
2. **35b vs 35b-MTP : MTP gagne** (90,5 vs 63,9 tok/s, 88 % d'acceptation
   sur du code ; décodage spéculatif = sortie identique en distribution).
   À confirmer par un job réel quand le closer sera sollicité.
3. **30b vs Coder-Next : le 30b reste le cheval de trait** (2,8× plus
   rapide sur cette spec) ; **Coder-Next Q2 = candidat closer** — qualité
   intacte à 2-bit sur ce job, +20 pts SWE-V de marge théorique pour les
   specs où le 30b plafonne. Départager Coder-Next vs 35b-MTP sur une spec
   dure (classe ranks.js/plus) au prochain besoin d'escalade.
4. La falaise VRAM se déplace avec le KV : ncmoe optimal au bench court
   (ctx 4k) est UN CRAN trop agressif à `-c 32768` (30b : 20 → 24).
