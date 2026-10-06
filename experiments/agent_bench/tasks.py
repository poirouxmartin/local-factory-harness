"""The corpus: what the bench asks an agent to do, and how a run is judged.

A task seeds a byte-identical git workspace, states a goal in the operator's
language, and asserts on the resulting workspace by RUNNING the code, never by
matching literal text. That rule is not style: pass 1 of the 2026-07-23 A/B
marked two correct runs failed because the model rewrote function-style tests
as a TestCase -- the right answer, different bytes, and the check was scoring
the test's shape.
"""
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path


def _rmtree(path):
    # git packs its objects read-only; shutil.rmtree chokes on them on Windows,
    # which left a half-deleted workspace and a FileExistsError on the next run.
    def clear(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if path.exists():
        shutil.rmtree(path, onerror=clear)


def git(ws, *a):
    subprocess.run(["git", "-C", str(ws), *a], capture_output=True, check=True)


def seed(ws, files):
    _rmtree(ws)
    ws.mkdir(parents=True)
    for name, text in files.items():
        p = ws / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    git(ws, "init", "-q", "-b", "main")
    git(ws, "config", "user.email", "b@b.b")
    git(ws, "config", "user.name", "bench")
    git(ws, "add", "-A")
    git(ws, "commit", "-qm", "seed")


def read(ws, name):
    return (ws / name).read_text(encoding="utf-8")


# The smoke pass (2026-07-22, RUNS=2) came back err=0 miss=0 on every run of
# the three tasks below: they are too easy to separate the variants, since B's
# whole thesis is about wasted and failed tool calls. The four tasks after them
# are built so those failure modes CAN happen -- text that punishes editing from
# memory, an anchor that matches twice, a symbol that must be found across a
# tree, and a file full of bait for scope creep.
SERVICE_PY = (
    '"""Connecteur reseau."""\n'
    "\n"
    "\n"
    "def check_status(client,  timeout=5):\n"          # two spaces, on purpose
    '    """Verifie l\'etat du connecteur (acces reseau requis)."""\n'
    "    return client.ping(timeout=timeout)\n"
)

ROUTES_PY = (
    'def get_user(id):\n'
    '    log("hit")\n'
    '    return db.find("users", id)\n'
    "\n"
    "\n"
    'def get_item(id):\n'
    '    log("hit")\n'
    '    return db.find("items", id)\n'
)

LEGACY_PY = (
    "import os\n"
    "import json\n"
    "\n"
    "TVA = 0.196\n"
    "\n"
    "def prix_ttc( ht ):\n"
    "    return ht * ( 1 + TVA )\n"
    "\n"
    "def charge(path):\n"
    "    try:\n"
    "        return json.loads(open(path).read())\n"
    "    except:\n"
    "        return {}\n"
)

RETRY_PY = (
    "MAX_RETRIES = 3\n"
    "BACKOFF = 0.5\n"
    "\n"
    "\n"
    "def should_retry(n):\n"
    "    return n < MAX_RETRIES\n"
)


CLIENT_PY = (
    "from core.net.retry import should_retry\n"
    "\n"
    "\n"
    "def call(fn):\n"
    "    n = 0\n"
    "    while should_retry(n):\n"
    "        n += 1\n"
    "    return n\n"
)


def unchanged(ws, name, text):
    return read(ws, name) == text


def eval_ok(ws, snippet):
    """Run a snippet against the produced workspace: does the code DO the job.
    Uses this interpreter, not the agent's `python` (see below)."""
    r = subprocess.run([sys.executable, "-c", snippet], cwd=str(ws),
                       capture_output=True)
    return r.returncode == 0


def _chain_files(n=18, secret="Pluton42"):
    """A read-gated chain: file n00's first line names the next file, that one
    names the next, in a SHUFFLED order (so the file-name order is not the
    chain), and the terminal file holds the secret. There is no greppable
    marker that reveals the path, nothing to edit, and no shell shortcut on the
    `noshell` toolset -- so the only way to the secret is to READ each file in
    full. That is what forces the ~14 near-max tool outputs a session needs to
    cross the 64k window's compaction threshold, which three transform tasks
    could not (the 30b scripted, quit early, or blind-edited; 2026-07-23)."""
    import random
    rest = list(range(1, n))
    random.Random(1234).shuffle(rest)
    order = [0] + rest                       # the chain always starts at n00
    files = {}
    for pos, idx in enumerate(order):
        lines = _filler("c{:02d}".format(idx)).split("\n")
        lines[0] = ("PROCHAIN FICHIER: n{:02d}".format(order[pos + 1])
                    if pos + 1 < len(order) else "FIN MOT: {}".format(secret))
        files["chain/n{:02d}.txt".format(idx)] = "\n".join(lines)
    return files


def _filler(tag, n_lines=120):
    """~60 chars/line so a file lands near 7200 chars -- under read_file's
    8000-char output cap (`agent_tools.MAX_OUTPUT`), so one read returns the
    file whole. That whole output is what piles up in the raw history, and
    compaction is measured against the raw history un-evicted (chat_context,
    2026-07-21 fix). Fourteen such files, read and rewritten, cross the 70 %
    threshold of the 64k window (~40k tokens) -- which the ~2.7k-token
    long_refactor_chain never does."""
    return "".join(
        "{} ligne {:03d} remplissage realiste pour gonfler ce fichier\n"
        .format(tag, i) for i in range(n_lines))


# --- tasks: each needs read -> edit -> (the model) verify; small on purpose ---
TASKS = [
    {
        "name": "rename_and_caller",
        "files": {
            "calc.py": "def add(a, b):\n    return a + b\n",
            "main.py": "from calc import add\n\nprint(add(2, 3))\n",
        },
        "goal": ("Renomme la fonction `add` en `somme` dans calc.py, puis mets "
                 "à jour son seul appelant dans main.py. Ne change rien d'autre."),
        "ok": lambda ws: ("def somme(" in read(ws, "calc.py")
                          and "add(" not in read(ws, "main.py")
                          and "somme(2, 3)" in read(ws, "main.py")),
    },
    {
        "name": "fix_exact_value",
        "files": {
            "config.py": ("HOST = \"127.0.0.1\"\n# le port de prod\nPORT = 8080\n"
                          "TIMEOUT = 30\n"),
        },
        "goal": ("Dans config.py le PORT vaut 8080 mais doit être 9090. "
                 "Corrige uniquement cette valeur."),
        "ok": lambda ws: ("PORT = 9090" in read(ws, "config.py")
                          and "8080" not in read(ws, "config.py")
                          and "TIMEOUT = 30" in read(ws, "config.py")),
    },
    {
        "name": "add_function_and_test",
        "files": {
            "mathx.py": "def mul(a, b):\n    return a * b\n",
            "test_mathx.py": "from mathx import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n",
        },
        "goal": ("Ajoute à mathx.py une fonction `puissance(base, exp)` qui "
                 "retourne base**exp, et ajoute un test dans test_mathx.py qui "
                 "vérifie puissance(2, 3) == 8."),
        # Scored on behaviour, not on the test's shape. The first pass asked
        # for the literal `puissance(2, 3)` and marked two correct runs failed:
        # `python` on this box is a 3.12 without pytest, so the agent fell back
        # to unittest, got "Ran 0 tests" on function-style tests, and rewrote
        # the file as a TestCase -- right answer, different bytes.
        "ok": lambda ws: (
            "def puissance(" in read(ws, "mathx.py")
            and eval_ok(ws, "from mathx import puissance; "
                            "assert puissance(2, 3) == 8")
            and "puissance" in read(ws, "test_mathx.py")
            and "8" in read(ws, "test_mathx.py")),
    },
    {
        # The signature carries a double space: an edit written from memory
        # cannot match it. Reading first is the only way through.
        "name": "edit_from_memory",
        "files": {"service.py": SERVICE_PY},
        "goal": ("Dans service.py, le timeout par defaut de check_status doit "
                 "passer de 5 a 15. Ne change que cette valeur."),
        "ok": lambda ws: unchanged(
            ws, "service.py", SERVICE_PY.replace("timeout=5", "timeout=15")),
    },
    {
        # `log("hit")` appears twice, so the naive anchor is refused with
        # "2 matches"; the edit needs surrounding lines.
        "name": "duplicate_anchor",
        "files": {"routes.py": ROUTES_PY},
        "goal": ('Dans routes.py, get_item doit logger log("item hit") au lieu '
                 'de log("hit"). get_user ne change pas.'),
        "ok": lambda ws: unchanged(
            ws, "routes.py",
            ROUTES_PY.replace('    log("hit")\n    return db.find("items"',
                              '    log("item hit")\n    return db.find("items"')),
    },
    {
        # The symbol has to be found across a tree. On Windows a shell `grep`
        # fails outright, so reaching for it instead of search() is scored.
        "name": "find_the_constant",
        "files": {
            "README.md": "# demo\n\nUn petit service.\n",
            "app/main.py": "from core.net.client import call\n\nprint(call(None))\n",
            "core/__init__.py": "",
            "core/net/__init__.py": "",
            "core/net/retry.py": RETRY_PY,
            "core/net/client.py": CLIENT_PY,
            "docs/notes.md": "Le nombre de tentatives est defini dans core/net.\n",
        },
        "goal": ("Le nombre maximum de tentatives vaut 3 quelque part dans ce "
                 "projet; passe-le a 7. Ne modifie que sa definition."),
        "ok": lambda ws: (
            unchanged(ws, "core/net/retry.py",
                      RETRY_PY.replace("MAX_RETRIES = 3", "MAX_RETRIES = 7"))
            and unchanged(ws, "core/net/client.py", CLIENT_PY)),
    },
    {
        # Bait for scope creep: an unused import, spaced parens, a bare except.
        # Both variants forbid touching them; only the file's bytes can tell.
        "name": "no_scope_creep",
        "files": {"legacy.py": LEGACY_PY},
        "goal": ("Dans legacy.py, le taux de TVA passe de 0.196 a 0.20. "
                 "Rien d'autre ne doit changer."),
        # 0.2 and 0.20 are the same rate: normalize the line back and compare
        # the rest byte for byte, or the check would score formatting, not scope.
        "ok": lambda ws: read(ws, "legacy.py").replace(
            "TVA = 0.20\n", "TVA = 0.196\n").replace(
            "TVA = 0.2\n", "TVA = 0.196\n") == LEGACY_PY
        and "TVA = 0.196" not in read(ws, "legacy.py"),
    },
    {
        "name": "long_refactor_chain",
        # Eight modules, one shared helper: the agent must read most of them
        # before it can safely rename. Enough round-trips, and enough tool
        # output, to cross the 70 % compaction threshold.
        "files": dict(
            {"helper.py": "def fmt(x):\n    return str(x)\n"},
            **{"mod{}.py".format(i):
               "from helper import fmt\n\n\ndef show{}(v):\n"
               "    return fmt(v) + ' #{}'\n".format(i, i)
               for i in range(8)}),
        "goal": ("Renomme la fonction `fmt` de helper.py en `format_value`, "
                 "puis mets a jour TOUS ses appelants dans les modules mod0 a "
                 "mod7. Ne change rien d'autre."),
        "ok": lambda ws: (
            "def format_value(" in read(ws, "helper.py")
            and all("format_value(v)" in read(ws, "mod{}.py".format(i))
                    and "fmt(" not in read(ws, "mod{}.py".format(i))
                    for i in range(8))),
    },
    {
        "name": "long_survey",
        # Twelve files, one planted constant. The agent must search and read
        # widely; the tool OUTPUT is the bulk here, which is exactly what
        # eviction stubs.
        "files": dict(
            {"notes/target.py": "# la valeur de prod\nSEUIL = 4271\n"},
            **{"notes/f{}.py".format(i):
               "# fichier {}\nVALEUR_{} = {}\n".format(i, i, 1000 + i) * 12
               for i in range(12)}),
        "goal": ("Trouve dans notes/ la constante SEUIL, puis ecris dans "
                 "resume.md une ligne `SEUIL=<valeur>` avec sa valeur reelle."),
        "ok": lambda ws: "SEUIL=4271" in read(ws, "resume.md"),
    },
    {
        # HUGE: the agent must READ each file in full (~7.2k chars, one read
        # since it is under the 8000-char output cap) and WRITE an EXPANDED
        # copy. The write carries the whole new file in its arguments, so an
        # expand-on-write triples the raw tokens each file adds. A 30b quits
        # this kind of bulk work after ~5 files (measured 07-23), so the task
        # is built to cross the 64k window's ~40k-token compaction threshold
        # WITHIN those five -- three reads+writes of a tripled file suffice.
        # Run on the `noshell` toolset: with run_command available the model
        # scripts the whole thing in one PowerShell call and nothing piles up.
        # Completion is secondary; these exist so line items 4 and 5 have a
        # session that actually triggers compaction and eviction.
        "name": "huge_triplicate",
        "files": {"doc/d{:02d}.txt".format(i): _filler("d{:02d}".format(i))
                  for i in range(14)},
        "goal": ("Pour chaque fichier doc/d*.txt, cree doc/out/d*.txt (meme "
                 "nom) dont le contenu est celui du fichier source REPETE 3 "
                 "fois de suite. Traite les fichiers un par un."),
        # Lenient: each output exists and is ~3x the source. A partial run is a
        # valid measurement of compaction; it just is not a completion.
        "ok": lambda ws: all(
            read(ws, "doc/out/d{:02d}.txt".format(i)).count("remplissage") >= 300
            for i in range(14)),
    },
    {
        # HUGE, aggregation shape (vs the per-file shape above), so the two
        # arms of the compaction/eviction A/B are not one idiom. Concatenating
        # every file into one forces reading all of them; the single growing
        # output file is written incrementally, and each append carries its
        # payload in the arguments.
        "name": "huge_concat",
        "files": {"data/v{:02d}.txt".format(i): _filler("v{:02d}".format(i))
                  for i in range(14)},
        "goal": ("Ecris combined.txt : la concatenation du contenu de TOUS "
                 "les fichiers data/v*.txt, dans l'ordre de v00 a v13."),
        "ok": lambda ws: all(
            "v{:02d} ligne".format(i) in read(ws, "combined.txt")
            for i in range(14)),
    },
    {
        # HUGE, read-gated: the one shape the 30b cannot shortcut. See
        # _chain_files. Follow the chain far enough and the raw history crosses
        # the compaction threshold; this is the task built to make line items
        # 4 and 5 reachable. Run on `noshell`.
        "name": "huge_chain",
        "files": _chain_files(),
        "goal": ("Commence par chain/n00.txt. La PREMIERE ligne de chaque "
                 "fichier indique 'PROCHAIN FICHIER: nXX' : ouvre ce fichier, "
                 "et continue de fichier en fichier. Quand tu atteins un "
                 "fichier dont la premiere ligne est 'FIN MOT: <mot>', ecris "
                 "ce <mot> dans reponse.txt."),
        "ok": lambda ws: "Pluton42" in read(ws, "reponse.txt"),
    },
]
