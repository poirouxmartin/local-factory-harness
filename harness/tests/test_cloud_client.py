"""The bench's cloud rung. Transport is tested without network: what matters
is the request shape (auth, provider pin) and that the ledger prices a cell."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "experiments" / "coder_bench"))

import cloud_client  # noqa: E402


@pytest.fixture(autouse=True)
def clean_ledger():
    cloud_client.reset_ledger()
    yield
    cloud_client.reset_ledger()


def _usage(cost, prompt, completion, provider="Anthropic", model=None):
    payload = {"usage": {"cost": cost, "prompt_tokens": prompt,
                         "completion_tokens": completion},
               "provider": provider}
    if model is not None:
        payload["model"] = model
    return payload


def test_empty_ledger_is_zeroed():
    snap = cloud_client.ledger_snapshot()
    assert snap["calls"] == 0
    assert snap["cost_usd"] == 0.0
    assert snap["providers"] == ()
    assert snap["models"] == ()


def test_record_accumulates():
    cloud_client._record(_usage(0.01, 100, 20))
    cloud_client._record(_usage(0.02, 200, 30))
    snap = cloud_client.ledger_snapshot()
    assert snap["calls"] == 2
    assert snap["cost_usd"] == pytest.approx(0.03)
    assert snap["prompt_tokens"] == 300
    assert snap["completion_tokens"] == 50


def test_delta_prices_one_cell_only():
    cloud_client._record(_usage(0.01, 100, 20))
    before = cloud_client.ledger_snapshot()
    cloud_client._record(_usage(0.05, 400, 60))
    delta = cloud_client.ledger_delta(before)
    assert delta["calls"] == 1
    assert delta["cost_usd"] == pytest.approx(0.05)
    assert delta["prompt_tokens"] == 400
    assert delta["completion_tokens"] == 60


def test_delta_reports_only_providers_seen_since_before():
    cloud_client._record(_usage(0.01, 10, 1, provider="Anthropic"))
    before = cloud_client.ledger_snapshot()
    cloud_client._record(_usage(0.01, 10, 1, provider="Bedrock"))
    assert cloud_client.ledger_delta(before)["providers"] == ["Bedrock"]


def test_delta_reports_only_models_seen_since_before():
    cloud_client._record(_usage(0.01, 10, 1, model="claude-haiku-4-5"))
    before = cloud_client.ledger_snapshot()
    cloud_client._record(_usage(0.01, 10, 1, model="claude-haiku-4-5-20260101"))
    assert cloud_client.ledger_delta(before)["models"] == [
        "claude-haiku-4-5-20260101"]


def test_missing_cost_and_provider_do_not_crash():
    cloud_client._record({"usage": {"prompt_tokens": 5}})
    snap = cloud_client.ledger_snapshot()
    assert snap["cost_usd"] == 0.0
    assert snap["completion_tokens"] == 0
    assert snap["providers"] == ("unknown",)
    assert snap["models"] == ("unknown",)


import json
import socket
import urllib.error


class _FakeResponse:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _reply(content="ok", cost=0.01, prompt=100, completion=20,
           model="anthropic/claude-haiku-4-5"):
    return {"choices": [{"message": {"content": content},
                         "finish_reason": "stop"}],
            "usage": {"cost": cost, "prompt_tokens": prompt,
                      "completion_tokens": completion},
            "provider": "Anthropic",
            "model": model}


@pytest.fixture
def sent(monkeypatch):
    """Captures the request cloud_client would have put on the wire."""
    box = {"payload": _reply()}

    def fake_urlopen(req, timeout=None):
        box["url"] = req.full_url
        box["auth"] = req.get_header("Authorization")
        box["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse(box["payload"])

    monkeypatch.setattr(cloud_client.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    return box


def test_request_carries_auth_and_pins_the_provider(sent):
    cloud_client._openrouter_chat("anthropic/claude-haiku-4-5", "sys", "usr",
                                  0.1, {})
    assert sent["url"] == cloud_client.OPENROUTER_URL
    assert sent["auth"] == "Bearer test-key"
    assert sent["body"]["model"] == "anthropic/claude-haiku-4-5"
    assert sent["body"]["provider"] == {
        "order": [cloud_client.OPENROUTER_PROVIDER], "allow_fallbacks": False}
    assert sent["body"]["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "usr"}]


def test_num_predict_becomes_max_tokens_and_is_capped(sent):
    cloud_client._openrouter_chat("anthropic/claude-haiku-4-5", "s", "u", 0.1,
                                  {"num_predict": 10 ** 9})
    assert sent["body"]["max_tokens"] == cloud_client.MAX_OUTPUT["claude-haiku-4-5"]


def test_temperature_sent_only_where_the_model_accepts_it(sent):
    cloud_client._openrouter_chat("anthropic/claude-haiku-4-5", "s", "u", 0.7, {})
    assert sent["body"]["temperature"] == 0.7
    cloud_client._openrouter_chat("anthropic/claude-sonnet-5", "s", "u", 0.7, {})
    assert "temperature" not in sent["body"]


def test_response_is_normalized_to_the_chat_contract(sent):
    out = cloud_client._openrouter_chat("anthropic/claude-haiku-4-5", "s", "u",
                                        0.1, {})
    assert out["content"] == "ok"
    assert out["done_reason"] == "stop"
    assert out["eval_count"] == 20
    assert out["output_tokens"] == 20
    assert out["input_tokens"] == 100
    # Local-GPU metrics have no meaning across a network (spec: accepted
    # degradation), and must stay zero rather than carry a plausible lie.
    assert out["ttft_s"] == 0.0
    assert out["prefill_tokens_per_s"] == 0.0
    assert out["tokens_per_s"] >= 0.0


def test_response_includes_prompt_count(sent):
    """Without prompt_count, a failed attempt cannot be told apart from a
    context wall (finding 2, 2026-07-27 review) -- the number the harness-tax
    question turns on. The default `sent` fixture reply carries prompt=100."""
    out = cloud_client._openrouter_chat("anthropic/claude-haiku-4-5", "s", "u",
                                        0.1, {})
    assert out["prompt_count"] == 100


def test_call_lands_in_the_ledger(sent):
    cloud_client._openrouter_chat("anthropic/claude-haiku-4-5", "s", "u", 0.1, {})
    snap = cloud_client.ledger_snapshot()
    assert snap["calls"] == 1
    assert snap["cost_usd"] == pytest.approx(0.01)
    assert snap["providers"] == ("Anthropic",)


def test_openrouter_response_records_the_responding_model(sent):
    """The served variant, not the requested id: on free-tier routing
    especially, OpenRouter can answer with a different model than the one
    pinned in the request. The response's own top-level `model` field is the
    only honest source for that."""
    sent["payload"] = _reply(model="anthropic/claude-haiku-4-5-20260315")
    cloud_client._openrouter_chat("anthropic/claude-haiku-4-5", "s", "u", 0.1, {})
    snap = cloud_client.ledger_snapshot()
    assert snap["models"] == ("anthropic/claude-haiku-4-5-20260315",)


def test_openrouter_response_missing_model_falls_back_to_unknown(sent):
    sent["payload"] = {"choices": [{"message": {"content": "ok"},
                                    "finish_reason": "stop"}],
                       "usage": {"cost": 0.01, "prompt_tokens": 10,
                                "completion_tokens": 2},
                       "provider": "Anthropic"}
    cloud_client._openrouter_chat("anthropic/claude-haiku-4-5", "s", "u", 0.1, {})
    snap = cloud_client.ledger_snapshot()
    assert snap["models"] == ("unknown",)


def test_http_error_surfaces_the_server_body(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(
            "u", 402, "Payment Required", {},
            __import__("io").BytesIO(b'{"error":"insufficient credits"}'))

    monkeypatch.setattr(cloud_client.urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError) as excinfo:
        cloud_client._openrouter_chat("anthropic/claude-haiku-4-5", "s", "u",
                                      0.1, {})
    assert "402" in str(excinfo.value)
    assert "insufficient credits" in str(excinfo.value)


def test_read_timeout_surfaces_readable(monkeypatch):
    """socket.timeout on getresponse() is not wrapped by urllib's do_open in
    Python 3.9; with TIMEOUT=1800s this is the plausible failure on a long
    generation, and must go through _raise_readable, not escape bare
    (finding 6, 2026-07-27 review)."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    def boom(req, timeout=None):
        raise socket.timeout("timed out")

    monkeypatch.setattr(cloud_client.urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError) as excinfo:
        cloud_client._openrouter_chat("anthropic/claude-haiku-4-5", "s", "u",
                                      0.1, {})
    assert "OpenRouter" in str(excinfo.value)


def test_empty_content_does_not_crash(sent):
    sent["payload"] = {"choices": [{"message": {"content": None},
                                    "finish_reason": "length"}],
                       "usage": {}}
    out = cloud_client._openrouter_chat("anthropic/claude-haiku-4-5", "s", "u",
                                        0.1, {})
    assert out["content"] == ""
    assert out["done_reason"] == "length"


def test_choice_missing_message_does_not_crash(sent):
    """A choice lacking `message` (streaming-shaped `delta`, or a per-choice
    error object) must not KeyError (finding 7, 2026-07-27 review)."""
    sent["payload"] = {"choices": [{"finish_reason": "error"}], "usage": {}}
    out = cloud_client._openrouter_chat("anthropic/claude-haiku-4-5", "s", "u",
                                        0.1, {})
    assert out["content"] == ""
    assert out["done_reason"] == "error"


def test_malformed_response_missing_choices(sent):
    """HTTP 200 with no choices key (provider-side failure) surfaces as
    readable error -- but OpenRouter still billed for it (this payload
    carries a real cost), so the ledger must record the call before raising
    (finding 1, 2026-07-27 review): an under-count is the worst way for a
    spend instrument to err."""
    sent["payload"] = {"usage": {"cost": 0.01}, "error": "provider unavailable"}
    with pytest.raises(RuntimeError) as excinfo:
        cloud_client._openrouter_chat("anthropic/claude-haiku-4-5", "s", "u",
                                      0.1, {})
    assert "malformed" in str(excinfo.value).lower() or "choices" in str(excinfo.value).lower()
    snap = cloud_client.ledger_snapshot()
    assert snap["calls"] == 1
    assert snap["cost_usd"] == pytest.approx(0.01)


def test_malformed_response_explicit_error(sent):
    """HTTP 200 with error object (not choices) surfaces as readable error."""
    sent["payload"] = {"error": {"code": "provider_error", "message": "model capacity exceeded"}}
    with pytest.raises(RuntimeError) as excinfo:
        cloud_client._openrouter_chat("anthropic/claude-haiku-4-5", "s", "u",
                                      0.1, {})
    # Should include the error message or at least the fact it's malformed
    error_str = str(excinfo.value)
    assert "malformed" in error_str.lower() or "choices" in error_str.lower() or "provider" in error_str.lower()


def test_is_cloud_covers_both_backends():
    assert cloud_client.is_cloud("anthropic/claude-haiku-4-5")
    assert cloud_client.is_cloud("claude-haiku-4-5")
    assert not cloud_client.is_cloud("qwen3-coder:30b")
    assert not cloud_client.is_cloud("qwen2.5-coder:7b")


def test_chat_routes_prefixed_ids_to_openrouter(sent):
    out = cloud_client.chat("anthropic/claude-haiku-4-5", "s", "u")
    assert out["content"] == "ok"
    assert sent["body"]["provider"]["allow_fallbacks"] is False


def test_chat_routes_bare_claude_id_to_anthropic_backend(monkeypatch):
    """The bare-`claude-` dispatch branch (finding 8, 2026-07-27 review): never
    exercised through the public `chat()` entrypoint before this test. Locks
    the positional-argument contract between `chat()` and `_anthropic_chat`."""
    calls = []

    def fake_anthropic_chat(model, system, user, temperature, num_ctx, options):
        calls.append((model, system, user, temperature, num_ctx, options))
        return {"content": "stubbed"}

    monkeypatch.setattr(cloud_client, "_anthropic_chat", fake_anthropic_chat)
    out = cloud_client.chat("claude-haiku-4-5", "sys", "usr", 0.3, 8192,
                            {"num_predict": 100})
    assert out == {"content": "stubbed"}
    assert calls == [("claude-haiku-4-5", "sys", "usr", 0.3, 8192,
                      {"num_predict": 100})]


class _FakeBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeUsage:
    def __init__(self, input_tokens, output_tokens):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeMessage:
    def __init__(self, text, input_tokens, output_tokens, stop_reason="end_turn",
                 model="claude-haiku-4-5-20260315"):
        self.content = [_FakeBlock(text)]
        self.usage = _FakeUsage(input_tokens, output_tokens)
        self.stop_reason = stop_reason
        self.model = model


class _FakeStream:
    def __init__(self, msg):
        self._msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._msg


class _FakeMessages:
    def __init__(self, msg):
        self._msg = msg

    def stream(self, **kwargs):
        return _FakeStream(self._msg)


class _FakeAnthropicClient:
    def __init__(self, msg):
        self.messages = _FakeMessages(msg)


def test_anthropic_chat_records_ledger_with_distinct_provider_and_zero_cost(monkeypatch):
    """Before this fix, `_record` was called only from `_openrouter_chat`, so
    a `--models claude-haiku-4-5` run wrote `cost_usd: 0.0` and
    `provider: ""` -- a plausible-looking number that is wrong (finding 3,
    2026-07-27 review). A non-empty provider with zero cost should read as
    "cloud call, price not exposed by this backend", not "$0"."""
    msg = _FakeMessage("hi", input_tokens=50, output_tokens=10)
    monkeypatch.setattr(cloud_client, "_get_client",
                        lambda: _FakeAnthropicClient(msg))
    out = cloud_client._anthropic_chat("claude-haiku-4-5", "s", "u", 0.1,
                                       None, {})
    assert out["content"] == "hi"
    assert out["input_tokens"] == 50
    assert out["output_tokens"] == 10
    assert out["prompt_count"] == 50

    snap = cloud_client.ledger_snapshot()
    assert snap["calls"] == 1
    assert snap["cost_usd"] == 0.0
    assert snap["providers"] == ("anthropic-sdk",)
    assert snap["prompt_tokens"] == 50
    assert snap["completion_tokens"] == 10
    # The SDK response's own `model`, not the id that was requested: proves
    # the same divergence `providers` guards against is guarded on the model
    # axis too, for the one backend that never goes through `_record`'s
    # OpenRouter-shaped payload directly.
    assert snap["models"] == ("claude-haiku-4-5-20260315",)


def test_anthropic_chat_records_a_different_served_model_than_requested(monkeypatch):
    """The whole point of reading the response instead of the request: a bare
    `claude-haiku-4-5` request can be answered by a dated snapshot id. Provider
    pinning says nothing about this -- only the response's own `model` field
    does."""
    msg = _FakeMessage("hi", input_tokens=5, output_tokens=2,
                       model="claude-haiku-4-5-20261231")
    monkeypatch.setattr(cloud_client, "_get_client",
                        lambda: _FakeAnthropicClient(msg))
    cloud_client._anthropic_chat("claude-haiku-4-5", "s", "u", 0.1, None, {})
    assert cloud_client.ledger_snapshot()["models"] == ("claude-haiku-4-5-20261231",)


def test_preflight_rejects_a_missing_openrouter_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        cloud_client.preflight(["anthropic/claude-haiku-4-5"])
    assert "OPENROUTER_API_KEY" in str(excinfo.value)


def test_preflight_rejects_a_missing_anthropic_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        cloud_client.preflight(["claude-haiku-4-5"])
    assert "ANTHROPIC_API_KEY" in str(excinfo.value)


def test_preflight_ignores_local_models(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cloud_client.preflight(["qwen3-coder:30b", "qwen3.6:35b"])
