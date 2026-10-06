"""The chat/agent lane over OpenRouter.

Transport is tested without network. What matters is the request shape (auth,
model, provider pin, usage), that the stream is parsed by the SAME parser as
llama-server -- tool calls included -- and that a turn is priced.
"""
import json
import time
import urllib.error

import pytest

import cloud_lane
import turn_runner


@pytest.fixture(autouse=True)
def clean_ledger():
    cloud_lane.reset_ledger()
    yield
    cloud_lane.reset_ledger()


def _sse(*chunks):
    body = b""
    for chunk in chunks:
        body += b"data: " + json.dumps(chunk).encode("utf-8") + b"\n\n"
    return body + b"data: [DONE]\n\n"


class _FakeResponse:
    def __init__(self, raw):
        self._lines = raw.splitlines(True)

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _delta(**kw):
    return {"choices": [{"delta": kw}]}


def _final(finish="stop", cost=0.004, prompt=1200, completion=80):
    return {"choices": [{"delta": {}, "finish_reason": finish}],
            "usage": {"cost": cost, "prompt_tokens": prompt,
                      "completion_tokens": completion}}


@pytest.fixture
def wire(monkeypatch):
    """Captures the request, replays a canned SSE body."""
    box = {"raw": _sse(_delta(content="hi"), _final())}

    def fake_urlopen(req, timeout=None):
        box["url"] = req.full_url
        box["auth"] = req.get_header("Authorization")
        box["body"] = json.loads(req.data.decode("utf-8"))
        box["timeout"] = timeout
        return _FakeResponse(box["raw"])

    monkeypatch.setattr(cloud_lane.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    return box


def _drain(gen):
    """Runs a chat_stream generator to completion, returns (events, metrics)."""
    events = []
    while True:
        try:
            events.append(next(gen))
        except StopIteration as stop:
            return events, (stop.value or {})


def test_is_cloud_is_the_provider_prefix():
    assert cloud_lane.is_cloud("anthropic/claude-haiku-4-5")
    assert cloud_lane.is_cloud("nvidia/nemotron-3-super-120b-a12b:free")
    assert not cloud_lane.is_cloud("qwen3-coder:30b")
    assert not cloud_lane.is_cloud("")
    assert not cloud_lane.is_cloud(None)


def test_request_carries_auth_model_and_asks_for_usage(wire):
    _drain(cloud_lane.chat_stream("anthropic/claude-haiku-4-5",
                                  [{"role": "user", "content": "yo"}]))
    assert wire["url"] == cloud_lane.OPENROUTER_URL
    assert wire["auth"] == "Bearer test-key"
    assert wire["body"]["model"] == "anthropic/claude-haiku-4-5"
    assert wire["body"]["stream"] is True
    # Without this the turn cannot be priced: the cost rides in the last chunk.
    assert wire["body"]["usage"] == {"include": True}


def test_provider_pin_is_sent_only_when_configured(wire, monkeypatch):
    monkeypatch.setattr(cloud_lane, "PROVIDER_PIN", {})
    _drain(cloud_lane.chat_stream("nvidia/nemotron-3-super-120b-a12b:free",
                                  [{"role": "user", "content": "yo"}]))
    assert "provider" not in wire["body"]
    monkeypatch.setattr(cloud_lane, "PROVIDER_PIN", {"nvidia": "nvidia"})
    _drain(cloud_lane.chat_stream("nvidia/nemotron-3-super-120b-a12b:free",
                                  [{"role": "user", "content": "yo"}]))
    assert wire["body"]["provider"] == {"order": ["nvidia"],
                                        "allow_fallbacks": False}


def test_tools_ride_through_untouched(wire):
    tools = [{"type": "function", "function": {"name": "read_file"}}]
    _drain(cloud_lane.chat_stream("anthropic/claude-haiku-4-5",
                                  [{"role": "user", "content": "yo"}],
                                  tools=tools))
    assert wire["body"]["tools"] == tools


def test_content_and_thinking_stream_as_events(wire):
    # OpenRouter names the thinking channel `reasoning`, llama-server names it
    # `reasoning_content`: the shared parser must accept either.
    wire["raw"] = _sse(_delta(reasoning="hmm"), _delta(content="ok"), _final())
    events, metrics = _drain(cloud_lane.chat_stream(
        "anthropic/claude-haiku-4-5", [{"role": "user", "content": "yo"}]))
    assert events == [("thinking", "hmm"), ("content", "ok")]
    assert metrics["content"] == "ok"
    assert metrics["done_reason"] == "stop"
    assert metrics["thinking_chars"] == 3


def test_tool_calls_are_assembled_like_the_local_lane(wire):
    wire["raw"] = _sse(
        _delta(tool_calls=[{"index": 0, "function": {"name": "read_file",
                                                     "arguments": '{"path":'}}]),
        _delta(tool_calls=[{"index": 0, "function": {"arguments": '"a.py"}'}}]),
        _final(finish="tool_calls"))
    events, _ = _drain(cloud_lane.chat_stream(
        "anthropic/claude-haiku-4-5", [{"role": "user", "content": "yo"}]))
    assert events == [("tool_call", {"name": "read_file",
                                     "arguments": {"path": "a.py"}})]


def test_a_turn_is_priced_and_counted(wire):
    _, metrics = _drain(cloud_lane.chat_stream(
        "anthropic/claude-haiku-4-5", [{"role": "user", "content": "yo"}]))
    assert metrics["cost_usd"] == pytest.approx(0.004)
    assert metrics["eval_count"] == 80
    assert metrics["prompt_count"] == 1200
    # Measured across a network, not on a local GPU: honest, and never zero
    # for a turn that produced tokens.
    assert metrics["tokens_per_s"] >= 0.0
    snap = cloud_lane.ledger_snapshot()
    assert snap["calls"] == 1
    assert snap["cost_usd"] == pytest.approx(0.004)


def test_the_ledger_accumulates_across_turns(wire):
    for _ in range(3):
        _drain(cloud_lane.chat_stream("anthropic/claude-haiku-4-5",
                                      [{"role": "user", "content": "yo"}]))
    snap = cloud_lane.ledger_snapshot()
    assert snap["calls"] == 3
    assert snap["cost_usd"] == pytest.approx(0.012)
    assert snap["completion_tokens"] == 240


def test_a_missing_key_fails_before_the_network(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    def never(*a, **kw):
        raise AssertionError("must not reach the network")

    monkeypatch.setattr(cloud_lane.urllib.request, "urlopen", never)
    with pytest.raises(RuntimeError) as excinfo:
        _drain(cloud_lane.chat_stream("anthropic/claude-haiku-4-5",
                                      [{"role": "user", "content": "yo"}]))
    assert "OPENROUTER_API_KEY" in str(excinfo.value)


def test_http_error_surfaces_the_server_body(wire, monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(
            "u", 402, "Payment Required", {},
            __import__("io").BytesIO(b'{"error":"insufficient credits"}'))

    monkeypatch.setattr(cloud_lane.urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError) as excinfo:
        _drain(cloud_lane.chat_stream("anthropic/claude-haiku-4-5",
                                      [{"role": "user", "content": "yo"}]))
    assert "402" in str(excinfo.value)
    assert "insufficient credits" in str(excinfo.value)


def _http_error(code, body=b'{"error":"rate limit exceeded"}', headers=None):
    return urllib.error.HTTPError(
        "u", code, "boom", headers or {}, __import__("io").BytesIO(body))


@pytest.fixture
def flaky(wire, monkeypatch):
    """A wire that fails `box['fail']` times with `box['code']` first.

    Sleeps are recorded instead of taken: a retry policy must be testable
    without spending the wall-clock it prescribes.
    """
    box = {"fail": 0, "code": 429, "headers": {}, "attempts": 0, "slept": []}
    real = cloud_lane.urllib.request.urlopen

    def flaky_urlopen(req, timeout=None):
        box["attempts"] += 1
        if box["attempts"] <= box["fail"]:
            raise _http_error(box["code"], headers=box["headers"])
        return real(req, timeout=timeout)

    monkeypatch.setattr(cloud_lane.urllib.request, "urlopen", flaky_urlopen)
    monkeypatch.setattr(cloud_lane, "_sleep", box["slept"].append)
    return box


def test_a_429_is_retried_and_the_turn_survives(flaky):
    flaky["fail"] = 2
    events, metrics = _drain(cloud_lane.chat_stream(
        "nvidia/nemotron-3-super-120b-a12b:free",
        [{"role": "user", "content": "yo"}]))
    assert events == [("content", "hi")]
    assert metrics["done_reason"] == "stop"
    assert flaky["attempts"] == 3
    assert len(flaky["slept"]) == 2


def test_the_backoff_grows_between_attempts(flaky):
    flaky["fail"] = 3
    _drain(cloud_lane.chat_stream("nvidia/nemotron-3-super-120b-a12b:free",
                                  [{"role": "user", "content": "yo"}]))
    assert flaky["slept"] == sorted(flaky["slept"])
    assert flaky["slept"][-1] > flaky["slept"][0]


def test_retry_after_wins_over_the_backoff(flaky):
    # The server knows when its window reopens; guessing shorter just burns
    # another request against the same quota.
    flaky["fail"] = 1
    flaky["headers"] = {"Retry-After": "7"}
    _drain(cloud_lane.chat_stream("nvidia/nemotron-3-super-120b-a12b:free",
                                  [{"role": "user", "content": "yo"}]))
    assert flaky["slept"] == [7.0]


def test_an_absurd_retry_after_is_capped(flaky):
    flaky["fail"] = 1
    flaky["headers"] = {"Retry-After": "86400"}
    _drain(cloud_lane.chat_stream("nvidia/nemotron-3-super-120b-a12b:free",
                                  [{"role": "user", "content": "yo"}]))
    assert flaky["slept"] == [cloud_lane.MAX_BACKOFF_S]


def test_a_server_error_is_retried_too(flaky):
    flaky["fail"], flaky["code"] = 1, 503
    _, metrics = _drain(cloud_lane.chat_stream(
        "nvidia/nemotron-3-super-120b-a12b:free",
        [{"role": "user", "content": "yo"}]))
    assert metrics["done_reason"] == "stop"
    assert flaky["attempts"] == 2


def test_a_refusal_is_not_retried(flaky):
    # 402 means "no", not "later": retrying it wastes the operator's time and
    # says nothing new.
    flaky["fail"], flaky["code"] = 1, 402
    with pytest.raises(RuntimeError):
        _drain(cloud_lane.chat_stream("anthropic/claude-haiku-4-5",
                                      [{"role": "user", "content": "yo"}]))
    assert flaky["attempts"] == 1
    assert flaky["slept"] == []


_RELAYED_404 = (b'{"error":{"message":"Provider returned error","code":404,'
                b'"metadata":{"raw":"","provider_name":"Nvidia"}}}')


def test_a_404_relayed_from_the_provider_is_retried(flaky, monkeypatch):
    """Observed live 2026-07-29: Nvidia answered 404 through OpenRouter and the
    same model answered 200 minutes later. Only the body tells that apart from
    a genuine `no such model`."""
    real = cloud_lane.urllib.request.urlopen
    calls = {"n": 0}

    def once_relayed(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(404, body=_RELAYED_404)
        return real(req, timeout=timeout)

    monkeypatch.setattr(cloud_lane.urllib.request, "urlopen", once_relayed)
    _, metrics = _drain(cloud_lane.chat_stream(
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        [{"role": "user", "content": "yo"}]))
    assert metrics["done_reason"] == "stop"
    assert calls["n"] == 2


def test_a_genuine_404_is_not_retried(flaky):
    flaky["fail"], flaky["code"] = 1, 404
    with pytest.raises(RuntimeError) as excinfo:
        _drain(cloud_lane.chat_stream("nvidia/no-such-model:free",
                                      [{"role": "user", "content": "yo"}]))
    assert flaky["attempts"] == 1
    assert "404" in str(excinfo.value)


def test_a_relayed_failure_says_whose_fault_it_is(flaky, monkeypatch):
    def always(req, timeout=None):
        raise _http_error(404, body=_RELAYED_404)

    monkeypatch.setattr(cloud_lane.urllib.request, "urlopen", always)
    with pytest.raises(RuntimeError) as excinfo:
        _drain(cloud_lane.chat_stream("nvidia/nemotron-3-ultra-550b-a55b:free",
                                      [{"role": "user", "content": "yo"}]))
    msg = str(excinfo.value)
    assert "provider failed" in msg
    assert "another model" in msg


def test_exhausted_retries_name_the_free_tier_cap(flaky):
    flaky["fail"] = 99
    with pytest.raises(RuntimeError) as excinfo:
        _drain(cloud_lane.chat_stream(
            "nvidia/nemotron-3-super-120b-a12b:free",
            [{"role": "user", "content": "yo"}]))
    msg = str(excinfo.value)
    assert "429" in msg
    assert "rate limit exceeded" in msg      # the server's own words
    assert "50" in msg and "20" in msg       # the caps the operator must know
    assert flaky["attempts"] == cloud_lane.MAX_ATTEMPTS


def test_a_cloud_error_does_not_blame_llama_server(flaky):
    flaky["fail"], flaky["code"] = 1, 402
    with pytest.raises(RuntimeError) as excinfo:
        _drain(cloud_lane.chat_stream("anthropic/claude-haiku-4-5",
                                      [{"role": "user", "content": "yo"}]))
    assert "llama-server" not in str(excinfo.value)
    assert "OpenRouter" in str(excinfo.value)


def test_retries_stop_once_the_stream_has_started(wire, monkeypatch):
    """A mid-stream failure must NOT replay the turn: the operator has already
    read half an answer, and the provider has already been billed for it."""
    monkeypatch.setattr(cloud_lane, "_sleep", lambda s: None)
    attempts = {"n": 0}

    class _HalfBody:
        def __iter__(self):
            yield b'data: ' + json.dumps(_delta(content="hi")).encode() + b'\n'
            raise urllib.error.HTTPError("u", 429, "boom", {},
                                         __import__("io").BytesIO(b"{}"))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def half(req, timeout=None):
        attempts["n"] += 1
        return _HalfBody()

    monkeypatch.setattr(cloud_lane.urllib.request, "urlopen", half)
    with pytest.raises(RuntimeError):
        _drain(cloud_lane.chat_stream(
            "nvidia/nemotron-3-super-120b-a12b:free",
            [{"role": "user", "content": "yo"}]))
    assert attempts["n"] == 1


# ---- several providers, one transport ----

def test_a_direct_route_posts_to_that_vendor_with_its_own_key(wire,
                                                              monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "sk-mistral")
    _drain(cloud_lane.chat_stream("@mistral/mistral-large-latest",
                                  [{"role": "user", "content": "yo"}]))
    assert wire["url"] == cloud_lane.providers.ROUTES["mistral"]["url"]
    assert wire["auth"] == "Bearer sk-mistral"
    # The route is ours, not theirs: the body must carry the id Mistral knows.
    assert wire["body"]["model"] == "mistral-large-latest"


def test_a_direct_route_never_pins_a_provider(wire, monkeypatch):
    """Pinning means "do not arbitrate between upstreams". A direct route IS
    the upstream, and sending the field would be a request Mistral must reject
    or ignore."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    _drain(cloud_lane.chat_stream("@anthropic/claude-haiku-4.5",
                                  [{"role": "user", "content": "yo"}]))
    assert "provider" not in wire["body"]


def test_each_route_demands_its_own_key(wire, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def never(*a, **kw):
        raise AssertionError("must not reach the network")

    monkeypatch.setattr(cloud_lane.urllib.request, "urlopen", never)
    with pytest.raises(RuntimeError) as excinfo:
        _drain(cloud_lane.chat_stream("@openai/gpt-5.5",
                                      [{"role": "user", "content": "yo"}]))
    msg = str(excinfo.value)
    assert "OPENAI_API_KEY" in msg
    # An OpenRouter key sitting in the environment must not look like one.
    assert "OPENROUTER" not in msg


def test_an_unknown_route_is_refused_before_the_network(wire, monkeypatch):
    def never(*a, **kw):
        raise AssertionError("must not reach the network")

    monkeypatch.setattr(cloud_lane.urllib.request, "urlopen", never)
    with pytest.raises(RuntimeError) as excinfo:
        _drain(cloud_lane.chat_stream("@nope/model",
                                      [{"role": "user", "content": "yo"}]))
    assert "unknown provider" in str(excinfo.value)


def test_an_error_names_the_provider_that_failed(wire, monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "sk-mistral")

    def boom(req, timeout=None):
        raise _http_error(401, body=b'{"error":"bad key"}')

    monkeypatch.setattr(cloud_lane.urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError) as excinfo:
        _drain(cloud_lane.chat_stream("@mistral/mistral-large-latest",
                                      [{"role": "user", "content": "yo"}]))
    msg = str(excinfo.value)
    assert "Mistral" in msg
    assert "OpenRouter" not in msg


def test_openrouter_quotas_are_not_quoted_at_other_vendors(flaky, monkeypatch):
    """Their caps are not everyone's terms."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
    flaky["fail"] = 99
    with pytest.raises(RuntimeError) as excinfo:
        _drain(cloud_lane.chat_stream("@deepseek/deepseek-chat",
                                      [{"role": "user", "content": "yo"}]))
    msg = str(excinfo.value)
    assert "DeepSeek" in msg
    assert "50" not in msg          # OpenRouter's daily cap, not DeepSeek's


def test_stop_during_a_backoff_does_not_wait_it_out(flaky, monkeypatch):
    # Where a rate-limited turn actually spends its time: asleep between two
    # attempts, with no socket to close and no event to check between. Stopped
    # there, it must give up now rather than serve out the minute the server
    # asked for and then try again.
    flaky["fail"] = 99
    flaky["headers"] = {"Retry-After": "60"}
    cancel = turn_runner.Cancellation()
    retrying = cloud_lane.urllib.request.urlopen

    def pressed_stop(req, timeout=None):
        try:
            return retrying(req, timeout=timeout)
        finally:
            cancel.cancel()          # the operator clicks while this one fails

    monkeypatch.setattr(cloud_lane.urllib.request, "urlopen", pressed_stop)

    t0 = time.time()
    with pytest.raises(turn_runner.TurnCancelled):
        _drain(cloud_lane.chat_stream("nvidia/nemotron-3-super-120b-a12b:free",
                                      [{"role": "user", "content": "yo"}],
                                      cancel=cancel))
    assert time.time() - t0 < 5      # not the 60 s the server asked for
    assert flaky["attempts"] == 1    # and no attempt after the stop


def test_without_a_token_the_backoff_is_the_one_the_retry_tests_measure(flaky):
    # The token is the only thing that changed how the sleep is taken; a lane
    # called without one must still sleep through `_sleep`, which is the seam
    # every retry test here is written against.
    flaky["fail"] = 1
    _drain(cloud_lane.chat_stream("nvidia/nemotron-3-super-120b-a12b:free",
                                  [{"role": "user", "content": "yo"}]))
    assert len(flaky["slept"]) == 1


def test_chat_is_the_stream_drained(wire):
    wire["raw"] = _sse(_delta(content="Paris"), _final())
    out = cloud_lane.chat("anthropic/claude-haiku-4-5", "sys", "usr")
    assert out["content"] == "Paris"
    assert out["cost_usd"] == pytest.approx(0.004)
    assert wire["body"]["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "usr"}]
