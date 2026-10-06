"""The spec-writer executant: goal in, proposed tests + targets out.

One model call under the GPU lock, then the human takes over: the proposal is
edited in the studio and only becomes a delegation through delegate(), which
re-validates everything. Validation here is therefore advisory -- a flawed
proposal is annotated, never discarded, because the editor may fix it in ten
seconds.

    py -3 harness/draft_job.py j_7f3a --jobs jobs --config factory.toml
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from factory_config import (ConfigError, load_llama_server, load_projects,
                            resolve_project, validate_context_files, validate_targets,
                            validate_tests)
from gpu_lock import GpuLock
from job_store import JobStore, seek_stdout_to_end
from llama_server_manager import LlamaServerManager

ROOT = Path(__file__).resolve().parent.parent
HEARTBEAT_INTERVAL = 30.0

# The strongest rung that answers in interactive time (~30 tok/s): a draft is a
# conversation with the operator, not a batch job.
DRAFT_MODEL = "qwen3-coder:30b"

# Drafts are interactive; nobody edits a proposal six hours after asking for it.
DEFAULT_GPU_TIMEOUT = 900

MAX_LISTED_FILES = 400


def _system_prompt():
    return ((ROOT / "agent.md").read_text(encoding="utf-8") + "\n\n---\n\n"
            + (ROOT / "personas" / "spec-writer.md").read_text(encoding="utf-8"))


def build_draft_prompt(goal, files):
    return "\n".join([
        "GOAL: {}".format(goal), "",
        "Repository files:",
        "\n".join(files), "",
        "Propose a delegation spec for this goal. Reply with exactly ONE JSON "
        "object in a ```json code block, with these keys:",
        '- "tests": map of file name -> pytest source. Names must match '
        "test_*.py. These tests are the judge: they must fail before the "
        "change and pass after.",
        '- "target_files": repo-relative files the coding model may rewrite '
        "or create.",
        '- "context_files": existing repo files worth reading as read-only '
        "reference. Keep them few and small.",
        '- "assumptions": list of assumptions you made where the goal is silent.',
        "No file outside the repository. No explanation outside the JSON.",
    ])


def extract_json(text):
    """The object out of a model reply: fenced block first, braces as fallback."""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = m.group(1) if m else None
    if candidate is None:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            candidate = text[start:end + 1]
    if candidate is None:
        raise ValueError("no JSON object in the reply")
    return json.loads(candidate)


def _normalize(obj):
    if not isinstance(obj, dict):
        raise ValueError("the proposal is not a JSON object")
    tests = obj.get("tests") or {}
    if not isinstance(tests, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in tests.items()):
        raise ValueError("'tests' must map file names to source strings")

    def strlist(key):
        v = obj.get(key) or []
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise ValueError("'{}' must be a list of strings".format(key))
        return v

    return {"tests": tests, "target_files": strlist("target_files"),
            "context_files": strlist("context_files"),
            "assumptions": strlist("assumptions")}


def _advisory_warnings(project, draft):
    """delegate() will enforce these for real; here they only annotate."""
    checks = ((validate_tests, (draft["tests"],)),
              (validate_targets, (draft["target_files"],)),
              (validate_context_files, (project, draft["context_files"],
                                        draft["target_files"])))
    warnings = []
    for fn, args in checks:
        try:
            fn(*args)
        except ConfigError as e:
            warnings.append(str(e))
    return warnings


def _repo_files(path, limit=MAX_LISTED_FILES):
    out = subprocess.run(["git", "-C", str(path), "ls-files"],
                         capture_output=True, text=True, check=True).stdout
    files = out.splitlines()
    if len(files) > limit:
        files = files[:limit] + ["... ({} more files not listed)".format(len(files) - limit)]
    return files


def _heartbeat_forever(store, job_id, interval, stop):
    while not stop.wait(interval):
        try:
            store.heartbeat(job_id)
        except OSError:
            pass


def _execute(store, job_id, jobs_root, config_path, chat_fn, unload_fn, model,
             system, gpu_lock_path, gpu_timeout, num_ctx, temperature):
    spec = store.read_spec(job_id)
    project = resolve_project(load_projects(config_path), spec["project"])

    store.update_state(job_id, status="waiting_gpu", pid=os.getpid())
    lock = GpuLock(gpu_lock_path or Path(jobs_root) / ".gpu.lock")
    if not lock.acquire(timeout=gpu_timeout, poll=2.0,
                        on_wait=lambda: store.heartbeat(job_id)):
        return {"status": "failed", "error": "gpu_timeout",
                "failure_reason": "gpu_timeout",
                "detail": "another job held the GPU for {}s".format(gpu_timeout)}

    try:
        store.update_state(job_id, status="running", model=model)
        prompt = build_draft_prompt(spec.get("goal", ""), _repo_files(project.path))
        resp = chat_fn(model, system, prompt, temperature=temperature, num_ctx=num_ctx)
        try:
            draft = _normalize(extract_json(resp["content"]))
        except (ValueError, TypeError) as e:
            return {"status": "failed", "error": "unparseable proposal: {}".format(e),
                    "raw": resp["content"][:4000], "model": model}
        return {"status": "succeeded", "draft": draft, "model": model,
                "project": project.name, "goal": spec.get("goal", ""),
                "warnings": _advisory_warnings(project, draft)}
    finally:
        # Keep VRAM free for the delegation that follows: its ladder starts
        # on the 7b, and the swap costs more than a later reload.
        unload_fn(model)
        lock.release()


def run_draft(job_id, jobs_root, config_path, chat_fn=None, unload_fn=None,
              model=DRAFT_MODEL, system=None, gpu_lock_path=None,
              gpu_timeout=DEFAULT_GPU_TIMEOUT, heartbeat_interval=HEARTBEAT_INTERVAL,
              num_ctx=16384, temperature=0.2):
    store = JobStore(jobs_root)
    manager = None
    system = system or _system_prompt()

    stop = threading.Event()
    beat = threading.Thread(target=_heartbeat_forever,
                            args=(store, job_id, heartbeat_interval, stop), daemon=True)
    beat.start()
    t0 = time.time()
    try:
        if chat_fn is None:
            # Zero-Ollama (2026-07-18): the draft requires the llama-server
            # runtime; injected chat_fns (tests, rigs) bypass it entirely.
            ls_cfg = load_llama_server(config_path)
            if ls_cfg is None:
                raise ConfigError("factory.toml has no [llama_server] section; "
                                  "the Ollama runtime was retired")
            manager = LlamaServerManager(ls_cfg)
            chat_fn = manager.chat
            unload_fn = unload_fn or manager.unload
        if unload_fn is None:
            unload_fn = lambda model: None  # injected chat_fn without an unloader
        result = _execute(store, job_id, jobs_root, config_path, chat_fn, unload_fn,
                          model, system, gpu_lock_path, gpu_timeout, num_ctx,
                          temperature)
    except Exception as e:  # a draft must always leave a verdict behind
        result = {"status": "error", "error": "{}: {}".format(type(e).__name__, e)}
    finally:
        stop.set()
        beat.join(timeout=2)
        if manager is not None:
            # A runner that exits without this leaks a 20 GB server: Windows
            # children survive their parent.
            manager.shutdown()

    result["total_seconds"] = round(time.time() - t0, 1)
    store.write_result(job_id, result)
    store.update_state(job_id, status=result["status"])
    return result


def _cli():
    ap = argparse.ArgumentParser(description="Run one spec draft to completion.")
    ap.add_argument("job_id")
    ap.add_argument("--jobs", default=str(ROOT / "jobs"))
    ap.add_argument("--config", default=str(ROOT / "factory.toml"))
    ap.add_argument("--model", default=DRAFT_MODEL)
    a = ap.parse_args()
    result = run_draft(a.job_id, a.jobs, a.config, model=a.model)
    seek_stdout_to_end()
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["status"] == "succeeded" else 1)


if __name__ == "__main__":
    _cli()
