"""Run a spec.json through the production ladder.

`factory_cli delegate` takes the goal as a command-line argument, which a
multi-line contract does not survive intact. A spec written as JSON keeps its
newlines, and -- since today's lesson is that a model never sees its tests --
keeping goal and tests in one reviewable file is the right shape anyway.

    py -3.9 experiments/coder_bench/delegate_spec.py experiments/coder_bench/spec_lessons.json
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harness"))

from factory_config import load_llama_server                        # noqa: E402
from job_store import JobStore                                      # noqa: E402
from llama_server_manager import LlamaServerManager                 # noqa: E402
import loop_job                                                     # noqa: E402

CONFIG = ROOT / "factory.toml"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("--out", default=None)
    ap.add_argument("--start-stage", type=int, default=None,
                    help="1=7b, 2=30b, 3=35b; default: the whole ladder")
    a = ap.parse_args()

    spec = json.loads(Path(a.spec).read_text(encoding="utf-8"))
    if a.start_stage:
        spec["start_stage"] = a.start_stage
    run_dir = Path(a.out).resolve() if a.out else (
        Path(__file__).resolve().parent / "runs" /
        ("delegation-" + time.strftime("%Y%m%d-%H%M%S")))
    run_dir.mkdir(parents=True, exist_ok=True)

    manager = LlamaServerManager(load_llama_server(CONFIG))
    store = JobStore(run_dir)
    job_id = store.create(spec)
    print("job {} -> {}".format(job_id, run_dir), flush=True)
    t0 = time.time()
    try:
        result = loop_job.run_job(job_id, run_dir, CONFIG,
                                  chat_fn=manager.chat,
                                  unload_fn=manager.unload,
                                  gpu_lock_path=ROOT / "jobs" / ".gpu.lock")
    finally:
        manager.shutdown()
    print("\nstatus      : {}".format(result.get("status")))
    print("failure     : {}".format(result.get("failure_reason")))
    print("attempts    : {}".format(len(result.get("attempts") or [])))
    print("final model : {}".format(result.get("final_model")))
    print("regression  : {}".format(result.get("regression_passed")))
    print("review      : {}".format((result.get("review") or {}).get("verdict")))
    print("seconds     : {}".format(round(time.time() - t0)))
    print("diff bytes  : {}".format(len(result.get("diff") or "")))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
