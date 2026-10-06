"""llama-server speaks OpenAI /v1/chat/completions; the ladder speaks the
factory chat contract. This client is the adapter, measured against a real
local HTTP server (the network layer is the thing under test)."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import llama_client
import turn_runner


class FakeLlamaServer(BaseHTTPRequestHandler):
    seen = None
    reply = None

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeLlamaServer.seen = {"path": self.path, "body": body}
        out = json.dumps(FakeLlamaServer.reply).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    httpd = HTTPServer(("127.0.0.1", 0), FakeLlamaServer)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield "http://127.0.0.1:{}".format(httpd.server_port)
    httpd.shutdown()


def test_chat_maps_the_openai_reply_to_the_factory_contract(server):
    FakeLlamaServer.reply = {
        "choices": [{"message": {"content": "the code"}, "finish_reason": "stop"}],
        "timings": {"predicted_per_second": 90.5, "prompt_per_second": 1138.0,
                    "prompt_ms": 1234.0, "predicted_n": 350, "prompt_n": 87},
    }
    resp = llama_client.chat(server, "sys prompt", "user prompt",
                             temperature=0.3, num_ctx=32768,
                             options={"num_predict": 4096})
    assert resp["content"] == "the code"
    assert resp["done_reason"] == "stop"
    assert resp["eval_count"] == 350
    assert resp["tokens_per_s"] == 90.5
    # prompt_n is what a KV-cache A/B reads: cached prefix tokens are not
    # re-evaluated, so it drops. chat_stream reported it; chat silently did not.
    assert resp["prefill_count"] == 87
    assert resp["prompt_count"] == 87  # no cache_n reported: nothing was reused
    assert resp["prefill_tokens_per_s"] == 1138.0
    assert resp["ttft_s"] == pytest.approx(1.234)
    body = FakeLlamaServer.seen["body"]
    assert body["messages"][0] == {"role": "system", "content": "sys prompt"}
    assert body["messages"][1] == {"role": "user", "content": "user prompt"}
    assert body["temperature"] == 0.3
    assert body["max_tokens"] == 4096


def test_chat_forwards_the_sampling_profile(server):
    # Without this, llama-server falls back to llama.cpp defaults (top_p .95,
    # top_k 40, min_p .05) -- measured to re-drown diff replies (j_575efd4c).
    FakeLlamaServer.reply = {
        "choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
        "timings": {"predicted_per_second": 1.0, "prompt_per_second": 1.0,
                    "prompt_ms": 10.0, "predicted_n": 1},
    }
    llama_client.chat(server, "s", "u",
                      options={"top_p": 0.8, "top_k": 20, "min_p": 0,
                               "presence_penalty": 1.0, "num_ctx": 4096})
    body = FakeLlamaServer.seen["body"]
    assert body["top_p"] == 0.8 and body["top_k"] == 20 and body["min_p"] == 0
    assert body["presence_penalty"] == 1.0
    assert "num_ctx" not in body  # server-side flag, never a request field


def test_chat_maps_length_finish_reason(server):
    FakeLlamaServer.reply = {
        "choices": [{"message": {"content": "trunca"}, "finish_reason": "length"}],
        "timings": {"predicted_per_second": 1.0, "prompt_per_second": 1.0,
                    "prompt_ms": 10.0, "predicted_n": 4096},
    }
    resp = llama_client.chat(server, "s", "u")
    assert resp["done_reason"] == "length"


def test_chat_without_num_predict_omits_max_tokens(server):
    FakeLlamaServer.reply = {
        "choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
        "timings": {"predicted_per_second": 1.0, "prompt_per_second": 1.0,
                    "prompt_ms": 10.0, "predicted_n": 1},
    }
    llama_client.chat(server, "s", "u")
    assert "max_tokens" not in FakeLlamaServer.seen["body"]


# ---- chat_stream (SSE) ----

class FakeSseServer(BaseHTTPRequestHandler):
    seen = None
    chunks = None  # list of dicts to stream as "data: ..." lines

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeSseServer.seen = {"path": self.path, "body": body}
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for c in FakeSseServer.chunks:
            self.wfile.write(b"data: " + json.dumps(c).encode("utf-8") + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *a):
        pass


class _QuietHTTPServer(HTTPServer):
    """A test that stops reading mid-stream (chat_stream closes its generator)
    leaves this fake writing into a dead socket. That is the scenario under
    test, not a failure: printing socketserver's traceback for it polluted the
    suite's stderr and made real errors easy to miss."""

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)


@pytest.fixture
def sse_server():
    httpd = _QuietHTTPServer(("127.0.0.1", 0), FakeSseServer)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield "http://127.0.0.1:{}".format(httpd.server_port)
    httpd.shutdown()


def _drain(gen):
    events, metrics = [], None
    while True:
        try:
            events.append(next(gen))
        except StopIteration as stop:
            metrics = stop.value
            break
    return events, metrics


def test_stream_yields_deltas_and_maps_timings(sse_server):
    FakeSseServer.chunks = [
        {"choices": [{"delta": {"reasoning_content": "hmm"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "hel"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}],
         "timings": {"predicted_per_second": 60.0, "prompt_per_second": 900.0,
                     "prompt_ms": 500.0, "predicted_n": 2, "prompt_n": 40}},
    ]
    events, m = _drain(llama_client.chat_stream(
        sse_server, [{"role": "user", "content": "hi"}]))
    assert ("thinking", "hmm") in events
    assert ("content", "hel") in events and ("content", "lo") in events
    assert m["content"] == "hello"
    assert m["done_reason"] == "stop"
    assert m["tokens_per_s"] == 60.0
    assert m["prefill_tokens_per_s"] == 900.0
    assert m["prefill_count"] == 40
    assert m["eval_count"] == 2
    assert m["ttft_s"] == pytest.approx(0.5)
    assert m["thinking_chars"] == 3 and m["content_chars"] == 5
    assert FakeSseServer.seen["body"]["stream"] is True


def test_stream_counts_the_cached_prefix_in_the_prompt_size(sse_server):
    """`prompt_n` is what the server EVALUATED, not what the prompt weighs.

    On a stable prefix llama-server re-evaluates almost nothing: measured
    2026-07-22 against the live server, a 1103-token prompt came back as
    `prompt_n: 1, cache_n: 1102` (`usage.prompt_tokens: 1103` agrees). Every
    consumer that read prefill_count as "how full is the window" therefore
    collapsed to ~0 the moment the cache hit -- which is every turn after the
    first, in exactly the long sessions where the answer matters.

    prefill_count keeps its meaning (prefill tok/s is computed against it);
    the size of the prompt is its own field.
    """
    FakeSseServer.chunks = [
        {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}],
         "timings": {"predicted_n": 2, "prompt_n": 1, "cache_n": 1102}},
    ]
    _, m = _drain(llama_client.chat_stream(
        sse_server, [{"role": "user", "content": "hi"}]))
    assert m["prefill_count"] == 1
    assert m["prompt_count"] == 1103


def test_stream_accumulates_fragmented_tool_calls(sse_server):
    FakeSseServer.chunks = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"name": "read_file", "arguments": "{\"pa"}}]},
            "finish_reason": None}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "th\": \"a.py\"}"}}]},
            "finish_reason": "tool_calls"}],
         "timings": {"predicted_n": 5}},
    ]
    events, m = _drain(llama_client.chat_stream(
        sse_server, [{"role": "user", "content": "go"}],
        tools=[{"type": "function", "function": {"name": "read_file"}}]))
    assert ("tool_call", {"name": "read_file",
                          "arguments": {"path": "a.py"}}) in events
    assert m["done_reason"] == "tool_calls"
    assert FakeSseServer.seen["body"]["tools"][0]["function"]["name"] == "read_file"


def test_stream_maps_wire_history_to_openai(sse_server):
    FakeSseServer.chunks = [
        {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}]
    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "run_command",
                                      "arguments": {"cmd": "dir"}}}]},
        {"role": "tool", "tool_name": "run_command", "content": "listing"},
        {"role": "tool", "tool_name": "system", "content": "notice"},
    ]
    _drain(llama_client.chat_stream(sse_server, history))
    sent = FakeSseServer.seen["body"]["messages"]
    assert sent[2]["tool_calls"][0]["function"]["arguments"] == "{\"cmd\": \"dir\"}"
    assert sent[2]["tool_calls"][0]["id"] == sent[3]["tool_call_id"]
    # orphan tool note (no outstanding call id): becomes a plain user note,
    # strict jinja templates reject tool messages without a matching call
    assert sent[4] == {"role": "user", "content": "[system] notice"}


def test_stream_marks_length_and_repetition_stops(sse_server):
    FakeSseServer.chunks = [
        {"choices": [{"delta": {"content": "cut"}, "finish_reason": "length"}],
         "timings": {"predicted_n": 4096}}]
    _, m = _drain(llama_client.chat_stream(
        sse_server, [{"role": "user", "content": "x"}]))
    assert m["stopped"] == "length" and m["done_reason"] == "length"

    line = "Je vais lancer le test.\n"
    FakeSseServer.chunks = [
        {"choices": [{"delta": {"content": line * 60}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "never seen"}, "finish_reason": "stop"}]},
    ]
    _, m = _drain(llama_client.chat_stream(
        sse_server, [{"role": "user", "content": "x"}]))
    assert m["stopped"] == "repetition"


# ---- text tool-call recovery (qwen3-coder emits XML, not tool_calls) ----

# The exact bytes qwen3-coder:30b returns under llama-server --jinja: the call
# arrives as content text, finish_reason "stop", tool_calls empty (confirmed
# 2026-07-19). Without recovery the agent treats it as a final answer and dies.
QWEN3_CODER_XML = (
    "I'll run the shell command to check the Python version.\n\n"
    "<function=run_command>\n"
    "<parameter=command>\n"
    "python --version\n"
    "</parameter>\n"
    "</function>\n"
    "</tool_call>")


def test_parse_recovers_a_qwen3_coder_xml_tool_call():
    calls, cleaned = llama_client.parse_text_tool_calls(QWEN3_CODER_XML)
    assert calls == [{"name": "run_command",
                      "arguments": {"command": "python --version"}}]
    # the preamble survives; the raw call is stripped from the saved content
    assert cleaned == "I'll run the shell command to check the Python version."


def test_parse_coerces_numeric_params_and_keeps_strings():
    text = ("<function=read_file>"
            "<parameter=path>src/a.py</parameter>"
            "<parameter=offset>10</parameter>"
            "<parameter=limit>-5</parameter>"
            "</function>")
    calls, _ = llama_client.parse_text_tool_calls(text)
    assert calls == [{"name": "read_file",
                      "arguments": {"path": "src/a.py", "offset": 10,
                                    "limit": -5}}]


# ---- a parameter that is not a scalar --------------------------------------

# What ChatGPT actually wrote for set_plan on session c_14f20c89 (2026-08-06).
# The XML protocol carries no type, so `steps` -- an array of objects -- came
# back as one string; nothing downstream noticed, and carnet.parse_plan
# iterated it CHARACTER BY CHARACTER. The plan panel showed "0/36" over one
# letter per row. The tool schema is the only place the type exists, so the
# parser has to be given it.
_PLAN_TOOLS = [{"type": "function", "function": {
    "name": "set_plan",
    "parameters": {"type": "object", "properties": {
        "goal": {"type": "string"},
        "steps": {"type": "array", "items": {"type": "object"}}},
        "required": ["goal", "steps"]}}}]


def test_parse_reads_an_array_param_as_json_when_the_schema_says_array():
    text = ("<function=set_plan>"
            "<parameter=goal>ship it</parameter>"
            '<parameter=steps>[{"step":"read a.py"},{"step":"build"}]</parameter>'
            "</function>")
    calls, _ = llama_client.parse_text_tool_calls(text, _PLAN_TOOLS)
    assert calls[0]["arguments"]["steps"] == [{"step": "read a.py"},
                                              {"step": "build"}]


def test_parse_splits_a_prose_array_param_instead_of_its_characters():
    # The model wrote the list as a numbered sentence. Splitting it wrong is
    # survivable; handing a string to code that expects a list is not.
    text = ("<function=set_plan>"
            "<parameter=goal>ship it</parameter>"
            "<parameter=steps>1. Inspecter Application.h;2. Compiler"
            "</parameter></function>")
    calls, _ = llama_client.parse_text_tool_calls(text, _PLAN_TOOLS)
    assert calls[0]["arguments"]["steps"] == ["1. Inspecter Application.h",
                                              "2. Compiler"]


def test_parse_leaves_a_declared_string_param_alone():
    # `2024` typed as a string stays a string: the schema outranks the shape.
    tools = [{"type": "function", "function": {
        "name": "write_file",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}}}}}]
    text = ("<function=write_file><parameter=path>y.txt</parameter>"
            "<parameter=content>2024</parameter></function>")
    calls, _ = llama_client.parse_text_tool_calls(text, tools)
    assert calls[0]["arguments"]["content"] == "2024"


def test_parse_without_tools_keeps_the_old_coercion():
    # Every other caller passes no schema; nothing about them changes.
    text = ("<function=set_plan><parameter=steps>a;b</parameter></function>")
    calls, _ = llama_client.parse_text_tool_calls(text)
    assert calls[0]["arguments"]["steps"] == "a;b"


def test_parse_recovers_multiple_calls():
    text = ("<function=read_file><parameter=path>a</parameter></function>"
            "<function=read_file><parameter=path>b</parameter></function>")
    calls, _ = llama_client.parse_text_tool_calls(text)
    assert [c["arguments"]["path"] for c in calls] == ["a", "b"]


def test_parse_recovers_a_hermes_json_tool_call():
    text = ('reflexion\n<tool_call>\n'
            '{"name": "run_command", "arguments": {"command": "ls"}}\n'
            '</tool_call>')
    calls, cleaned = llama_client.parse_text_tool_calls(text)
    assert calls == [{"name": "run_command", "arguments": {"command": "ls"}}]
    assert cleaned == "reflexion"


def test_parse_returns_nothing_on_plain_text():
    calls, cleaned = llama_client.parse_text_tool_calls("just a normal answer.")
    assert calls == [] and cleaned == "just a normal answer."


def test_has_tool_call_markers_flags_orphan_closers():
    # A thinking model (qwen3.6:35b) left only the closing tags in its
    # reasoning; parse_text_tool_calls can't recover them, but this is still
    # an ATTEMPTED call, not a final answer.
    orphan = "Let me check the venv.\n</parameter>\n</function>\n</tool_call>"
    assert llama_client.has_tool_call_markers(orphan)
    assert llama_client.has_tool_call_markers("<function=read_file>")
    assert llama_client.has_tool_call_markers("<tool_call>")


def test_has_tool_call_markers_ignores_a_plain_answer():
    assert not llama_client.has_tool_call_markers("Done, the file is fixed.")
    assert not llama_client.has_tool_call_markers("")


def test_stream_forwards_sampling_and_num_predict(sse_server):
    FakeSseServer.chunks = [
        {"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]}]
    _drain(llama_client.chat_stream(
        sse_server, [{"role": "user", "content": "x"}], temperature=0.7,
        options={"top_p": 0.8, "num_predict": 4096, "num_ctx": 16384}))
    body = FakeSseServer.seen["body"]
    assert body["temperature"] == 0.7 and body["top_p"] == 0.8
    assert body["max_tokens"] == 4096
    assert "num_ctx" not in body and "num_predict" not in body


# ---- Stop, while the read is still waiting ----

class StallingSseServer(BaseHTTPRequestHandler):
    """Answers the headers, then says nothing -- a prefill that never lands.

    This is the shape of the wait Stop could not reach: the connection is up,
    the client is inside a socket read, and no event will come to hang a flag
    check on for as long as the timeout allows.
    """
    opened = threading.Event()
    release = threading.Event()

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.flush()
        StallingSseServer.opened.set()
        StallingSseServer.release.wait(20)

    def log_message(self, *a):
        pass


@pytest.fixture
def stalling_server():
    StallingSseServer.opened.clear()
    StallingSseServer.release.clear()
    httpd = _QuietHTTPServer(("127.0.0.1", 0), StallingSseServer)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield "http://127.0.0.1:{}".format(httpd.server_port)
    StallingSseServer.release.set()
    httpd.shutdown()


def _in_a_thread(gen):
    """Drive a lane the way a turn does: on its own thread, so the test can be
    the one pressing Stop."""
    box = {}

    def run():
        try:
            for _ in gen:
                pass
        except BaseException as e:                # noqa: BLE001 - reported
            box["raised"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, box


def test_stop_reaches_a_read_still_waiting_for_the_first_token(stalling_server):
    cancel = turn_runner.Cancellation()
    t, box = _in_a_thread(llama_client.chat_stream(
        stalling_server, [{"role": "user", "content": "hi"}], cancel=cancel))
    assert StallingSseServer.opened.wait(5)   # in the read, with nothing to read

    cancel.cancel()

    t.join(5)
    # The timeout for this call is 900 s: a thread still alive here is a Stop
    # the operator would have watched do nothing.
    assert not t.is_alive()
    assert isinstance(box.get("raised"), turn_runner.TurnCancelled)


def test_a_socket_closed_by_stop_is_not_reported_as_a_dead_server(stalling_server):
    # The read fails because we broke it. Told as "llama-server est parti", the
    # operator would go looking for a crash that did not happen.
    cancel = turn_runner.Cancellation()
    t, box = _in_a_thread(llama_client.chat_stream(
        stalling_server, [{"role": "user", "content": "hi"}], cancel=cancel))
    assert StallingSseServer.opened.wait(5)
    cancel.cancel()
    t.join(5)
    assert not isinstance(box.get("raised"), RuntimeError)


def test_without_a_token_the_stream_reads_exactly_as_before(sse_server):
    FakeSseServer.chunks = [
        {"choices": [{"delta": {"content": "hello"}, "finish_reason": "stop"}]}]
    events, metrics = _drain(llama_client.chat_stream(
        sse_server, [{"role": "user", "content": "hi"}]))
    assert events == [("content", "hello")]
    assert metrics["content"] == "hello"


def test_a_stop_between_two_deltas_ends_the_stream(sse_server):
    # The other half: the token is also read where events DO flow, so a stop
    # mid-answer does not wait for the next one either.
    FakeSseServer.chunks = [
        {"choices": [{"delta": {"content": "un"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "deux"}, "finish_reason": "stop"}]}]
    cancel = turn_runner.Cancellation()
    gen = llama_client.chat_stream(sse_server, [{"role": "user", "content": "x"}],
                                   cancel=cancel)
    assert next(gen) == ("content", "un")
    cancel.cancel()
    with pytest.raises(turn_runner.TurnCancelled):
        next(gen)


def test_tokenize_returns_the_server_token_count(server):
    FakeLlamaServer.reply = {"tokens": [100, 200, 300, 400]}
    assert llama_client.tokenize(server, "quatre tokens ici") == 4
    assert FakeLlamaServer.seen["path"] == "/tokenize"
    assert FakeLlamaServer.seen["body"] == {"content": "quatre tokens ici"}


def test_tokenize_of_the_empty_string_is_zero_without_a_call(server):
    FakeLlamaServer.seen = None
    assert llama_client.tokenize(server, "") == 0
    assert FakeLlamaServer.seen is None
