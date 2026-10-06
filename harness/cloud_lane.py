"""The chat/agent lane over OpenRouter.

Why this module is thin: OpenRouter serves an OpenAI-compatible SSE endpoint
with OpenAI tool-call framing -- the same protocol llama-server serves. So the
parser, the repetition guard and the tool-call assembly are NOT duplicated
here; `llama_client.consume_sse` does that work for both lanes and this module
only builds the request and prices the turn.

What the two lanes do not share is the runtime contract. A local model means
VRAM: a GPU lock held while it is resident, an idle timer, an unload. A cloud
model means none of the four, and means money instead -- which is why every
turn lands in a ledger the operator can read.

    OPENROUTER_API_KEY  the key, from the environment, never from a file
"""
import json
import os
import time
import urllib.error
import urllib.request

import credentials
import llama_client
import providers

OPENROUTER_URL = providers.ROUTES["openrouter"]["url"]   # kept: tests name it
TIMEOUT = 1800.0        # an agent turn can think for minutes

# Set once at startup so a key stored from the studio is reachable from a
# module that must not know what a config file is.
CONFIG_PATH = None

# Provider pinned per vendor prefix, fallbacks off. A model id must name one
# served variant: OpenRouter arbitrating between providers mid-session would
# make a session's numbers unattributable. Absent from this table = let
# OpenRouter route, which is the honest default for a vendor we have not
# measured.
PROVIDER_PIN = {"anthropic": "anthropic", "nvidia": "nvidia"}

# A free model is rate-limited, not rationed by tokens: 20 requests/minute and
# 50/day below 10 USD of purchased credits (1000/day above it). One agent turn
# spends one request PER TOOL CALL, so a working session hits the minute window
# routinely -- and without this, a 429 killed the turn outright.
RETRY_STATUS = (429, 500, 502, 503, 504)   # "later", as opposed to "no"
MAX_ATTEMPTS = 4
BACKOFF_S = 2.0
MAX_BACKOFF_S = 60.0
FREE_TIER_NOTE = ("free models allow 20 requests/minute and 50/day below "
                  "10 USD of purchased credits (1000/day above it); one agent "
                  "turn spends one request per tool call")

_ledger = {"calls": 0, "cost_usd": 0.0, "prompt_tokens": 0,
           "completion_tokens": 0}


def _sleep(seconds):
    """Indirection so a retry policy can be tested without waiting for it."""
    time.sleep(seconds)


def is_cloud(model):
    """A provider prefix is what marks a cloud id: `vendor/model`.

    Deliberately the same rule the bench uses (`cloud_client.is_cloud`), so a
    model that benched as a cloud rung is spelled identically in a session.
    """
    return "/" in (model or "")


def reset_ledger():
    _ledger.update(calls=0, cost_usd=0.0, prompt_tokens=0, completion_tokens=0)


def ledger_snapshot():
    return dict(_ledger)


def preflight(model):
    """Fail before the first turn, not mid-session."""
    if not is_cloud(model):
        return
    cfg = providers.config(model)      # raises on an unknown route
    if cfg.get("browser"):
        # No key to require: a browser route is authorised by being signed in,
        # and its own module says so. Checking a credential here would fail
        # every ChatGPT session before it started.
        return
    credentials.require(cfg["key"], CONFIG_PATH,
                        purpose="the {} lane".format(cfg["label"]))


def _request(model, messages, temperature, tools, options):
    preflight(model)
    cfg = providers.config(model)
    body = {"model": providers.remote_id(model),
            "messages": llama_client._to_openai(messages),
            "stream": True}
    # The turn's accounting rides in the last chunk, and only if asked for --
    # but not in the same words everywhere. `usage: {include}` is OpenRouter's
    # extension and the only one that carries a price; every other route wants
    # the standard `stream_options` and rejects the extension outright.
    if providers.usage_ext(model):
        body["usage"] = {"include": True}
    else:
        body["stream_options"] = {"include_usage": True}
    llama_client._sampling(body, temperature, options)
    if cfg["pin"]:
        # Only an aggregator arbitrates between upstream providers for one
        # model id, so only there does pinning mean anything. A direct route IS
        # the provider.
        pin = PROVIDER_PIN.get(providers.remote_id(model).split("/")[0])
        if pin:
            body["provider"] = {"order": [pin], "allow_fallbacks": False}
    if tools:
        body["tools"] = tools
    key = credentials.require(cfg["key"], CONFIG_PATH)
    return urllib.request.Request(
        cfg["url"], data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key})


def _wait_for(e, attempt):
    """How long before attempt N+1: the server's `Retry-After` if it sent one,
    otherwise exponential backoff. Capped -- a provider asking for a day off is
    a session to abandon, not to sleep through.
    """
    raw = None
    try:
        raw = e.headers.get("Retry-After")
    except AttributeError:
        pass
    if raw:
        try:
            return min(float(raw), MAX_BACKOFF_S)
        except (TypeError, ValueError):
            pass            # HTTP-date form: fall through to the backoff
    return min(BACKOFF_S * (2 ** attempt), MAX_BACKOFF_S)


def _read_body(e):
    try:
        return e.read().decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _is_relayed(body):
    """True when OpenRouter is reporting someone else's failure.

    A 404 normally means "no such model", which retrying cannot fix. But
    OpenRouter also answers 404 when the upstream provider is the one that
    broke, and it says so: `{"error":{"message":"Provider returned error",
    "metadata":{"provider_name":"Nvidia"}}}`. Observed live on 2026-07-29 --
    the same model answered 200 minutes later. That one is "later", not "no",
    and the two are only distinguishable by the body.
    """
    if not body:
        return False
    try:
        parsed = json.loads(body)
    except ValueError:
        return False
    # `error` is a string on some paths ("insufficient credits") and an object
    # on others: only the object form can carry the relay evidence.
    err = parsed.get("error") if isinstance(parsed, dict) else None
    if not isinstance(err, dict):
        return False
    if (err.get("metadata") or {}).get("provider_name"):
        return True
    return "provider returned error" in str(err.get("message", "")).lower()


def _open(req, timeout=None, cancel=None):
    """`urlopen`, with the retries a shared endpoint requires.

    Only failures that mean "later" are retried: a 402 or a 400 says no, and
    replaying it costs the operator time to learn nothing. Called by
    `consume_sse` before its first delta, so a retry never replays a turn the
    operator has already started reading.

    `cancel` covers the backoff, which is where a rate-limited turn spends its
    time: up to a minute of sleep per attempt, with no socket to close and no
    event to stop between.
    """
    route = providers.by_url(getattr(req, "full_url", "")) or "openrouter"
    for attempt in range(MAX_ATTEMPTS):
        if cancel is not None:
            cancel.check()
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            body = _read_body(e)
            if e.code not in RETRY_STATUS and not _is_relayed(body):
                _raise_readable(e, body, route=route)
            last, last_body = e, body
            if attempt == MAX_ATTEMPTS - 1:
                break
            if cancel is None:
                _sleep(_wait_for(e, attempt))
            elif cancel.wait(_wait_for(e, attempt)):
                cancel.check()      # woken by the button, not by the delay
    _raise_readable(last, last_body, exhausted=MAX_ATTEMPTS, route=route)


def _raise_readable(e, body="", exhausted=0, route="openrouter"):
    """The operator reads this line and must know what to do next. A bare 429
    does not say that the daily quota is the thing that ran out."""
    note = ""
    if exhausted:
        note = " (gave up after {} attempts)".format(exhausted)
    if e.code == 429 and route == "openrouter":
        # The free-tier caps are OpenRouter's own; quoting them at Mistral
        # would be a confident lie about someone else's terms.
        note += " -- " + FREE_TIER_NOTE
    elif _is_relayed(body):
        note += " -- the provider failed, not the request; try again or pick "\
                "another model"
    raise RuntimeError("{} HTTP {}{}: {}".format(
        providers.ROUTES[route]["label"], e.code, note,
        body[:500] or e.reason)) from e


def chat_stream(model, messages, temperature=0.7, num_ctx=None, timeout=TIMEOUT,
                tools=None, options=None, cancel=None):
    """Same contract as `llama_client.chat_stream`, one argument apart: the
    first positional is the model id, because for a cloud lane the model IS
    the endpoint. `num_ctx` is ignored -- the window belongs to the provider.
    """
    req = _request(model, messages, temperature, tools, options)
    t0 = time.time()

    def opener(r, timeout=None):
        # Bound to this turn's token so Stop also lands during a backoff: a
        # 429 can park the turn for a minute before the read even starts.
        return _open(r, timeout=timeout, cancel=cancel)

    raw = yield from llama_client.consume_sse(req, timeout, opener=opener,
                                              who=providers.label(model),
                                              cancel=cancel)
    elapsed = time.time() - t0
    usage = raw["usage"] or {}
    out = usage.get("completion_tokens") or 0
    prompt = usage.get("prompt_tokens") or 0
    cost = float(usage.get("cost") or 0.0)
    _ledger["calls"] += 1
    _ledger["cost_usd"] += cost
    _ledger["prompt_tokens"] += prompt
    _ledger["completion_tokens"] += out
    ttft = round(raw["t_first"] - t0, 3) if raw["t_first"] else 0.0
    return llama_client.shape_metrics(raw, {
        "eval_count": out,
        # Wall-clock over the network, not a GPU's rate. Named the same because
        # the UI reads one field; it measures what the operator waits for.
        "tokens_per_s": round(out / elapsed, 1) if elapsed and out else 0.0,
        "prefill_count": prompt,
        "prompt_count": prompt,
        # No KV cache of ours to reuse, so prefill rate has no meaning: zero
        # rather than a plausible lie in a field used to compare lanes.
        "prefill_tokens_per_s": 0.0,
        "ttft_s": ttft,
        "cost_usd": round(cost, 6),
    })


def chat(model, system, user, temperature=0.1, num_ctx=None, options=None):
    """One completion, shaped like the local client's return value.

    Implemented by draining the stream: one transport, so a fix to the parser
    cannot reach the streaming lane and miss this one.
    """
    gen = chat_stream(model, [{"role": "system", "content": system},
                              {"role": "user", "content": user}],
                      temperature=temperature, options=options)
    while True:
        try:
            next(gen)
        except StopIteration as stop:
            return stop.value or {}
