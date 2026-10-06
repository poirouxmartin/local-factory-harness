"""Loop Engineering runner (with escalation ladder).

Drives one task through edit -> test -> observe cycles against a local model:

  1. Load governance (agent.md) + a persona as the system prompt.
  2. Send the task goal + current target file + last pytest output.
  3. Extract the corrected file from the reply, write it, run pytest.
  4. Repeat until green, or the stage's attempts run out, or the model PLATEAUS
     (same number of failing tests N attempts running) -- then ESCALATE to the
     next ladder stage (bigger model / higher temperature) instead of retrying
     the same stuck model. Proven necessary by the expr benchmark (ADR-009).
  5. Restore the seeded bug via git so the task stays reusable.

Governance (agent.md) is injected as the system prompt on every call.

Usage:
    py -3 harness/loop.py expr --model qwen3-coder:30b --save
    py -3 harness/loop.py expr --ladder --save      # 7b -> 14b -> 30b auto-escalate
"""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from factory_config import load_llama_server
from llama_server_manager import LlamaServerManager

ROOT = Path(__file__).resolve().parent.parent  # local-factory repo root

# agent.md rule 2 forbids these; we count emissions as a compliance metric.
STUB_PATTERNS = [r"^\s*pass\s*$", r"\bTODO\b", r"\bFIXME\b", r"NotImplementedError"]

# Default escalation ladder: cheap+fast first, escalate to bigger/hotter on plateau.
# The top rung is a thinking model: near-greedy sampling makes it loop, so it runs
# at Qwen's precise-coding preset (0.6) rather than the 0.1 used by the others.
DEFAULT_LADDER = [
    {"model": "qwen2.5-coder:7b", "temperature": 0.1, "attempts": 2},
    {"model": "qwen2.5-coder:14b", "temperature": 0.3, "attempts": 2},
    {"model": "qwen3-coder:30b", "temperature": 0.7, "attempts": 3},
    {"model": "qwen3.6:35b", "temperature": 0.6, "attempts": 3},
]


def load_governance():
    return (ROOT / "agent.md").read_text(encoding="utf-8")


def load_persona(name):
    return (ROOT / "personas" / f"{name}.md").read_text(encoding="utf-8")


def extract_code(text):
    """Return the largest fenced python block, or None."""
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, re.DOTALL)
    if not blocks:
        return None
    return max(blocks, key=len).strip() + "\n"


def count_stub_violations(code):
    return sum(len(re.findall(p, code, re.MULTILINE)) for p in STUB_PATTERNS)


def run_tests(task_dir, test_args):
    cmd = [sys.executable, "-m", "pytest", *test_args]
    p = subprocess.run(cmd, cwd=str(task_dir), capture_output=True, text=True)
    out = (p.stdout or "") + "\n" + (p.stderr or "")
    passed = p.returncode == 0
    failed = int(m.group(1)) if (m := re.search(r"(\d+) failed", out)) else 0
    npass = int(m.group(1)) if (m := re.search(r"(\d+) passed", out)) else 0
    return passed, npass, failed, out, p.returncode


def git_restore(relpath):
    subprocess.run(["git", "-C", str(ROOT), "checkout", "--", relpath],
                   capture_output=True, text=True)


def build_prompt(goal, target, current, last_output):
    parts = [
        f"TASK: {goal}",
        "",
        f"Fix the file `{target}`. Return ONLY the complete corrected contents "
        f"of `{target}` in a single ```python code block. No explanation.",
        "",
        f"Current `{target}`:",
        "```python",
        current.strip(),
        "```",
    ]
    if last_output:
        parts += ["", "The latest pytest run FAILED. Fix these failures:",
                  "```", last_output.strip(), "```"]
    return "\n".join(parts)


def run_task(task_name, model=None, persona="coder", temperature=0.1,
             num_ctx=16384, max_attempts=None, ladder=None, plateau_k=2,
             chat_fn=None):
    task_dir = ROOT / "tasks" / task_name
    cfg = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    target = cfg["target_file"]
    test_args = cfg.get("test_args", ["-q"])
    goal = cfg.get("goal", "")
    target_path = task_dir / target
    rel_target = str(target_path.relative_to(ROOT)).replace("\\", "/")

    if ladder is None:
        stages = [{"model": model or "qwen2.5-coder:7b", "temperature": temperature,
                   "attempts": max_attempts or cfg.get("max_attempts", 5)}]
    else:
        stages = ladder

    system = load_governance() + "\n\n---\n\n" + load_persona(persona)
    attempts = []
    last_output = ""
    success = False
    stages_used = 0
    # Same guard as loop_job: an abnormal pytest exit is model-invariant, so
    # the identical signature repeated aborts the run instead of escalating
    # (j_bc119315: 10 attempts / 688 s on one collection error).
    struct_hist, structural_abort = [], False
    t_start = time.time()

    manager = None
    if chat_fn is None:
        # Zero-Ollama (2026-07-18): the bench rides the llama-server runtime;
        # ladder model names must be [llama_server.models] entries.
        ls_cfg = load_llama_server(ROOT / "factory.toml")
        if ls_cfg is None:
            raise SystemExit("factory.toml has no [llama_server] section; "
                             "the Ollama runtime was retired")
        manager = LlamaServerManager(ls_cfg)
        chat_fn = manager.chat

    try:
        for si, stage in enumerate(stages):
            stages_used += 1
            if si > 0:
                # Clean slate on escalation: don't make the stronger model inherit the
                # weaker one's broken file / failure log -- it does worse. (ADR-009 refined.)
                git_restore(rel_target)
                last_output = ""
            smodel = stage["model"]
            stemp = stage.get("temperature", temperature)
            sattempts = stage.get("attempts", 3)
            fail_hist = []
            for _ in range(sattempts):
                current = target_path.read_text(encoding="utf-8")
                user = build_prompt(goal, target, current, last_output)
                t_call = time.time()
                resp = chat_fn(smodel, system, user, temperature=stemp, num_ctx=num_ctx,
                               options={"num_predict": 8192})
                elapsed = time.time() - t_call
                code = extract_code(resp["content"])
                rec = {"attempt": len(attempts) + 1, "stage": si + 1, "model": smodel,
                       "temperature": stemp, "tokens_per_s": round(resp.get("tokens_per_s", 0.0), 1),
                       "prefill_tokens_per_s": round(resp.get("prefill_tokens_per_s", 0.0), 1),
                       "ttft_s": round(resp.get("ttft_s", 0.0), 2),
                       "gen_seconds": round(elapsed, 1)}
                if not code:
                    rec.update({"result": "no_code_block", "passed": False})
                    attempts.append(rec)
                    last_output = ("Your previous reply had no python code block. "
                                   "Return the FULL corrected file in one ```python code block.")
                    continue
                stubs = count_stub_violations(code)
                target_path.write_text(code, encoding="utf-8")
                passed, npass, failed, out, rc = run_tests(task_dir, test_args)
                rec.update({"passed": passed, "tests_passed": npass,
                            "tests_failed": failed, "stub_violations": stubs})
                attempts.append(rec)
                last_output = out[-3000:]
                if passed:
                    success = True
                    break
                if rc in (0, 1):
                    fail_hist.append(failed)
                    struct_hist = []
                else:
                    sig = re.sub(r"\s+", " ", re.sub(r"\d+\.\d+s?", "", out[-800:])).strip()
                    struct_hist.append("{}|{}".format(rc, sig))
                    if len(struct_hist) >= 3 and len(set(struct_hist[-3:])) == 1:
                        rec["structural_abort"] = True
                        structural_abort = True
                        break
                # Plateau: same failing count for plateau_k attempts -> escalate stage.
                if len(fail_hist) >= plateau_k and len(set(fail_hist[-plateau_k:])) == 1:
                    rec["plateau"] = True
                    break
            if success or structural_abort:
                break
    finally:
        if manager is not None:
            # Windows children survive their parent: an unclean exit leaks a
            # 20 GB server otherwise.
            manager.shutdown()

    total = round(time.time() - t_start, 1)
    git_restore(rel_target)  # reset seeded bug -> task reusable
    return {
        "task": task_name,
        "ladder": [s["model"] for s in stages],
        "persona": persona,
        "num_ctx": num_ctx,
        "success": success,
        "final_model": attempts[-1]["model"] if attempts else None,
        "escalated": stages_used > 1,
        "structural_abort": structural_abort,
        "stages_used": stages_used,
        "attempts_used": len(attempts),
        "total_seconds": total,
        "attempts": attempts,
    }


def _cli():
    ap = argparse.ArgumentParser(description="Run one Loop Engineering task.")
    ap.add_argument("task")
    ap.add_argument("--model", default="qwen2.5-coder:7b")
    ap.add_argument("--persona", default="coder")
    ap.add_argument("--temp", type=float, default=0.1)
    ap.add_argument("--num-ctx", type=int, default=16384)
    ap.add_argument("--max-attempts", type=int, default=None)
    ap.add_argument("--ladder", action="store_true",
                    help="use the default 7b->14b->30b escalation ladder")
    ap.add_argument("--plateau-k", type=int, default=2,
                    help="consecutive equal-failure attempts that trigger escalation")
    ap.add_argument("--save", action="store_true")
    a = ap.parse_args()

    ladder = DEFAULT_LADDER if a.ladder else None
    res = run_task(a.task, a.model, a.persona, a.temp, a.num_ctx,
                   a.max_attempts, ladder=ladder, plateau_k=a.plateau_k)
    print(json.dumps(res, indent=2))
    if a.save:
        ts = time.strftime("%Y%m%d-%H%M%S")
        tag = "ladder" if a.ladder else a.model.replace(":", "_").replace("/", "_")
        out = ROOT / "experiments" / "results" / f"{ts}_{a.task}_{tag}.json"
        out.write_text(json.dumps(res, indent=2), encoding="utf-8")
        print("saved:", out)
    sys.exit(0 if res["success"] else 1)


if __name__ == "__main__":
    _cli()
