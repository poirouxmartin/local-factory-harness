"""TTFT / --cache-reuse A/B -- the last unmeasured claim of the llama-server
migration (ROADMAP "Prompt-cache stability").

Two questions, and the second is the one that costs us something today:

 1. Does `--cache-reuse 256` buy any TTFT at all?
 2. Is the byte-stable prefix rule worth its price? Lessons (`0a277d6`) and
    workspace MEMORY.md (`8121bac`) both snapshot their block ONCE per session
    purely to keep the system prompt byte-identical across turns. If a mutated
    prefix costs nothing, that constraint is paid for nothing.

So: 2 server configs (cache-reuse on/off) x 2 prompt regimes (stable prefix vs
a system block that changes every turn) x N turns, measuring what llama-server
itself reports (`timings.prompt_n`, `timings.prompt_ms`).

Chat-lane settings on purpose (ctx 64k, KV q4), because that is the lane the
byte-stability rule protects.

Run DETACHED (CLAUDE.md): this loads a 17 GB model twice.
    py -3.9 experiments/ttft_cache_reuse_bench.py --out experiments/results
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# harness modules import each other flat (`from repetition_guard import ...`),
# so the package dir itself goes on the path, not the repo root.
sys.path.insert(0, str(ROOT / "harness"))

import llama_client                            # noqa: E402
import llama_server_manager as lsm             # noqa: E402
from factory_config import load_llama_server   # noqa: E402

MODEL = "qwen3-coder:30b"
CTX = 65536
KV = "q4_0"
TURNS = 5

# ~2.5k tokens of stable prefix: the shape of a real agent system prompt once
# agent.md, the workspace instruction file, lessons and MEMORY.md are in it.
PREFIX_FILLER = ("You are a coding agent working inside a workspace. "
                 "Prefer the built-in tools over shell commands. "
                 "Never claim you cannot modify files. ") * 120


def _system(turn, mutate):
    """The system prompt for this turn. With `mutate`, a counter changes near
    the TOP of the block -- the worst case, and exactly what an un-snapshotted
    lessons block would do: every later token's KV is invalidated."""
    head = "Session note {}: lessons updated.\n".format(turn) if mutate else ""
    return head + PREFIX_FILLER


def _run_regime(url, mutate, turns):
    """One conversation, `turns` user messages, history resent every turn."""
    rows = []
    history = []
    for t in range(turns):
        history.append("Question {}: name one Python stdlib module.".format(t))
        user = "\n".join(history)
        t0 = time.time()
        out = llama_client.chat(url, _system(t, mutate), user,
                                temperature=0.1, num_ctx=CTX,
                                options={"num_predict": 16})
        rows.append({
            "turn": t,
            "prompt_n": out.get("prefill_count", 0),
            "prefill_tok_s": round(out.get("prefill_tokens_per_s", 0.0), 1),
            "ttft_s": round(out.get("ttft_s", 0.0), 3),
            "wall_s": round(time.time() - t0, 2),
        })
        print("   turn {} ttft {:.3f}s prefill {:.0f} tok/s".format(
            t, rows[-1]["ttft_s"], rows[-1]["prefill_tok_s"]), flush=True)
    return rows


def _serve(cfg, cache_reuse):
    """A manager whose argv we control: cache-reuse is hard-coded in ensure()."""
    mgr = lsm.LlamaServerManager(cfg, kv_cache_type=KV, ctx=CTX)
    if not cache_reuse:
        real_spawn = mgr.spawn_fn

        def spawn_without_flag(argv):
            argv = list(argv)
            i = argv.index("--cache-reuse")
            del argv[i:i + 2]
            return real_spawn(argv)
        mgr.spawn_fn = spawn_without_flag
    return mgr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "experiments" / "results"))
    ap.add_argument("--turns", type=int, default=TURNS)
    args = ap.parse_args()

    cfg = load_llama_server(ROOT / "factory.toml")
    results = {}
    for cache_reuse in (True, False):
        arm = "cache_reuse_{}".format("on" if cache_reuse else "off")
        print("\n=== {} : loading {} ===".format(arm, MODEL), flush=True)
        mgr = _serve(cfg, cache_reuse)
        try:
            url = mgr.ensure(MODEL)
            for mutate in (False, True):
                regime = "mutated_prefix" if mutate else "stable_prefix"
                print(" -- {} / {}".format(arm, regime), flush=True)
                # warm the slot so turn 0 is not paying the cold load
                llama_client.chat(url, _system(0, False), "hello",
                                  temperature=0.1, num_ctx=CTX,
                                  options={"num_predict": 8})
                results["{}|{}".format(arm, regime)] = _run_regime(
                    url, mutate, args.turns)
        finally:
            mgr.shutdown()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "{}_ttft_cache_reuse.json".format(stamp)).write_text(
        json.dumps({"model": MODEL, "ctx": CTX, "kv": KV,
                    "turns": args.turns, "results": results},
                   indent=2), encoding="utf-8")

    lines = ["# TTFT / --cache-reuse A/B -- {}".format(stamp), "",
             "Model {}, ctx {}, KV {}, {} turns, history resent each turn."
             .format(MODEL, CTX, KV, args.turns), "",
             "| arm | regime | turn | prompt_n | prefill tok/s | TTFT s |",
             "|---|---|---|---|---|---|"]
    for key, rows in results.items():
        arm, regime = key.split("|")
        for r in rows:
            lines.append("| {} | {} | {} | {} | {} | {} |".format(
                arm, regime, r["turn"], r["prompt_n"],
                r["prefill_tok_s"], r["ttft_s"]))
    for key, rows in results.items():
        later = [r["ttft_s"] for r in rows[1:]] or [0]
        lines += ["", "**{}**: mean TTFT after turn 0 = {:.3f} s".format(
            key, sum(later) / len(later))]
    (out_dir / "{}_ttft_cache_reuse.md".format(stamp)).write_text(
        "\n".join(lines) + "\n", encoding="utf-8")
    print("\nwrote {}/{}_ttft_cache_reuse.{{json,md}}".format(args.out, stamp))


if __name__ == "__main__":
    main()
