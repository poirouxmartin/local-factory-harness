"""A/B the agent.md content on real turns.

The item: "shorter, imperative, examples of good/bad tool calls." The claim to
test is that variant B lowers wasted and failed tool calls WITHOUT losing task
completion -- not that a shorter prompt is prettier.

Each task runs in a fresh git workspace through the real Factory.agent_reply
(real llama-server, real tools), once per variant, N times, models fixed. We
swap only factory_mcp.GLOBAL_AGENT_MD between variants -- everything else, down
to the workspace bytes, is identical.

Scored per (task, variant, run):
  - tool_calls      how many tool calls the turn made
  - tool_errors     tool outputs starting with "error:" (a wasted round-trip)
  - edit_misses     edit_file calls that returned "0 matches" (the target bug)
  - ok              the task's own assertion on the final workspace state
  - eval_tokens     generated tokens (cost)
  - seconds

Long GPU run -> launch detached (Start-Process), never via a task runner.
"""
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path


def _rmtree(path):
    # git packs its objects read-only; shutil.rmtree chokes on them on Windows,
    # which left a half-deleted workspace and a FileExistsError on the next run.
    def clear(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if path.exists():
        shutil.rmtree(path, onerror=clear)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "harness"))

import factory_mcp  # noqa: E402
import workspace_memory  # noqa: E402

MODEL = os.environ.get("BENCH_MODEL", "qwen3-coder:30b")
RUNS = int(os.environ.get("BENCH_RUNS", "5"))
ONLY = os.environ.get("BENCH_ONLY")  # substring filter on task name, for smoke
VARIANTS = {"A": HERE / "a_current.md", "B": HERE / "b_short.md"}
if os.environ.get("BENCH_VARIANT"):
    VARIANTS = {k: v for k, v in VARIANTS.items()
                if k == os.environ["BENCH_VARIANT"]}
WORK = HERE / "_work"


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
]


def score_events(events):
    calls = [p for k, p in events if k == "tool_call"]
    results = [p for k, p in events if k == "tool_result"]
    # A tool that refused says "error:"; a shell command that failed says it in
    # its exit code, and that round-trip is just as wasted. Counting only the
    # first hid every failed run_command of pass 1 -- `workspace_memory.failed`
    # is the rule that already gets this right.
    tool_errors = sum(1 for r in results
                      if workspace_memory.failed(r.get("output")))
    edit_misses = sum(1 for r in results
                      if "0 matches" in (r.get("output") or ""))
    # eval_count on the final done is the last model call's, not the turn's
    # total; sum every model call's tokens instead (each final-answer/tool
    # cycle carries one). Robust to the stopped-turn payloads that lack it.
    done_payloads = [p for k, p in events if k == "done"]
    eval_tokens = sum(p.get("eval_count") or 0 for p in done_payloads) or None
    return {
        "tool_calls": len(calls),
        "tool_errors": tool_errors,
        "edit_misses": edit_misses,
        "eval_tokens": eval_tokens,
        "stopped": (done_payloads[-1].get("stopped") if done_payloads else None),
        "call_names": [c.get("name") for c in calls],
    }


def run_one(factory, task, ws):
    seed(ws, task["files"])
    sid = factory.chat_create(MODEL, kind="agent", mode="auto",
                              workspace=str(ws))["session_id"]
    t0 = time.time()
    events = list(factory.agent_reply(sid, task["goal"]))
    dt = time.time() - t0
    s = score_events(events)
    s["seconds"] = round(dt, 1)
    try:
        s["ok"] = bool(task["ok"](ws))
    except Exception as e:
        s["ok"] = False
        s["ok_error"] = str(e)
    s["session"] = sid
    return s


def main():
    cfg = ROOT / "factory.toml"
    factory = factory_mcp.Factory(ROOT / "jobs", cfg)
    out = HERE / "results.jsonl"
    fh = io.open(out, "w", encoding="utf-8")
    log = io.open(HERE / "bench.log", "w", encoding="utf-8")

    def say(m):
        print(m); log.write(m + "\n"); log.flush()

    say("bench start {}  model={}  runs={}".format(
        time.strftime("%H:%M:%S"), MODEL, RUNS))
    rows = []
    for variant, path in VARIANTS.items():
        factory_mcp.GLOBAL_AGENT_MD = path  # the only thing that changes
        for task in TASKS:
            if ONLY and ONLY not in task["name"]:
                continue
            for run in range(RUNS):
                tag = "{}/{}/{}".format(variant, task["name"], run)
                say("  {} ...".format(tag))
                try:
                    s = run_one(factory, task, WORK / task["name"])
                except Exception as e:
                    s = {"error": repr(e)}
                s.update(variant=variant, task=task["name"], run=run)
                rows.append(s)
                fh.write(json.dumps(s) + "\n"); fh.flush()
                say("    ok={ok} calls={tool_calls} err={tool_errors} "
                    "miss={edit_misses} tok={eval_tokens} {seconds}s".format(
                        ok=s.get("ok"), tool_calls=s.get("tool_calls"),
                        tool_errors=s.get("tool_errors"),
                        edit_misses=s.get("edit_misses"),
                        eval_tokens=s.get("eval_tokens"), seconds=s.get("seconds")))
    factory._release_runtime()
    for line in summarize(rows):
        say(line)
    say("bench done {}".format(time.strftime("%H:%M:%S")))
    fh.close(); log.close()


def summarize(rows):
    """Per-variant totals, then the per-task split -- the log is the report."""
    def agg(sub):
        n = len(sub) or 1
        num = lambda k: sum(r.get(k) or 0 for r in sub)  # noqa: E731
        return ("ok {}/{}  calls {:.1f}  err {}  miss {}  tok {:.0f}  "
                "{:.1f}s".format(sum(1 for r in sub if r.get("ok")), len(sub),
                                 num("tool_calls") / n, num("tool_errors"),
                                 num("edit_misses"), num("eval_tokens") / n,
                                 num("seconds") / n))

    out = ["", "--- summary (means per run, err/miss are totals) ---"]
    variants = sorted({r.get("variant") for r in rows})
    for v in variants:
        out.append("{}  {}".format(v, agg([r for r in rows if r.get("variant") == v])))
    for task in [t["name"] for t in TASKS]:
        sub = [r for r in rows if r.get("task") == task]
        if not sub:
            continue
        out.append("  " + task)
        for v in variants:
            out.append("    {}  {}".format(
                v, agg([r for r in sub if r.get("variant") == v])))
    return out


if __name__ == "__main__":
    main()
