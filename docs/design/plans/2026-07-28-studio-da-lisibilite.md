# Plan d'implémentation — DA du studio et principe de lisibilité

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Donner au studio une direction artistique unique (accent doré, Instrument Sans, pictogrammes duotone) et remplacer le déversement de la réflexion de l'agent par une ligne d'activité vivante qui se fige en récapitulatif repliable.

**Architecture:** Un fichier de jetons CSS que tout le studio consomme, un sprite SVG local pour les pictogrammes, et un module JavaScript **pur** (`activity.js`) qui traduit les appels d'outils et regroupe les messages en tours. `app.js` ne fait que rendre le résultat dans le DOM. Aucun endpoint, aucun format de transcript ne change.

**Tech Stack:** HTML/CSS/JS servis à plat, sans build ni dépendance npm. Tests en pytest ; la logique JS pure est pilotée par `node` depuis pytest, exactement comme `harness/web/markdown.js` et `harness/tests/test_web_markdown.py`.

**Spec:** `docs/design/specs/2026-07-28-studio-da-lisibilite-design.md`

## Global Constraints

- **Pas de build.** Aucun bundler, aucune dépendance npm. Les fichiers sont servis tels quels.
- **Hors-ligne.** Aucune URL externe dans le HTML ou le CSS servis. Seule exception tolérée : l'espace de noms `http://www.w3.org/2000/svg`, qui est un identifiant XML et non une requête réseau.
- **Pas d'`innerHTML`.** Le texte vient d'un modèle et de fichiers sur disque. Construire des nœuds, comme le fait déjà `h()` (`harness/web/app.js:125-134`).
- **Statiques à plat.** La route `/static/([\w.-]+)` (`harness/factory_web.py:436`) interdit les séparateurs ; ne pas l'élargir (`..` passe déjà le filtre de caractères). Polices et sprite vivent dans `harness/web/`.
- **Palette exacte :** `--bg: #121219`, `--panel: #1a1a24`, `--line: #262634`, `--fg: #d7d8e2`, `--dim: #9092a6`, `--accent: #d2b672`, `--accent-deep: #bd9c58`, `--on-accent: #1a1608`, `--ok: #57c99a`, `--err: #d4695f`, `--rejected: #b98ad9`, `--dead: #7a8194`.
- **Polices :** Instrument Sans (400/500/600), JetBrains Mono (400/500).
- **Suite de tests :** `py -3 -m pytest harness/tests -q -m "not browser"` doit rester verte (1087 tests au 2026-07-28). `py -3` et non `py -3.9` : 3.12 est le standard de la fabrique.
- **Commits directs sur `main`**, convention du projet (`CLAUDE.md`). Message en anglais, ASCII, conventional commit.

---

### Task 1: Jetons de design, polices, ménage

**Files:**
- Create: `harness/web/tokens.css`
- Create: `harness/web/instrument-sans.woff2` (binaire téléchargé)
- Create: `harness/web/jetbrains-mono.woff2` (binaire téléchargé)
- Create: `harness/tests/test_web_offline.py`
- Modify: `harness/web/index.html:9` (lien vers `tokens.css`), `:32` (suppression du bouton de test)
- Modify: `harness/web/style.css:1-5` (le bloc `:root` devient un import de jetons), `:78-82` (largeur des prompts)
- Modify: `harness/factory_web.py:30-32` (MIME `.woff2`, `.svg`)

**Interfaces:**
- Consumes: rien.
- Produces: les variables CSS listées dans les contraintes globales, disponibles pour toutes les tâches suivantes ; les familles `"Instrument Sans"` et `"JetBrains Mono"`.

- [ ] **Step 1: Écrire le test hors-ligne (il doit échouer)**

Créer `harness/tests/test_web_offline.py` :

```python
"""The studio must run with no network at all.

A single @import to a font CDN turns a working offline studio into a blank
one on a plane -- and it is the kind of line that gets pasted in without
thinking. This test is the mechanical guarantee, and it is cheap.

The w3.org SVG namespace is an XML identifier, not a fetch: it is allowed.
"""
import re

import factory_web

WEB = factory_web.WEB_DIR
ALLOWED = ("http://www.w3.org/",)
URL = re.compile(r"https?://[^\s\"')]+")


def external_urls(text):
    return [u for u in URL.findall(text) if not u.startswith(ALLOWED)]


def test_no_external_url_in_served_html_and_css():
    for path in sorted(list(WEB.glob("*.html")) + list(WEB.glob("*.css"))):
        assert external_urls(path.read_text(encoding="utf-8")) == [], path.name


def test_fonts_are_self_hosted():
    css = (WEB / "tokens.css").read_text(encoding="utf-8")
    assert css.count("@font-face") >= 2
    for family in ('"Instrument Sans"', '"JetBrains Mono"'):
        assert family in css
    assert 'url("/static/instrument-sans.woff2")' in css
    assert 'url("/static/jetbrains-mono.woff2")' in css


def test_font_files_exist_and_are_woff2():
    for name in ("instrument-sans.woff2", "jetbrains-mono.woff2"):
        blob = (WEB / name).read_bytes()
        assert blob[:4] == b"wOF2", name
        assert len(blob) > 10_000, name


def test_woff2_and_svg_have_a_mime_type():
    assert factory_web.MIME[".woff2"] == "font/woff2"
    assert factory_web.MIME[".svg"] == "image/svg+xml"
```

- [ ] **Step 2: Lancer le test, vérifier qu'il échoue**

Run: `py -3 -m pytest harness/tests/test_web_offline.py -q`
Expected: FAIL — `tokens.css` n'existe pas, `MIME` n'a pas `.woff2`.

- [ ] **Step 3: Télécharger les deux polices**

Google Fonts sert du woff2 quand la requête ressemble à un navigateur récent. Récupérer d'abord les URL, puis les fichiers :

```bash
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'
curl -sA "$UA" 'https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400..600' > /tmp/is.css
curl -sA "$UA" 'https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400..500' > /tmp/jb.css
grep -o 'https://[^)]*\.woff2' /tmp/is.css | tail -1
grep -o 'https://[^)]*\.woff2' /tmp/jb.css | tail -1
```

`tail -1` prend le bloc `latin` (les blocs précédents sont cyrillique/grec). Télécharger ensuite :

```bash
curl -so harness/web/instrument-sans.woff2 "<url instrument sans latin>"
curl -so harness/web/jetbrains-mono.woff2 "<url jetbrains mono latin>"
ls -l harness/web/*.woff2
```

Vérifier que chaque fichier fait plus de 10 Ko et que le total reste sous 200 Ko. Le sous-ensemble `latin` de Google couvre les accents français (é, è, à, ç, ù, ô).

- [ ] **Step 4: Écrire `harness/web/tokens.css`**

```css
/* Everything the studio's look is made of. style.css consumes these and
   holds no literal colour, font or radius of its own -- which is why the
   Jobs, Dashboard, Models and Projects views change appearance without
   being touched.

   Self-hosted woff2, flat in harness/web/: the /static route forbids
   separators (factory_web.py:436) and the studio must run offline. */

@font-face {
  font-family: "Instrument Sans";
  src: url("/static/instrument-sans.woff2") format("woff2");
  font-weight: 400 600;
  font-display: swap;
}
@font-face {
  font-family: "JetBrains Mono";
  src: url("/static/jetbrains-mono.woff2") format("woff2");
  font-weight: 400 500;
  font-display: swap;
}

:root {
  --bg: #121219;
  --panel: #1a1a24;
  --line: #262634;
  --fg: #d7d8e2;
  --dim: #9092a6;

  /* Gold sits where the old orange "rejected" badge sat, and the two were
     confused on sight (mockup, 2026-07-28). The accent is reserved for what
     is ACTIVE: current tab, agent at work, inline code, primary buttons. */
  --accent: #d2b672;
  --accent-deep: #bd9c58;
  --on-accent: #1a1608;

  --ok: #57c99a;
  --err: #d4695f;
  --rejected: #b98ad9;   /* was orange */
  --dead: #7a8194;       /* was purple */

  --font-ui: "Instrument Sans", system-ui, sans-serif;
  --font-mono: "JetBrains Mono", ui-monospace, Consolas, monospace;

  --radius: 10px;
  --radius-lg: 14px;
}

/* Three settings carry as much of the "elegant" as the typeface does. */
body { line-height: 1.6; }
.num, td.num, .metric-value { font-variant-numeric: tabular-nums; }
.label-caps {
  font-size: 10px; letter-spacing: .11em; text-transform: uppercase;
  color: var(--dim);
}
```

- [ ] **Step 5: Brancher les jetons**

Dans `harness/web/index.html`, avant la ligne 9 :

```html
<link rel="stylesheet" href="/static/tokens.css?v=1">
```

Supprimer entièrement la ligne 32 (`<button id="test-toast-btn" …>`) — reste de mise au point.

Dans `harness/web/style.css`, supprimer le bloc `:root` des lignes 1-5 : les jetons vivent désormais dans `tokens.css`.

**Attention — 27 règles de `style.css` consomment encore les anciens noms.** Les renommer toutes, dans ce même commit ; il ne doit rester qu'un seul nom par couleur (vérifié : `app.js` n'utilise aucune variable CSS, le chantier est confiné à `style.css`).

```
var(--green)  -> var(--ok)
var(--red)    -> var(--err)
var(--orange) -> var(--rejected)
var(--purple) -> var(--dead)
```

Contrôle : `grep -c 'var(--\(green\|red\|orange\|purple\))' harness/web/style.css` doit retourner 0.

Faire ensuite pointer les polices sur les variables :

```css
body {
  margin: 0; display: flex; flex-direction: column; height: 100vh;
  overflow: hidden; background: var(--bg); color: var(--fg);
  font: 14px/1.6 var(--font-ui);
}
```

Remplacer aussi `font-family: ui-monospace, Consolas, monospace;` (ligne 83) par `font-family: var(--font-mono);`.

Élargir les prompts (item du backlog) : dans la règle `input, textarea, select` (lignes 78-82), ajouter `max-width: 100%;` et porter le `padding` à `10px 12px`.

Les règles `.badge.rejected` et `.badge.dead` (`style.css:62-63`) sont déjà couvertes par le renommage — ne pas en ajouter de nouvelles, ce serait un doublon. Vérifier seulement que leur `color` reste lisible sur les nouveaux fonds : `#fff` sur le mauve `#b98ad9` est trop faible, passer cette règle-là à `color: var(--on-accent)`.

- [ ] **Step 6: Ajouter les deux MIME**

Dans `harness/factory_web.py`, lignes 30-32 :

```python
MIME = {".html": "text/html; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".woff2": "font/woff2",
        ".svg": "image/svg+xml"}
```

- [ ] **Step 7: Lancer les tests**

Run: `py -3 -m pytest harness/tests/test_web_offline.py -q`
Expected: PASS (4 tests)

Run: `py -3 -m pytest harness/tests -q -m "not browser"`
Expected: PASS, aucun test perdu par rapport à 1087.

- [ ] **Step 8: Commit**

```bash
git add harness/web/tokens.css harness/web/instrument-sans.woff2 harness/web/jetbrains-mono.woff2 harness/web/index.html harness/web/style.css harness/factory_web.py harness/tests/test_web_offline.py
git commit -F - <<'EOF'
feat(studio): design tokens, self-hosted fonts, offline guarantee

One file holds everything the look is made of, so the views nobody touches
change with it. The gold accent takes the territory the orange "rejected"
badge used to hold -- they were confused on sight -- so rejected goes mauve
and dead goes slate.

The offline test is the point: a single @import to a font CDN turns a
working studio into a blank page with no network, and it is exactly the kind
of line that gets pasted in without thinking.
EOF
```

---

### Task 2: `activity.js` — traduire un appel d'outil

**Files:**
- Create: `harness/web/activity.js`
- Create: `harness/tests/test_web_activity.py`

**Interfaces:**
- Consumes: rien.
- Produces: `describeCall(name, args) -> {icon: string, verb: string, target: string}`, exporté via `module.exports` pour pytest, comme `markdown.js:109-111`. Les valeurs de `icon` sont les identifiants de symboles que la tâche 3 doit fournir.

- [ ] **Step 1: Écrire les tests (ils doivent échouer)**

Créer `harness/tests/test_web_activity.py` :

```python
"""The studio's activity line (`harness/web/activity.js`).

Pure functions, no DOM, driven through `node` -- the studio itself stays
Node-free (no build step, no dependency), exactly like markdown.js.
"""
import json
import shutil
import subprocess

import pytest

import factory_web

ACT_JS = factory_web.ROOT / "harness" / "web" / "activity.js"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def call(fn, *args):
    out = subprocess.run(
        [NODE, "-e",
         "const m = require(process.argv[1]);"
         "const a = JSON.parse(process.argv[3]);"
         "process.stdout.write(JSON.stringify(m[process.argv[2]](...a)))",
         str(ACT_JS), fn, json.dumps(args)],
        capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_read_file_reads_its_path():
    assert call("describeCall", "read_file", {"path": "harness/regress.py"}) == {
        "icon": "file", "verb": "lit", "target": "harness/regress.py"}


@needs_node
def test_run_command_shows_the_command_itself():
    d = call("describeCall", "run_command", {"command": "py -3 -m pytest -q"})
    assert d == {"icon": "terminal", "verb": "", "target": "py -3 -m pytest -q"}


@needs_node
def test_set_plan_counts_its_steps():
    d = call("describeCall", "set_plan",
             {"goal": "g", "steps": [{"step": "a"}, {"step": "b"}]})
    assert d == {"icon": "plan", "verb": "pose le plan", "target": "2 etapes"}


@needs_node
def test_an_unknown_tool_is_not_an_error():
    """Toolsets move. The view must not break when they move."""
    d = call("describeCall", "brand_new_tool", {"whatever": "x"})
    assert d == {"icon": "tool", "verb": "brand_new_tool", "target": "x"}


@needs_node
def test_a_long_target_is_clipped():
    d = call("describeCall", "run_command", {"command": "x" * 200})
    assert len(d["target"]) == 80
    assert d["target"].endswith("…")


@needs_node
def test_missing_arguments_never_throw():
    assert call("describeCall", "read_file", {}) == {
        "icon": "file", "verb": "lit", "target": ""}
    assert call("describeCall", "read_file", None) == {
        "icon": "file", "verb": "lit", "target": ""}
```

- [ ] **Step 2: Lancer les tests, vérifier qu'ils échouent**

Run: `py -3 -m pytest harness/tests/test_web_activity.py -q`
Expected: FAIL — `Cannot find module .../activity.js`

- [ ] **Step 3: Écrire `harness/web/activity.js`**

```js
// What the operator sees while the agent works: one live line, rewritten in
// place, instead of every thought and every tool blob stacked in the log.
//
// Pure on purpose -- no DOM, no globals. app.js renders what this returns,
// and pytest drives it through node (test_web_activity.py), the same deal
// as markdown.js.

const CLIP = 80;

function clip(s) {
  s = s == null ? "" : String(s);
  return s.length > CLIP ? s.slice(0, CLIP - 1) + "…" : s;
}

// name -> icon id (see icons.svg), verb, and how to read the target out of
// the call arguments. Argument names come from the tool definitions
// themselves: agent_tools.py, carnet.py, factory_mcp.py.
const TOOLS = {
  list_dir:         { icon: "file",     verb: "liste",    t: (a) => a.path },
  read_file:        { icon: "file",     verb: "lit",      t: (a) => a.path },
  search:           { icon: "search",   verb: "cherche",  t: (a) => a.pattern },
  write_file:       { icon: "pencil",   verb: "ecrit",    t: (a) => a.path },
  edit_file:        { icon: "pencil",   verb: "modifie",  t: (a) => a.path },
  run_command:      { icon: "terminal", verb: "",         t: (a) => a.command },
  remember:         { icon: "memory",   verb: "note",     t: (a) => a.note },
  recall:           { icon: "memory",   verb: "relit",    t: (a) => a.query },
  log_wall:         { icon: "wall",     verb: "mur",      t: (a) => a.wall },
  set_plan:         { icon: "plan",     verb: "pose le plan",
                      t: (a) => (a.steps || []).length + " etapes" },
  step_done:        { icon: "plan",     verb: "etape",    t: (a) => a.step },
  ask_operator:     { icon: "hand",     verb: "demande a l'operateur",
                      t: (a) => a.question },
  request_resource: { icon: "hand",     verb: "demande une ressource",
                      t: (a) => a.resource },
  delegate:         { icon: "gear",     verb: "delegue",  t: (a) => a.goal },
  job_status:       { icon: "gear",     verb: "suit le job",   t: (a) => a.job_id },
  job_result:       { icon: "gear",     verb: "lit le job",    t: (a) => a.job_id },
  job_log:          { icon: "gear",     verb: "lit le log",    t: (a) => a.job_id },
  job_cancel:       { icon: "gear",     verb: "annule le job", t: (a) => a.job_id },
};

// An unknown tool is not an error -- it is a toolset that moved. Show its
// name and the first thing that looks like a value.
function firstValue(args) {
  for (const v of Object.values(args)) {
    if (typeof v === "string" || typeof v === "number") return v;
  }
  return "";
}

function describeCall(name, args) {
  const a = args || {};
  const e = TOOLS[name];
  if (!e) return { icon: "tool", verb: String(name), target: clip(firstValue(a)) };
  let target = "";
  try { target = e.t(a); } catch (_) { target = ""; }
  return { icon: e.icon, verb: e.verb, target: clip(target) };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { describeCall, TOOLS };  // for the pytest harness only
}
```

- [ ] **Step 4: Lancer les tests**

Run: `py -3 -m pytest harness/tests/test_web_activity.py -q`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add harness/web/activity.js harness/tests/test_web_activity.py
git commit -F - <<'EOF'
feat(studio): read a tool call as icon, verb and target

The live activity line needs one sentence per call, not a JSON blob clipped
at 80 characters. Argument names are taken from the tool definitions
themselves, so the table cannot drift from what the agent actually sends.

An unknown tool falls back to its own name: toolsets move, and the view must
not break when they move.
EOF
```

---

### Task 3: Le sprite de pictogrammes

**Files:**
- Create: `harness/web/icons.svg`
- Modify: `harness/web/app.js` (ajouter `icon()` juste après `h()`, ligne 134)
- Modify: `harness/tests/test_web_activity.py` (ajouter le test croisé)

**Interfaces:**
- Consumes: `TOOLS` de `activity.js` (tâche 2).
- Produces: `icon(name)` dans `app.js`, qui rend `<svg class="ico"><use href="/static/icons.svg#ico-<name>"></use></svg>`.

- [ ] **Step 1: Écrire le test croisé (il doit échouer)**

Ajouter à la fin de `harness/tests/test_web_activity.py` :

```python
def test_every_icon_named_by_the_table_exists_in_the_sprite():
    """One sprite, one style. A table entry pointing at a missing symbol
    renders an empty box, and nothing says why."""
    sprite = (factory_web.WEB_DIR / "icons.svg").read_text(encoding="utf-8")
    table = json.loads(call_raw_tools())
    wanted = {e["icon"] for e in table.values()} | {"tool"}
    missing = [n for n in sorted(wanted) if 'id="ico-%s"' % n not in sprite]
    assert missing == []
```

et le lecteur de table, juste au-dessus :

```python
def call_raw_tools():
    out = subprocess.run(
        [NODE, "-e",
         "const {TOOLS} = require(process.argv[1]);"
         "const o = {};"
         "for (const [k, v] of Object.entries(TOOLS)) o[k] = {icon: v.icon};"
         "process.stdout.write(JSON.stringify(o))",
         str(ACT_JS)],
        capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return out.stdout
```

Marquer le test `@needs_node`.

- [ ] **Step 2: Lancer le test, vérifier qu'il échoue**

Run: `py -3 -m pytest harness/tests/test_web_activity.py -q -k sprite`
Expected: FAIL — `icons.svg` n'existe pas.

- [ ] **Step 3: Écrire `harness/web/icons.svg`**

Un fichier de symboles, style duotone : une forme de fond à `opacity=".22"` remplie en `currentColor`, un contour à `stroke-width="1.5"`, coins arrondis. Les identifiants sont préfixés `ico-`.

Symboles requis par la table : `ico-file`, `ico-search`, `ico-pencil`, `ico-terminal`, `ico-memory`, `ico-wall`, `ico-plan`, `ico-hand`, `ico-gear`, `ico-tool`.
Symboles requis par l'interface : `ico-think`, `ico-ok`, `ico-err`, `ico-warn`, `ico-info`, `ico-wait`, `ico-chevron`, `ico-stop`, `ico-send`, `ico-copy`, `ico-trash`, `ico-clock`, `ico-gpu`, `ico-cloud`, `ico-git`, `ico-diff`, `ico-session`, `ico-jobs`, `ico-models`, `ico-projects`.

Squelette, avec les trois premiers symboles écrits en entier — les autres suivent exactement la même construction (fond `opacity=".22"`, contour `1.5`) :

```svg
<svg xmlns="http://www.w3.org/2000/svg" style="display:none">
  <symbol id="ico-file" viewBox="0 0 24 24" fill="none">
    <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"
          fill="currentColor" opacity=".22"/>
    <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"
          stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>
    <path d="M14 3v5h5" stroke="currentColor" stroke-width="1.5"
          stroke-linejoin="round"/>
  </symbol>
  <symbol id="ico-terminal" viewBox="0 0 24 24" fill="none">
    <rect x="2.5" y="4.5" width="19" height="15" rx="3"
          fill="currentColor" opacity=".22"/>
    <rect x="2.5" y="4.5" width="19" height="15" rx="3"
          stroke="currentColor" stroke-width="1.5"/>
    <path d="M7 10l3 2-3 2M13 15h4" stroke="currentColor" stroke-width="1.6"
          stroke-linecap="round" stroke-linejoin="round"/>
  </symbol>
  <symbol id="ico-think" viewBox="0 0 24 24" fill="none">
    <circle cx="12" cy="12" r="9" fill="currentColor" opacity=".22"/>
    <circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.5"
            opacity=".35"/>
    <path d="M12 3a9 9 0 0 1 9 9" stroke="currentColor" stroke-width="2"
          stroke-linecap="round"/>
  </symbol>
  <!-- … les autres symboles, même construction … -->
</svg>
```

Source des tracés : Phosphor Icons, licence MIT (`https://github.com/phosphor-icons/core`). Recopier les chemins dans ce fichier ; ne pas ajouter de dépendance ni de lien externe.

- [ ] **Step 4: Ajouter `icon()` dans `app.js`**

Juste après `h()` (`harness/web/app.js:134`) :

```js
// One sprite, one style. `use` keeps the markup to one line per icon and the
// file cached once -- served locally, never fetched from a CDN.
function icon(name, cls) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "ico" + (cls ? " " + cls : ""));
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "/static/icons.svg#ico-" + name);
  svg.append(use);
  return svg;
}
```

Et la règle de taille dans `style.css` :

```css
.ico { width: 16px; height: 16px; flex-shrink: 0; display: block; }
.ico.lg { width: 20px; height: 20px; }
```

- [ ] **Step 5: Lancer les tests**

Run: `py -3 -m pytest harness/tests/test_web_activity.py -q`
Expected: PASS (7 tests)

Run: `py -3 -m pytest harness/tests/test_web_offline.py -q`
Expected: PASS — le `xmlns` w3.org du sprite est autorisé, aucune autre URL.

- [ ] **Step 6: Commit**

```bash
git add harness/web/icons.svg harness/web/app.js harness/web/style.css harness/tests/test_web_activity.py
git commit -F - <<'EOF'
feat(studio): one duotone sprite for every pictogram

Phosphor duotone paths copied in (MIT), served locally, addressed by
`use href`. The cross-check test is what keeps it honest: a table entry
pointing at a symbol nobody drew renders an empty box, and nothing on screen
says why.
EOF
```

---

### Task 4: `activity.js` — grouper les tours et formater le récapitulatif

**Files:**
- Modify: `harness/web/activity.js` (ajouter deux fonctions et les exporter)
- Modify: `harness/tests/test_web_activity.py` (ajouter les tests)

**Interfaces:**
- Consumes: `describeCall` (tâche 2).
- Produces:
  - `groupTurns(messages) -> [{lead: message|null, items: [message]}]` — `lead` est le message `user` qui ouvre le tour, `null` pour le tour d'avant-propos.
  - `formatRecap(turn) -> string` — par exemple `"4 outils · 18 s · 2 lus, 1 test"`.

- [ ] **Step 1: Écrire les tests (ils doivent échouer)**

Ajouter à `harness/tests/test_web_activity.py` :

```python
@needs_node
def test_a_turn_runs_from_one_user_message_to_the_next():
    msgs = [{"role": "user", "content": "a", "ts": 1},
            {"role": "tool", "tool_name": "read_file", "ts": 2},
            {"role": "assistant", "content": "ok", "ts": 3},
            {"role": "user", "content": "b", "ts": 4},
            {"role": "assistant", "content": "ok2", "ts": 5}]
    turns = call("groupTurns", msgs)
    assert len(turns) == 2
    assert turns[0]["lead"]["content"] == "a"
    assert len(turns[0]["items"]) == 2
    assert turns[1]["lead"]["content"] == "b"


@needs_node
def test_messages_before_the_first_user_message_are_kept():
    """A resumed session, or a system message in front. Losing them would be
    silent, which is the one thing we do not accept."""
    msgs = [{"role": "system", "content": "s", "ts": 1},
            {"role": "user", "content": "a", "ts": 2}]
    turns = call("groupTurns", msgs)
    assert turns[0]["lead"] is None
    assert len(turns[0]["items"]) == 1
    assert turns[1]["lead"]["content"] == "a"


@needs_node
def test_an_empty_transcript_has_no_turn():
    assert call("groupTurns", []) == []


@needs_node
def test_recap_counts_tools_and_duration():
    turn = {"lead": {"role": "user", "ts": 100},
            "items": [{"role": "tool", "tool_name": "read_file", "ts": 101},
                      {"role": "tool", "tool_name": "read_file", "ts": 102},
                      {"role": "tool", "tool_name": "run_command", "ts": 105},
                      {"role": "assistant", "content": "ok", "ts": 118}]}
    assert call("formatRecap", turn) == "3 outils · 18 s · 2 lus, 1 commande"


@needs_node
def test_recap_singular_when_there_is_one_tool():
    turn = {"lead": {"role": "user", "ts": 10},
            "items": [{"role": "tool", "tool_name": "read_file", "ts": 11},
                      {"role": "assistant", "content": "ok", "ts": 13}]}
    assert call("formatRecap", turn) == "1 outil · 3 s · 1 lu"


@needs_node
def test_a_turn_with_no_tool_has_no_recap():
    turn = {"lead": {"role": "user", "ts": 10},
            "items": [{"role": "assistant", "content": "ok", "ts": 12}]}
    assert call("formatRecap", turn) == ""
```

- [ ] **Step 2: Lancer les tests, vérifier qu'ils échouent**

Run: `py -3 -m pytest harness/tests/test_web_activity.py -q -k "turn or recap"`
Expected: FAIL — `m.groupTurns is not a function`

- [ ] **Step 3: Implémenter**

Ajouter dans `harness/web/activity.js`, avant l'export :

```js
// A turn runs from one user message to the next. Anything sitting in front
// of the first user message -- a resumed session, a system preamble -- goes
// into a lead-less turn rather than being dropped on the floor.
function groupTurns(messages) {
  const turns = [];
  let cur = null;
  for (const m of messages || []) {
    if (m.role === "user") {
      cur = { lead: m, items: [] };
      turns.push(cur);
      continue;
    }
    if (!cur) { cur = { lead: null, items: [] }; turns.push(cur); }
    cur.items.push(m);
  }
  return turns;
}

const READS = new Set(["read_file", "list_dir", "search"]);
const WRITES = new Set(["write_file", "edit_file"]);

function plural(n, one, many) { return n + " " + (n > 1 ? many : one); }

function formatRecap(turn) {
  const tools = (turn.items || []).filter((m) => m.role === "tool");
  if (!tools.length) return "";
  let reads = 0, writes = 0, commands = 0;
  for (const m of tools) {
    if (READS.has(m.tool_name)) reads++;
    else if (WRITES.has(m.tool_name)) writes++;
    else if (m.tool_name === "run_command") commands++;
  }
  const parts = [plural(tools.length, "outil", "outils")];
  const start = turn.lead ? turn.lead.ts : (turn.items[0] || {}).ts;
  const end = (turn.items[turn.items.length - 1] || {}).ts;
  if (start != null && end != null) parts.push(Math.round(end - start) + " s");
  const what = [];
  if (reads) what.push(plural(reads, "lu", "lus"));
  if (writes) what.push(plural(writes, "ecrit", "ecrits"));
  if (commands) what.push(plural(commands, "commande", "commandes"));
  if (what.length) parts.push(what.join(", "));
  return parts.join(" · ");
}
```

Étendre l'export : `module.exports = { describeCall, groupTurns, formatRecap, TOOLS };`

- [ ] **Step 4: Lancer les tests**

Run: `py -3 -m pytest harness/tests/test_web_activity.py -q`
Expected: PASS (13 tests)

- [ ] **Step 5: Commit**

```bash
git add harness/web/activity.js harness/tests/test_web_activity.py
git commit -F - <<'EOF'
feat(studio): group a transcript into turns and summarise one

The recap line after a turn is computed from the stored messages, client
side: role, ts and tool_name are already there, so the transcript format
does not move.

Messages sitting before the first user message land in a lead-less turn. A
resumed session must not lose them silently.
EOF
```

---

### Task 5: La ligne d'activité vivante

**Files:**
- Modify: `harness/web/app.js:840-906` (le `feed()` du streaming), `:944-951` (`toolChip` devient le détail déplié)
- Modify: `harness/web/index.html:33` (charger `activity.js` avant `app.js`)
- Modify: `harness/web/style.css` (styles `.activity`)

**Interfaces:**
- Consumes: `describeCall` (tâche 2), `icon()` (tâche 3).
- Produces: dans `app.js`, `activityLine()` qui retourne `{el, show(iconName, verb, target), flush()}`.

- [x] **Step 1: Charger `activity.js`**

Dans `harness/web/index.html`, avant `app.js` (ligne 34) :

```html
<script src="/static/activity.js"></script>
```

- [x] **Step 2: Écrire la ligne d'activité dans `app.js`**

À placer près de `toolChip` (`harness/web/app.js:944`) :

```js
// One live line instead of a stack of blobs. Rewritten in place, at most ten
// times a second: rendering per delta is the cost pattern already measured at
// 11.5 ms/token (see chatBubble). The end of a turn forces a last paint, so a
// late update is never swallowed by the window.
function activityLine() {
  const ico = h("span", { class: "activity-ico" });
  const label = h("span", { class: "activity-label" });
  const el = h("div", { class: "activity" }, ico, label);
  let pending = null, queued = false, lastPaint = 0;

  const paint = () => {
    queued = false;
    lastPaint = Date.now();
    if (!pending) return;
    ico.replaceChildren(icon(pending.icon));
    label.textContent = [pending.verb, pending.target]
      .filter(Boolean).join(" ");
    pending = null;
  };
  const schedule = () => {
    if (queued) return;
    const wait = Math.max(0, 100 - (Date.now() - lastPaint));
    queued = true;
    setTimeout(paint, wait);
  };
  return {
    el,
    show(iconName, verb, target) {
      pending = { icon: iconName, verb, target };
      schedule();
    },
    flush() { if (pending) paint(); },
  };
}
```

- [x] **Step 3: Brancher la ligne sur les événements**

Dans `feed()` (`harness/web/app.js:845-906`), remplacer les trois branches concernées.

Réflexion — la ligne remplace l'ouverture forcée du `<details>` :

```js
    if (msg.thinking) {
      const r = bubble();
      r.think.hidden = false;
      r.thinkText.textContent += msg.thinking;
      activity.show("think", "reflechit…", "");
      if (STATUS) { STATUS.set("réflexion"); STATUS.feed(msg.thinking.length); }
    }
```

La ligne `r.think.open = true;` disparaît : c'est la cause directe du « toutes les réflexions ouvertes » du backlog. La ligne `r.think.open = false;` de la branche `msg.chunk` disparaît aussi, devenue sans objet.

Appel d'outil :

```js
    } else if (msg.tool_call) {
      seal();
      const d = describeCall(msg.tool_call.name, msg.tool_call.arguments);
      activity.show(d.icon, d.verb, d.target);
      chip = toolChip(msg.tool_call.name, msg.tool_call.arguments, "",
        Date.now() / 1000);
      chip.open = false;
      log.append(chip);
      reply = null;
      if (STATUS) STATUS.set("outil : " + msg.tool_call.name);
    }
```

Rédaction, et fin de tour :

```js
    } else if (msg.chunk) {
      const r = bubble();
      activity.show("pencil", "redige la reponse", "");
      r.text.textContent += msg.chunk;
      if (STATUS) { STATUS.set("rédaction"); STATUS.feed(msg.chunk.length); }
    }
```

Dans la branche `msg.done` et dans la branche `msg.error`, appeler `activity.flush()` en première instruction, puis retirer `activity.el` du log.

Créer la ligne à côté de `reply`/`chip` (`harness/web/app.js:831-832`) :

```js
  const activity = activityLine();
  log.append(activity.el);
```

- [x] **Step 4: Styler la ligne**

Dans `harness/web/style.css` :

```css
.activity {
  display: flex; align-items: center; gap: 9px;
  color: var(--accent); font-size: 13px; padding: 4px 2px;
}
.activity-label { font-variant-numeric: tabular-nums; }
.activity:empty { display: none; }
```

- [x] **Step 5: Vérifier la suite**

Run: `py -3 -m pytest harness/tests -q -m "not browser"`
Expected: PASS. Les tests de `test_factory_web.py` qui lisent `app.js` doivent rester verts ; si l'un d'eux cherche `think.open`, le corriger dans le même commit et dire lequel.

- [x] **Step 6: Commit**

```bash
git add harness/web/app.js harness/web/index.html harness/web/style.css
git commit -F - <<'EOF'
feat(studio): one live activity line instead of open thoughts

Thinking was force-opened on every delta (app.js:851) and every tool dropped
a blob in the log: that is the "far too much pollution in the conversation"
of the backlog, verbatim. Now one line says what is happening -- icon, verb,
target -- and the detail stays one click away.

At most ten paints a second, with a forced paint at the end of the turn: per
delta rendering is the 11.5 ms/token cost we already paid for once.
EOF
```

---

### Task 6: Le récapitulatif au rechargement

**Files:**
- Modify: `harness/web/app.js:1145-1185` (la reconstruction du log dans `renderChat`)
- Modify: `harness/web/style.css` (styles `.recap`)
- Create: `harness/tests/test_chat_recap_e2e.py` (marqué `browser`, hors gate)

**Interfaces:**
- Consumes: `groupTurns`, `formatRecap` (tâche 4), `icon()` (tâche 3).
- Produces: rien pour la suite.

- [x] **Step 1: Rendre les tours repliés**

Dans `renderChat`, branche `session.kind === "agent"` (`harness/web/app.js:1145-1160`), remplacer la boucle plate par un rendu par tour :

```js
      log = h("div", { class: "chat-log" });
      for (const turn of groupTurns(stored)) {
        if (turn.lead) log.append(chatBubble("user", turn.lead.content,
          undefined, turn.lead.ts).bubble);
        const recapText = formatRecap(turn);
        // Never folded: replies, approvals and errors. Folding hides
        // chatter, never something that asks the operator for a decision.
        const folded = [], plain = [];
        for (const m of turn.items) {
          if (m.role === "assistant" && !m.content && !m.thinking) continue;
          (m.role === "tool" && !m.error ? folded : plain).push(m);
        }
        if (recapText && folded.length) {
          const det = h("details", { class: "recap" },
            h("summary", {},
              icon("chevron"), h("span", { text: recapText })));
          for (const m of folded) {
            det.append(toolChip(m.tool_name || "tool", null, m.content, m.ts));
          }
          log.append(det);
        }
        for (const m of plain) {
          const b = chatBubble(m.role, m.content, m.thinking, m.ts);
          setChatMeta(b.meta, m.metrics);
          if (m.error) {
            b.meta.textContent = "erreur : " + m.error;
            b.meta.classList.add("chat-error");
          }
          log.append(b.bubble);
        }
      }
```

- [x] **Step 2: Styler le récapitulatif**

```css
.recap > summary {
  display: flex; align-items: center; gap: 8px; cursor: pointer;
  padding: 6px 11px; border-radius: var(--radius);
  background: var(--panel); border: 1px solid var(--line);
  color: var(--dim); font-size: 12px; font-variant-numeric: tabular-nums;
  list-style: none;
}
.recap > summary::-webkit-details-marker { display: none; }
.recap[open] > summary .ico { transform: rotate(90deg); }
.recap > summary:hover { color: var(--fg); }
```

- [x] **Step 3: Écrire la preuve visuelle (hors gate)**

Créer `harness/tests/test_chat_recap_e2e.py`, marqué `browser` — un test navigateur ne doit jamais bloquer un push (`.githooks/pre-push:15-18`) :

```python
"""Visual proof of the folded turn. Marked browser: out of the push gate.

Run it yourself: py -3 -m pytest harness/tests -m browser
"""
import pytest

pytestmark = pytest.mark.browser


def test_a_finished_turn_shows_one_recap_line(studio_page):
    page = studio_page
    page.goto("http://127.0.0.1:8188/#chat")
    page.wait_for_selector(".chat-log")
    recaps = page.locator(".recap > summary")
    assert recaps.count() >= 1
    assert not page.locator(".recap[open]").count()
    recaps.first.click()
    assert page.locator(".recap[open]").count() == 1
```

Si la fixture `studio_page` n'existe pas dans `harness/tests/conftest.py`, l'ajouter là — le serveur de studio y est déjà démarré par les tests existants ; réutiliser ce mécanisme plutôt que d'en ouvrir un second.

- [x] **Step 4: Vérifier**

Run: `py -3 -m pytest harness/tests -q -m "not browser"`
Expected: PASS, toujours sans perte de test.

Run: `py -3 -m pytest harness/tests -q -m browser`
Expected: PASS — et faire une capture d'écran de la conversation pour Martin.

- [x] **Step 5: Commit**

```bash
git add harness/web/app.js harness/web/style.css harness/tests/test_chat_recap_e2e.py harness/tests/conftest.py
git commit -F - <<'EOF'
feat(studio): a finished turn folds into one recap line

Reloading a session used to redraw every tool blob it ever produced. It now
draws one line per turn -- "4 outils, 18 s, 2 lus, 1 commande" -- that opens
on click.

What is never folded: replies, approval panels and errors. Folding is for
chatter, not for something that asks the operator to decide.
EOF
```

---

## Vérification finale

- [x] `py -3 -m pytest harness/tests -q -m "not browser"` — vert, au moins 1087 tests.
- [x] `py -3 -m pytest harness/tests/test_chat_recap_e2e.py -m browser` — vert,
  lancé à la main, captures pliée/dépliée montrées.
- [ ] `py -3 -m pytest harness/tests -q -m browser` — **rouge, et rouge avant ce
  lot** : 3 fichiers non suivis (`test_chat_layout*.py`, `test_chat_left_dom.py`)
  attendent `.chat-root` sur `/`, dont la vue par défaut est `jobs`
  (`app.js:1465`) ; vérifié en rejouant `test_chat_left_dom.py` sur `a26a3389`,
  même échec. `test_playwright_e2e.py` (suivi) échoue sur un argument du serveur
  MCP Playwright, hors sujet. À trancher à part : réparer ou supprimer.
- [ ] Studio ouvert, réseau coupé : les polices s'affichent, aucun caractère de secours.
- [ ] Une session d'agent réelle relue : une ligne vivante pendant le tour, une ligne récapitulative après, la réflexion accessible en un clic et jamais ouverte d'office.
- [ ] Capture montrée à Martin avant de clore le lot.
