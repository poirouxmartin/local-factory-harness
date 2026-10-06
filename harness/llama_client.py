"""Adapter: llama-server's OpenAI /v1/chat/completions -> the chat contract
the ladder and studio consume.

One llama-server holds ONE model, so there is no model name in the request:
the caller picks the server URL. num_ctx is a server start-up parameter (-c),
not a request field; it is accepted and ignored here so a ladder chat_fn can
be swapped in without touching loop_job.
"""
import contextlib
import json
import re
import socket
import time
import urllib.error
import urllib.request

import turn_runner
from repetition_guard import RepetitionGuard

TIMEOUT = 1800.0  # a 35b attempt can think for many minutes


def _unblocker(resp):
    """What to call, from another thread, to make a blocked read give up.

    Closing is enough on Windows, where closing a socket makes the `recv`
    already parked on it fail. Elsewhere the close only drops our reference
    and the read stays parked, so the socket is shut down first -- through a
    private attribute, deliberately: there is no public way to say "abort the
    read someone else is inside of", and the alternative is a Stop that waits
    out a 1800 s timeout.
    """
    def close():
        sock = getattr(getattr(resp, "fp", None), "raw", None)
        sock = getattr(sock, "_sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass                    # already gone: the close still runs
        resp.close()
    return close


@contextlib.contextmanager
def _stoppable(cancel, resp):
    """Arm Stop for the span this response is being read."""
    if cancel is None:
        yield
        return
    with cancel.unblocks(_unblocker(resp)):
        yield


def _sampling(body, temperature, options):
    body["temperature"] = temperature
    options = options or {}
    if options.get("num_predict"):
        body["max_tokens"] = options["num_predict"]
    # Forward the sampling profile: llama.cpp's own defaults (top_p .95,
    # top_k 40, min_p .05) are measurably more diverse than the Qwen card
    # profiles and re-drowned diff replies (autopsy j_575efd4c).
    for key in ("top_p", "top_k", "min_p", "presence_penalty", "repeat_penalty"):
        if key in options:
            body[key] = options[key]


def _raise_readable(e, who="llama-server"):
    """A bare HTTPError 500 tells the operator nothing; the server's body says
    "model not found" or "out of memory". Surface it.

    `who` names the transport that failed: the same parser now serves two, and
    an OpenRouter outage reported as a llama-server fault sends the operator
    looking at the wrong machine.
    """
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read().decode("utf-8", errors="replace").strip()
        except Exception:
            body = ""
        raise RuntimeError("{} HTTP {}: {}".format(
            who, e.code, body[:500] or e.reason)) from e
    raise RuntimeError("{} unreachable: {}".format(
        who, getattr(e, "reason", e))) from e


def _to_openai(messages):
    """Wire factory_mcp -> OpenAI: arguments JSON-encodés, ids synthétiques,
    notes 'tool' orphelines (tool_name=system, pas d'appel en attente)
    reversées en user -- un template jinja strict rejette un message tool
    sans tool_call correspondant."""
    out, n, pending_ids = [], 0, []
    for m in messages:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            calls, pending_ids = [], []
            for c in m["tool_calls"]:
                fn = c.get("function") or {}
                n += 1
                cid = "call_{}".format(n)
                pending_ids.append(cid)
                calls.append({"id": cid, "type": "function",
                              "function": {"name": fn.get("name", ""),
                                           "arguments": json.dumps(
                                               fn.get("arguments") or {})}})
            out.append({"role": "assistant", "content": m.get("content") or "",
                        "tool_calls": calls})
        elif role == "tool":
            if pending_ids:
                out.append({"role": "tool",
                            "tool_call_id": pending_ids.pop(0),
                            "name": m.get("tool_name") or "",
                            "content": m.get("content") or ""})
            else:
                out.append({"role": "user",
                            "content": "[system] " + (m.get("content") or "")})
        else:
            out.append({"role": role, "content": m.get("content") or ""})
    return out


def chat(base_url, system, user, temperature=0.1, num_ctx=None, options=None):
    body = {
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }
    _sampling(body, temperature, options)
    req = urllib.request.Request(
        base_url.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as f:
            out = json.loads(f.read().decode("utf-8"))
    except urllib.error.URLError as e:  # HTTPError included
        _raise_readable(e)
    choice = out["choices"][0]
    timings = out.get("timings", {})
    return {
        "content": choice["message"].get("content") or "",
        "done_reason": choice.get("finish_reason", ""),
        "eval_count": timings.get("predicted_n", 0),
        "tokens_per_s": timings.get("predicted_per_second", 0.0),
        # prompt_n counts the tokens actually EVALUATED, so a reused KV prefix
        # shows up here as a drop -- it is the evidence a cache A/B needs.
        # chat_stream has always returned it; chat dropped it.
        "prefill_count": timings.get("prompt_n", 0),
        # ...which is exactly why it must not be read as "how full is the
        # window": add back what the cache served (cache_n) for that question.
        "prompt_count": (timings.get("prompt_n", 0)
                         + timings.get("cache_n", 0)),
        "prefill_tokens_per_s": timings.get("prompt_per_second", 0.0),
        "ttft_s": timings.get("prompt_ms", 0.0) / 1000.0,
    }


def tokenize(base_url, text):
    """How many tokens the server's own tokenizer makes of `text`.

    Pricing a prompt's line items has to be exact, and the only tokenizer that
    agrees with the server is the server's. `chat_context.estimate` is chars
    per ratio: a deliberately conservative transport budget, never a
    measurement. An empty block costs nothing and is not worth a round-trip.
    """
    if not text:
        return 0
    req = urllib.request.Request(
        base_url.rstrip("/") + "/tokenize",
        data=json.dumps({"content": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as f:
            out = json.loads(f.read().decode("utf-8"))
    except urllib.error.URLError as e:  # HTTPError included
        _raise_readable(e)
    return len(out.get("tokens") or [])


# Some models (qwen3-coder) emit tool calls as XML text and some GGUF
# templates llama-server can't parse them into structured tool_calls -- the
# call arrives as plain content with finish_reason "stop". The agent loop then
# treats it as a final answer and stops silently (confirmed 2026-07-19 on
# qwen3-coder:30b). Recover the calls from the text as a last resort.
_FN_BLOCK = re.compile(r"<function=([^>\s]+)>(.*?)</function>", re.DOTALL)
_PARAM = re.compile(r"<parameter=([^>\s]+)>(.*?)</parameter>", re.DOTALL)
_HERMES = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
# Raw JSON call format: {"name": "...", "arguments": {...}} without any XML tags.
# Observed with ChatGPT when it was conditioned by previous plugin-format sessions.
_RAW_JSON_CALL = re.compile(r'\{[^{}]*"name"\s*:\s*"([^"]+)"[^{}]*"arguments"\s*:\s*\{[^}]*\}[^}]*\}', re.DOTALL)
_CALL_MARKER = re.compile(r"<tool_call>|<function=")
# Open OR close tags: a thinking model can leak just the closers of a call it
# tried to emit (session c_1a722191). Their presence marks an ATTEMPTED call.
_TOOL_MARKER = re.compile(r"</?tool_call>|</?function[=>]")
_INT = re.compile(r"-?\d+")
_FLOAT = re.compile(r"-?\d+\.\d+")


_LIST_SEP = re.compile(r"[\n;]+")


def _as_list(value):
    """A parameter declared `array`, whatever the model wrote.

    JSON first -- that is what the protocol asks for. A model that answered in
    prose instead gets split on its separators: a list split wrongly is a bad
    plan, while a STRING handed to code expecting a list is a plan of single
    characters (session c_14f20c89, 2026-08-06: `set_plan` steps written as
    "1. Inspecter ...;2. Compiler" became a 36-step plan, one letter each)."""
    try:
        data = json.loads(value)
    except ValueError:
        pass
    else:
        return data if isinstance(data, list) else [data]
    return [p.strip() for p in _LIST_SEP.split(value) if p.strip()]


def _coerce_param(value, kind=None):
    """XML params carry no type -- `kind` is the one the tool schema declares,
    when the caller knows it.

    Without it, only integers/floats are coerced (offset, limit, timeout are
    integer tool params) and everything else stays a string, so a command like
    `true` or `2024` is never turned into a bool/number. With it, the schema
    decides: an `array`/`object` param is the JSON it has to be, and a param
    declared `string` stays a string even when it looks like a number."""
    value = value.strip()
    if kind == "array":
        return _as_list(value)
    if kind == "object":
        try:
            return json.loads(value)
        except ValueError:
            return value
    if kind == "string":
        return value
    if _INT.fullmatch(value):
        return int(value)
    if _FLOAT.fullmatch(value):
        return float(value)
    return value


def _param_types(tools):
    """{tool: {param: declared type}} -- the types the text protocol drops."""
    out = {}
    for tool in tools or []:
        fn = tool.get("function") or tool
        name = fn.get("name")
        if not name:
            continue
        props = ((fn.get("parameters") or {}).get("properties") or {})
        out[name] = {p: (spec or {}).get("type")
                     for p, spec in props.items() if isinstance(spec, dict)}
    return out


def has_tool_call_markers(text):
    """True if the text holds tool-call scaffolding (opening OR closing tags).

    Tells an ATTEMPTED but unparsed call apart from a genuine final answer:
    orphan closers left in the reasoning channel, or a call llama-server
    couldn't structure. The agent loop must re-prompt on these, not stop."""
    return bool(_TOOL_MARKER.search(text or ""))


def _clean_chatgpt_noise(content):
    """Remove ChatGPT web app internal noise from the content.
    
    - Removes genui_search| prefixes (ChatGPT internal telemetry)
    - Removes invalid plugin-format JSON calls ({paths: [...], query: ...})
    - Removes orphaned braces and keywords from plugin calls
    - Preserves valid tool calls in <function=...> and {"name": ..., "arguments": ...} formats
    """
    if not content:
        return content

    # Valid tool-call blocks are held aside while the noise regexes run: the
    # orphaned-brace cleanup below would eat the `}}` that ends a nested JSON
    # call and the call would never parse (measured 2026-08-18 on the hermes
    # round-trip test). The plugin noise these rules target sits outside them.
    held = []

    def _hold(m):
        held.append(m.group(0))
        return "\x00{}".format(len(held) - 1)

    content = re.sub(
        r"<function=[^>]*>.*?</function>|<tool_call>\s*\{.*?\}\s*</tool_call>",
        _hold, content, flags=re.DOTALL)
    
    # ========================================================================
    # PHASE 1: Remove all plugin JSON objects first
    # This must come BEFORE prefix removal so that prefixes like fast| that
    # are followed by plugin JSON get their JSON cleaned up first
    # ========================================================================
    
    # Remove invalid plugin-format JSON: {"paths":[...],"query":"..."} 
    # These are NOT local-factory tool calls and should not be parsed
    content = re.sub(
        r'\{[^{}]*"paths"[^{}]*:[^{}]*"query"[^{}]*:[^}]*\}[^}<>]*',
        '',
        content,
        flags=re.DOTALL
    )
    
    # Remove plugin JSON with "paths" (even without "query")
    content = re.sub(
        r'\{[^{}]*"paths"[^{}]*:[^}]*\}[^}<>]*',
        '',
        content,
        flags=re.DOTALL
    )
    
    # Remove plugin JSON with "uri" (ChatGPT plugin calls)
    content = re.sub(
        r'\{[^{}]*"uri"[^{}]*:[^}]*\}[^}<>]*',
        '',
        content,
        flags=re.DOTALL
    )
    
    # Remove other plugin-format calls like: {"path":"/Plugin Management/...", "args":{...}}
    content = re.sub(
        r'\{[^{}]*"path"\s*:\s*"\/Plugin[^}]*\}[^}<>]*',
        '',
        content,
        flags=re.DOTALL
    )
    
    # Remove plugin JSON with "args" (common in OpenAI plugin calls)
    content = re.sub(
        r'\{[^{}]*"args"[^{}]*:[^}]*\}[^}<>]*',
        '',
        content,
        flags=re.DOTALL
    )
    
    # Remove plugin JSON with "app_id" (seen in connector plugins)
    content = re.sub(
        r'\{[^{}]*"app_id"[^{}]*:[^}]*\}[^}<>]*',
        '',
        content,
        flags=re.DOTALL
    )
    
    # Remove any remaining consecutive braces from plugin cleanup: }{ or }{  
    # This handles cases where multiple plugin JSON objects were adjacent
    content = re.sub(r'\}\{', '', content)
    content = re.sub(r'\}{', '', content)
    
    # ========================================================================
    # PHASE 2: Remove prefix patterns (now that plugin JSON is gone)
    # ========================================================================
    
    # Remove genui_search|... followed by any remaining noise
    content = re.sub(r'genui_search\|[^<\n]*', '', content)
    
    # Remove fast|... with URL and any remaining noise
    content = re.sub(r'fast\|[^<\n]*', '', content)
    
    # Remove calc|...
    content = re.sub(r'calc\|[^<\n]*', '', content)
    
    # Remove noop prefix
    content = re.sub(r'\bnoop\b[^<\n]*', '', content)
    
    # Remove dummy prefix
    content = re.sub(r'dummy[^<\n]*', '', content)
    
    # Remove site: URLs that appear as noise (e.g., "site:example.com" from plugins)
    content = re.sub(r'site:[^ <\n]+', '', content)
    
    # Remove orphaned braces at start/end and consecutive braces
    content = re.sub(r'^[\s]*\}', '', content)  # } at start
    content = re.sub(r'\}[\s]*\{', '', content)  # }{  
    content = re.sub(r'\}[\s]*\}', '', content)  # }}
    
    # Clean up extra spaces and newlines left by removals
    content = re.sub(r'[ \t]+', ' ', content)
    content = re.sub(r'\n+', '\n', content).strip()
    
    # Close unclosed <function=...> tags that ChatGPT sometimes emits
    # Pattern: <function=name> ... without closing </function>
    # We add </function> at the end of the content if there's an unclosed tag
    if '<function=' in content and '</function>' not in content:
        # Find the last occurrence of <function= and ensure it's closed
        # Simple approach: append </function> if there's an unclosed tag
        open_count = content.count('<function=')
        close_count = content.count('</function>')
        if open_count > close_count:
            content = content + '</function>' * (open_count - close_count)

    for i, block in enumerate(held):
        content = content.replace("\x00{}".format(i), block)
    return content


def parse_text_tool_calls(content, tools=None):
    """Recover tool calls a model wrote as TEXT instead of structured
    tool_calls. Returns (calls, cleaned_content): calls is a list of
    {"name", "arguments"} (the same payload chat_stream yields for a real
    tool_call), empty when the text holds none; cleaned_content is the text
    before the first call marker (the model's preamble) so the saved message
    doesn't double-carry the raw call.

    Pass `tools` -- the schema sent for the turn -- whenever it is at hand: it
    is the only description of a parameter's type this protocol has, and a
    tool with an array parameter (`set_plan`, `log_wall`) is unusable without
    it."""
    content = _clean_chatgpt_noise(content or "")
    calls = []
    types = _param_types(tools)
    for name, inner in _FN_BLOCK.findall(content):
        kinds = types.get(name) or {}
        args = {p: _coerce_param(val, kinds.get(p))
                for p, val in _PARAM.findall(inner)}
        calls.append({"name": name, "arguments": args})
    if not calls:
        for blob in _HERMES.findall(content):
            try:
                obj = json.loads(blob)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("name"):
                calls.append({"name": obj["name"],
                              "arguments": obj.get("arguments") or {}})
    # Try raw JSON calls without <tool_call> tags (ChatGPT plugin contamination format)
    if not calls:
        for match in _RAW_JSON_CALL.finditer(content):
            try:
                # Extract the full JSON object from the match
                json_str = match.group(0)
                obj = json.loads(json_str)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("name"):
                calls.append({"name": obj["name"],
                              "arguments": obj.get("arguments") or {}})
    if not calls:
        return [], content
    marker = _CALL_MARKER.search(content)
    cleaned = content[:marker.start()].rstrip() if marker else content
    return calls, cleaned


def consume_sse(req, timeout, opener=None, who="llama-server", cancel=None):
    """Parse one OpenAI-compatible SSE response, whoever serves it.

    Yields ("thinking"|"content", text) as deltas land, then one
    ("tool_call", {...}) per assembled call, and RETURNS the raw
    accumulations -- each transport shapes its own metrics from them, because
    llama-server prices a turn with `timings` and a cloud provider with
    `usage`. Delegate with `raw = yield from consume_sse(req, timeout)`.

    Shared on purpose: llama-server and OpenRouter speak the same protocol
    down to the tool-call framing, so the fragile part -- deltas, repetition
    guard, tool-call assembly -- must exist once.

    `opener` is the seam a transport uses to own its connection policy (the
    cloud lane retries a 429 there). It is called once, before the first
    delta, so what it does stays invisible to the caller -- and a failure
    arriving after the first yield is NOT retryable, which is exactly why the
    policy lives in the opener rather than around this generator.

    `cancel` is the turn's Stop button. The wait that needs it is the one for
    the FIRST token: the connection is open, the prompt is prefilling, and
    until it lands this loop is asleep in a socket read with no event to hang
    a flag check on. So the response is registered as what to close, and the
    flag is polled between lines for the stop that arrives mid-answer.
    """
    parts, guard, stopped, finish = [], RepetitionGuard(), None, ""
    timings, usage, pending_calls = {}, {}, {}
    phase = {"thinking_chars": 0, "content_chars": 0}
    t_think = t_content = t_first = None
    opener = opener or urllib.request.urlopen
    try:
        with contextlib.ExitStack() as open_stack:
            resp = open_stack.enter_context(opener(req, timeout=timeout))
            open_stack.enter_context(_stoppable(cancel, resp))
            for raw in resp:
                if cancel is not None:
                    cancel.check()
                raw = raw.strip()
                if not raw.startswith(b"data:"):
                    continue
                data = raw[5:].strip()
                if data == b"[DONE]":
                    break
                chunk = json.loads(data.decode("utf-8"))
                if chunk.get("timings"):
                    timings = chunk["timings"]
                # A cloud provider prices the turn here instead of in
                # `timings`, and sends it in the last chunk.
                if chunk.get("usage"):
                    usage = chunk["usage"]
                choice = (chunk.get("choices") or [{}])[0]
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
                delta = choice.get("delta") or {}
                # llama-server calls the thinking channel `reasoning_content`,
                # OpenRouter calls it `reasoning`. Same channel.
                think = delta.get("reasoning_content") or delta.get("reasoning") or ""
                if think:
                    now = time.time()
                    t_first = t_first or now
                    t_think = [t_think[0] if t_think else now, now]
                    phase["thinking_chars"] += len(think)
                    yield "thinking", think
                    if guard.feed(think, thinking=True):
                        stopped = "repetition"
                        break
                for frag in delta.get("tool_calls") or []:
                    slot = pending_calls.setdefault(
                        frag.get("index", 0), {"name": "", "arguments": ""})
                    fn = frag.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    slot["arguments"] += fn.get("arguments") or ""
                text = delta.get("content") or ""
                if text:
                    now = time.time()
                    t_first = t_first or now
                    t_content = [t_content[0] if t_content else now, now]
                    phase["content_chars"] += len(text)
                    parts.append(text)
                    yield "content", text
                    if guard.feed(text):
                        stopped = "repetition"
                        break
    except turn_runner.TurnCancelled:
        raise
    except Exception as e:
        # Stop closes the socket under a blocked read, and the read reports
        # that as anything from a truncated body to a closed file. Whatever it
        # says, WE did it: reporting the operator's own click as a dead server
        # would put a red error where they asked for silence.
        if cancel is not None:
            cancel.check()
        if isinstance(e, urllib.error.URLError):    # HTTPError included
            _raise_readable(e, who)
        raise
    if cancel is not None:
        # A close can also land as a clean end of stream rather than an error.
        cancel.check()
    if not stopped:
        for idx in sorted(pending_calls):
            slot = pending_calls[idx]
            try:
                args = json.loads(slot["arguments"]) if slot["arguments"] else {}
            except ValueError:
                args = {}
            yield "tool_call", {"name": slot["name"], "arguments": args}
    return {"parts": parts, "finish": finish, "stopped": stopped,
            "timings": timings, "usage": usage, "phase": phase,
            "t_think": t_think, "t_content": t_content, "t_first": t_first}


def shape_metrics(raw, extra=None):
    """The common tail of a streamed turn: content, stop reason, phase timings.

    Everything a transport measures its own way (tokens/s, ttft, cost) arrives
    through `extra`, so the shared fields cannot drift between the two lanes.
    """
    metrics = {"content": "".join(str(p) for p in raw["parts"] if p), "done_reason": raw["finish"]}
    metrics.update(extra or {})
    metrics.update(raw["phase"])
    stopped = raw["stopped"]
    if not stopped and raw["finish"] == "length":
        stopped = "length"
    if stopped:
        metrics["stopped"] = stopped
    t_think, t_content = raw["t_think"], raw["t_content"]
    metrics["thinking_s"] = round(t_think[1] - t_think[0], 3) if t_think else 0.0
    metrics["writing_s"] = round(t_content[1] - t_content[0], 3) if t_content else 0.0
    return metrics


def chat_stream(base_url, messages, temperature=0.7, num_ctx=None, timeout=900,
                tools=None, options=None, cancel=None):
    """Générateur SSE sur /v1/chat/completions : deltas thinking/content/
    tool_call, métriques via StopIteration.value. num_ctx est un flag serveur,
    ignoré ici. `cancel` : le bouton Stop du tour, cf. `consume_sse`."""
    body = {"messages": _to_openai(messages), "stream": True}
    _sampling(body, temperature, options)
    if tools:
        body["tools"] = tools
    req = urllib.request.Request(
        base_url.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    raw = yield from consume_sse(req, timeout, cancel=cancel)
    timings = raw["timings"]
    return shape_metrics(raw, {
        "eval_count": timings.get("predicted_n", 0),
        "tokens_per_s": timings.get("predicted_per_second", 0.0),
        "prefill_count": timings.get("prompt_n", 0),
        "prompt_count": (timings.get("prompt_n", 0)
                         + timings.get("cache_n", 0)),
        "prefill_tokens_per_s": timings.get("prompt_per_second", 0.0),
        "ttft_s": timings.get("prompt_ms", 0.0) / 1000.0,
    })
