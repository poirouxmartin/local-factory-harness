"""Visual proof of the folded turn: reloading a session must not redraw every
tool blob it ever produced.

Marked `browser` -- a test that drives a real browser never blocks a push
(`.githooks/pre-push:15-18`). Run it yourself:

    py -3 -m pytest harness/tests/test_chat_recap_e2e.py -m browser

It serves its own studio on an ephemeral port rather than expecting a live one
on 8188, so it says the same thing on a clean clone as it does here.
"""

import threading
import time

import pytest

from harness.factory_web import Factory, FactoryWebServer


class _StubManager:
    """Minimal LlamaServerManager: this test never reaches the runtime."""
    def __init__(self):
        self.current = None
        self.url = "http://stub"

    def ensure(self, model):
        self.current = (model, 1)
        return self.url

    def shutdown(self):
        self.current = None


class _NoFireTimer:
    def __init__(self, delay, fn):
        self.fn = fn

    def start(self):
        pass

    def cancel(self):
        pass


@pytest.fixture
def studio(tmp_path):
    cfg = tmp_path / "factory.toml"
    cfg.write_text("[projects.demo]\npath = \"{}\"\nrunner = \"pytest\"\n".format(
        tmp_path.as_posix()), encoding="utf-8")
    f = Factory(tmp_path / "jobs", cfg, spawn_fn=lambda job_id: 4242)
    f._llama = _StubManager()
    f._llama_cfg = {"chat_idle_s": 600, "models": {}, "port": 8091}
    f.timer_fn = _NoFireTimer
    srv = FactoryWebServer(("127.0.0.1", 0), f, token="t0k3n")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, f
    srv.shutdown()
    srv.server_close()


def _seed_two_turns(factory):
    """One turn with four tool calls, one turn with none."""
    sid = factory.chats.create("m:1", kind="agent", mode="auto")["session_id"]
    t = time.time()
    msgs = [
        {"role": "user", "content": "lis les deux fichiers", "ts": t},
        {"role": "assistant", "content": "", "thinking": "il faut lire", "ts": t + 1},
        {"role": "tool", "tool_name": "read_file", "content": "x = 1", "ts": t + 2},
        {"role": "tool", "tool_name": "read_file", "content": "y = 2", "ts": t + 3},
        {"role": "tool", "tool_name": "search", "content": "3 hits", "ts": t + 4},
        {"role": "tool", "tool_name": "run_command", "content": "ok", "ts": t + 5},
        {"role": "assistant", "content": "les deux sont lus", "ts": t + 18},
        {"role": "user", "content": "merci", "ts": t + 20},
        {"role": "assistant", "content": "de rien", "ts": t + 21},
    ]
    for m in msgs:
        factory.chats.append(sid, m)
    return sid


def test_a_finished_turn_shows_one_recap_line(studio, playwright, tmp_path):
    srv, factory = studio
    sid = _seed_two_turns(factory)
    browser = playwright.chromium.launch()
    try:
        page = browser.new_page()
        page.goto("http://127.0.0.1:{}/#chat/{}".format(
            srv.server_address[1], sid))
        page.wait_for_selector(".chat-log", state="attached", timeout=5000)

        # One recap for the turn that used tools, none for the turn that did
        # not -- and it starts closed.
        recaps = page.locator(".recap > summary")
        assert recaps.count() == 1
        assert page.locator(".recap[open]").count() == 0
        assert "4 outils" in recaps.first.inner_text()

        # The four chips are in the DOM but folded away, not redrawn flat.
        assert page.locator(".recap .chat-tool").count() == 4
        assert page.locator(".chat-log > .chat-tool").count() == 0

        # What the agent said is never folded: the two replies, plus the
        # thinking-only message, which keeps its own collapsed <details>.
        assert page.locator(".chat-log > .chat-msg.assistant").count() == 3
        assert page.locator(".chat-log > .chat-msg.user").count() == 2

        # Screenshots land in tmp_path: a test that drops PNGs in the repo
        # gets them committed by accident sooner or later.
        page.screenshot(path=str(tmp_path / "recap-folded.png"), full_page=True)
        recaps.first.click()
        assert page.locator(".recap[open]").count() == 1
        page.screenshot(path=str(tmp_path / "recap-open.png"), full_page=True)
        print("screenshots: " + str(tmp_path))
    finally:
        browser.close()
