"""Cloud rung for the coder bench: a `chat_fn` the ladder can drive.

Same call signature as `LlamaServerManager.chat`, so `run_job(chat_fn=...)`
takes it unchanged -- that seam is the whole point of the bench. Everything the
harness taxes a local model with (no repo access, no test execution, prompt
rebuilt per attempt, clean slate on escalation) is taxed identically here: the
horizontal axis only means something if the harness is the constant.

Needs OPENROUTER_API_KEY in the environment for the primary (OpenRouter)
path, and/or ANTHROPIC_API_KEY for the direct-SDK path (bare `claude-*`
model ids).
"""
import json
import os
import socket
import time
import urllib.error
import urllib.request

# Sonnet 5 rejects a non-default `temperature` with a 400 (the sampling params
# were removed on that generation). Haiku 4.5 still takes it, so the ladder's
# escalation temperatures survive on the rung where they can.
TEMPERATURE_OK = {"claude-haiku-4-5"}
MAX_OUTPUT = {"claude-haiku-4-5": 64000, "claude-sonnet-5": 128000}
DEFAULT_MAX_OUTPUT = 64000

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Pinned, fallbacks off. cloud_client's whole premise is that the harness is
# the constant (see module docstring); OpenRouter arbitrating between providers
# for one model id would close the registry's reservation by opening another.
OPENROUTER_PROVIDER = "anthropic"
TIMEOUT = 1800.0  # same order as llama_client: an attempt can think for minutes


def _bare(model):
    """`anthropic/claude-haiku-4-5` -> `claude-haiku-4-5`.

    The capability tables above are keyed on the model, not on who serves it.
    """
    return model.split("/")[-1]

_client = None

# Process-global, because `loop_job` builds each attempt record from an
# explicit whitelist (loop_job.py:298-303) and drops anything else the chat
# returns. Widening that whitelist would edit the path every production job
# takes for a bench-only need; a ledger the runner diffs around a cell does
# not. `providers` is a log, not a set: with fallbacks off it should hold one
# name, and a second one is the evidence the pin failed. `models` is the same
# shape for the same reason, but answers a different question: `providers`
# says who served the request, `models` says what the response itself claims
# answered it -- the two can diverge under free-tier routing even with the
# provider pinned.
_ledger = {"calls": 0, "cost_usd": 0.0, "prompt_tokens": 0,
           "completion_tokens": 0, "providers": [], "models": []}


def reset_ledger():
    _ledger.update(calls=0, cost_usd=0.0, prompt_tokens=0,
                   completion_tokens=0)
    del _ledger["providers"][:]
    del _ledger["models"][:]


def ledger_snapshot():
    return {"calls": _ledger["calls"],
            "cost_usd": _ledger["cost_usd"],
            "prompt_tokens": _ledger["prompt_tokens"],
            "completion_tokens": _ledger["completion_tokens"],
            "providers": tuple(_ledger["providers"]),
            "models": tuple(_ledger["models"])}


def ledger_delta(before, after=None):
    """What one cell cost: the difference between two snapshots."""
    if after is None:
        after = ledger_snapshot()
    seen = after["providers"][len(before["providers"]):]
    seen_models = after["models"][len(before["models"]):]
    return {"calls": after["calls"] - before["calls"],
            "cost_usd": round(after["cost_usd"] - before["cost_usd"], 6),
            "prompt_tokens": after["prompt_tokens"] - before["prompt_tokens"],
            "completion_tokens": (after["completion_tokens"]
                                  - before["completion_tokens"]),
            "providers": sorted(set(seen)),
            "models": sorted(set(seen_models))}


def _record(payload):
    usage = payload.get("usage") or {}
    _ledger["calls"] += 1
    _ledger["cost_usd"] += float(usage.get("cost") or 0.0)
    _ledger["prompt_tokens"] += usage.get("prompt_tokens") or 0
    _ledger["completion_tokens"] += usage.get("completion_tokens") or 0
    _ledger["providers"].append(payload.get("provider") or "unknown")
    _ledger["models"].append(payload.get("model") or "unknown")


def is_cloud(model):
    """A provider prefix means OpenRouter; a bare `claude-` means the SDK.

    The prefix is also what makes the routing legible in rows.jsonl, and
    run_bench.slug() already maps `/` to `_`.
    """
    return "/" in model or model.startswith("claude-")


def preflight(models):
    """Fail before cell 1, not at cell 7 of 12."""
    for model in models:
        if "/" in model and not os.environ.get("OPENROUTER_API_KEY"):
            raise RuntimeError("OPENROUTER_API_KEY is unset; the cloud rung "
                               "cannot run ({})".format(model))
        if (model.startswith("claude-")
                and not os.environ.get("ANTHROPIC_API_KEY")):
            raise RuntimeError("ANTHROPIC_API_KEY is unset; the cloud rung "
                               "cannot run ({})".format(model))


def _get_client():
    global _client
    if _client is None:
        import anthropic          # lazy: the local rungs must not need the SDK
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is unset; the cloud rung "
                               "cannot run")
        _client = anthropic.Anthropic()
    return _client


def chat(model, system, user, temperature=0.1, num_ctx=None, options=None):
    """One completion, shaped like the local client's return value.

    `num_ctx` is ignored: the context window is the model's, not ours.
    """
    options = options or {}
    if "/" in model:
        return _openrouter_chat(model, system, user, temperature, options)
    return _anthropic_chat(model, system, user, temperature, num_ctx, options)


def _anthropic_chat(model, system, user, temperature=0.1, num_ctx=None, options=None):
    """One completion, shaped like the local client's return value.

    `num_ctx` is ignored: the context window is the model's, not ours. The
    fields the ladder reads off the response (tokens_per_s, ttft_s, ...) are
    filled where they mean something and left at zero where they don't.
    """
    options = options or {}
    max_tokens = min(options.get("num_predict", 16384),
                     MAX_OUTPUT.get(model, DEFAULT_MAX_OUTPUT))
    kwargs = {"model": model, "max_tokens": max_tokens, "system": system,
              "messages": [{"role": "user", "content": user}]}
    if model in TEMPERATURE_OK:
        kwargs["temperature"] = temperature

    t0 = time.time()
    # Streaming: a 16k-token answer on a thinking model can outlast the
    # non-streaming HTTP timeout, and a dead call would cost the whole cell.
    with _get_client().messages.stream(**kwargs) as stream:
        msg = stream.get_final_message()
    elapsed = time.time() - t0

    text = "".join(b.text for b in msg.content if b.type == "text")
    out = msg.usage.output_tokens
    # Distinct provider tag and cost 0.0: this backend does not expose price,
    # so "anthropic-sdk" + $0 reads as "cloud call, price not exposed here" --
    # true -- rather than the ledger silently dropping this rung's spend, which
    # would read as "$0", i.e. false. See finding 3 of the 2026-07-27 review.
    _record({"usage": {"cost": 0.0, "prompt_tokens": msg.usage.input_tokens,
                       "completion_tokens": out},
             "provider": "anthropic-sdk",
             # The SDK response's own `model` field -- what actually answered,
             # not what was requested. Same field the ladder needs recorded
             # for the OpenRouter path; extending this synthesized dict keeps
             # both backends going through the one `_record` mechanism.
             "model": msg.model})
    return {"content": text,
            "eval_count": out,
            "tokens_per_s": (out / elapsed) if elapsed else 0.0,
            "prefill_tokens_per_s": 0.0,
            "ttft_s": 0.0,
            "done_reason": msg.stop_reason or "",
            "input_tokens": msg.usage.input_tokens,
            # Prompt caching means this is tokens sent, not necessarily tokens
            # evaluated -- an upper bound, but strictly more informative than
            # 0 for telling a truncated attempt apart from a context wall.
            "prompt_count": msg.usage.input_tokens,
            "output_tokens": out}


def _openrouter_chat(model, system, user, temperature, options):
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is unset; the cloud rung "
                           "cannot run")
    bare = _bare(model)
    body = {"model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "provider": {"order": [OPENROUTER_PROVIDER],
                         "allow_fallbacks": False},
            # Same default as _anthropic_chat: the two backends must cap
            # generation identically, or the bride stops being the constant.
            "max_tokens": min(options.get("num_predict", 16384),
                              MAX_OUTPUT.get(bare, DEFAULT_MAX_OUTPUT))}
    if bare in TEMPERATURE_OK:
        body["temperature"] = temperature

    req = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as f:
            payload = json.loads(f.read().decode("utf-8"))
    # HTTPError is a URLError subclass. socket.timeout on getresponse() is not
    # wrapped by urllib's do_open in Python 3.9, so with TIMEOUT=1800s a read
    # timeout on a long generation must be caught here too, or it escapes as
    # a bare, unreadable exception instead of going through _raise_readable.
    except (urllib.error.URLError, socket.timeout) as e:
        _raise_readable(e)
    elapsed = time.time() - t0

    # Record before validating the shape: OpenRouter bills for the call
    # whether or not the response carries usable choices, so the ledger --
    # a spend log -- must not share fate with the return-contract guard below.
    # Recorded exactly once: this is the only _record call on this path, and
    # every exit after this point (guard-raise or normal return) has already
    # passed through it.
    _record(payload)

    if "choices" not in payload or not payload["choices"]:
        _raise_malformed(payload)

    choice = payload["choices"][0]
    usage = payload.get("usage") or {}
    out = usage.get("completion_tokens") or 0
    return {"content": (choice.get("message") or {}).get("content") or "",
            "done_reason": choice.get("finish_reason", ""),
            "eval_count": out,
            "tokens_per_s": (out / elapsed) if elapsed else 0.0,
            # Zero on purpose: these measure a local GPU. A wall-clock guess
            # would be a plausible lie in a file whose job is measurement.
            "prefill_tokens_per_s": 0.0,
            "ttft_s": 0.0,
            "input_tokens": usage.get("prompt_tokens") or 0,
            # Prompt caching means this is tokens sent, not necessarily tokens
            # evaluated -- an upper bound, but strictly more informative than
            # 0 for telling a truncated attempt apart from a context wall.
            "prompt_count": usage.get("prompt_tokens") or 0,
            "output_tokens": out}


def _raise_readable(e):
    """A bare 402 tells the operator nothing; the body says "insufficient
    credits". Surface it. Mirrors llama_client._raise_readable."""
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read().decode("utf-8", errors="replace").strip()
        except Exception:
            body = ""
        raise RuntimeError("OpenRouter HTTP {}: {}".format(
            e.code, body[:500] or e.reason)) from e
    raise RuntimeError("OpenRouter unreachable: {}".format(
        getattr(e, "reason", e))) from e


def _raise_malformed(payload):
    """HTTP 200 but no choices: provider-side error or malformed response.
    Include the error object or a snippet of the payload for diagnosis."""
    try:
        body = json.dumps(payload).strip()
    except Exception:
        body = str(payload)
    # Never include the API key; truncate like _raise_readable does.
    raise RuntimeError("OpenRouter returned malformed response (no choices): {}".format(
        body[:500]))
