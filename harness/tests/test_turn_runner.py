"""The turn runner: a chat/agent turn lives server-side, not in the HTTP
connection that started it.

Boundary under test: a generator in, a buffer of events out, read by any number
of cursors. The generators here are plain fakes -- chat_reply/agent_reply are
exercised at the facade level."""
import threading
import time

import pytest

from turn_runner import Cancellation, TurnBusy, TurnCancelled, TurnRunner


def events(*kinds):
    """A generator with the factory's contract: ("start", None) first."""
    def gen():
        yield "start", None
        for k in kinds:
            yield "chunk", k
        yield "done", {"ok": True}
    return gen()


def wait_until(pred, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


class Gated:
    """A generator the test drives event by event."""

    def __init__(self):
        self.go = threading.Event()
        self.emitted = threading.Event()

    def gen(self):
        yield "start", None
        while True:
            self.go.wait()
            self.go.clear()
            if self.stop_after:
                yield "done", {}
                return
            yield "chunk", self.next_text
            self.emitted.set()

    stop_after = False
    next_text = ""

    def emit(self, text):
        self.next_text = text
        self.emitted.clear()
        self.go.set()
        assert self.emitted.wait(2.0)

    def finish(self):
        self.stop_after = True
        self.go.set()


def test_turn_runs_to_completion_without_any_reader():
    # The measured bug: the turn died when the browser went away. Nobody reads
    # this one at all, and it must still finish and buffer everything.
    runner = TurnRunner()
    turn = runner.start("c_1", events("a", "b", "c"))
    assert wait_until(lambda: turn.done)
    assert [e for e in turn.events] == [("chunk", "a"), ("chunk", "b"),
                                        ("chunk", "c"), ("done", {"ok": True})]


def test_reader_replays_from_its_cursor_then_follows_the_live_turn():
    # What a refresh does: attach at 0, get everything already said, then keep
    # receiving. The reader must block on the live edge, not spin or end early.
    runner = TurnRunner()
    g = Gated()
    turn = runner.start("c_1", g.gen())
    g.emit("deja dit")

    seen = []
    reader = threading.Thread(target=lambda: seen.extend(turn.read(0)))
    reader.start()
    assert wait_until(lambda: seen == [("chunk", "deja dit")])

    g.emit("en direct")
    assert wait_until(lambda: len(seen) == 2)
    g.finish()
    reader.join(2.0)
    assert not reader.is_alive()  # `done` ends the read
    assert seen == [("chunk", "deja dit"), ("chunk", "en direct"), ("done", {})]


def test_a_reader_that_leaves_does_not_take_the_turn_with_it():
    # Guard on the whole point: closing the viewer's generator (what a dropped
    # HTTP connection does) must not reach the turn.
    runner = TurnRunner()
    g = Gated()
    turn = runner.start("c_1", g.gen())
    view = turn.read(0)
    g.emit("un")
    assert next(view) == ("chunk", "un")
    view.close()  # the browser goes away

    g.emit("deux")
    g.finish()
    assert wait_until(lambda: turn.done)
    assert turn.events == [("chunk", "un"), ("chunk", "deux"), ("done", {})]


def test_second_turn_on_a_busy_session_is_refused():
    runner = TurnRunner()
    g = Gated()
    runner.start("c_1", g.gen())
    with pytest.raises(TurnBusy):
        runner.start("c_1", events("x"))
    g.finish()


def test_a_session_accepts_a_new_turn_once_the_previous_one_ended():
    runner = TurnRunner()
    first = runner.start("c_1", events("x"))
    assert wait_until(lambda: first.done)
    second = runner.start("c_1", events("y"))
    assert wait_until(lambda: second.done)
    assert runner.get("c_1") is second


def test_another_session_runs_in_parallel():
    runner = TurnRunner()
    a, b = Gated(), Gated()
    ta = runner.start("c_1", a.gen())
    tb = runner.start("c_2", b.gen())
    a.emit("a1")
    b.emit("b1")
    a.finish()
    b.finish()
    assert wait_until(lambda: ta.done and tb.done)
    assert ta.events[0] == ("chunk", "a1")
    assert tb.events[0] == ("chunk", "b1")


def test_stop_closes_the_generator_so_the_facade_saves_its_partial():
    # The Stop button used to work only because dropping the connection killed
    # the turn. Now it is a signal, and it must land in the generator's finally
    # -- that is where chat_reply persists the partial.
    closed = []

    def gen():
        yield "start", None
        try:
            while True:
                yield "chunk", "."
        except GeneratorExit:
            closed.append("interrupted")
            raise

    runner = TurnRunner()
    turn = runner.start("c_1", gen())
    assert wait_until(lambda: len(turn.events) > 2)
    runner.stop("c_1")
    assert wait_until(lambda: turn.done)
    assert closed == ["interrupted"]
    assert turn.stopped


def test_stop_reaches_a_turn_asleep_inside_its_lane():
    # The hole the token fills: between two events, Stop is a flag `run()`
    # reads. A turn waiting for a first token yields nothing at all, and the
    # flag was read by nobody until the server spoke -- up to TIMEOUT away.
    lane_entered = threading.Event()
    unwound = []
    cancel = Cancellation()

    def gen():
        yield "start", None
        try:
            lane_entered.set()
            cancel.wait(30)      # what a lane does: block on its server
            cancel.check()       # ... and read the token where it wakes up
            yield "chunk", "jamais"
        finally:
            unwound.append("partial saved")

    runner = TurnRunner()
    turn = runner.start("c_1", gen(), cancel=cancel)
    assert lane_entered.wait(2.0)
    assert not turn.events               # nothing to hang a stop on

    runner.stop("c_1")
    assert wait_until(lambda: turn.done)
    assert unwound == ["partial saved"]  # the facade's finally ran
    assert turn.stopped


def test_a_stop_from_inside_the_lane_is_not_an_error_event():
    # It is the operator's own click. Reported as ("error", ...) it would paint
    # the studio red for having done what was asked.
    cancel = Cancellation()

    def gen():
        yield "start", None
        yield "chunk", "un début"
        cancel.wait(30)
        cancel.check()

    runner = TurnRunner()
    turn = runner.start("c_1", gen(), cancel=cancel)
    assert wait_until(lambda: turn.events == [("chunk", "un début")])
    runner.stop("c_1")
    assert wait_until(lambda: turn.done)
    assert turn.events == [("chunk", "un début")]   # no terminal error
    assert turn.stopped


def test_a_turn_without_a_token_still_stops_between_two_events():
    # The old path stays: nothing but the web layer hands out tokens, and a
    # caller that does not must not lose the Stop button it already had.
    runner = TurnRunner()
    g = Gated()
    turn = runner.start("c_1", g.gen())      # no cancel=
    g.emit("un")
    runner.stop("c_1")
    g.next_text = "deux"
    g.go.set()          # not emit(): this yield is where the close lands
    assert wait_until(lambda: turn.done)
    assert turn.stopped


def test_the_token_closes_what_the_lane_is_blocked_on():
    # A flag cannot wake a socket already inside recv. The lane leaves the way
    # to break it here, and Stop is what calls it.
    cancel = Cancellation()
    closed = []
    with cancel.unblocks(lambda: closed.append("socket")):
        assert closed == []
        cancel.cancel()
        assert closed == ["socket"]
    cancel.cancel()
    assert closed == ["socket"]  # unregistered on the way out, not called twice


def test_a_token_cancelled_before_the_lane_arms_it_still_breaks_the_read():
    # The race the studio actually produces: Stop lands between the lane's
    # check and its registration. Nothing would ever call the closer, and the
    # read would wait out its timeout with the button already pressed.
    cancel = Cancellation()
    closed = []
    cancel.cancel()
    with cancel.unblocks(lambda: closed.append("socket")):
        assert closed == ["socket"]


def test_a_closer_that_fails_does_not_swallow_the_stop():
    cancel = Cancellation()
    closed = []
    with cancel.unblocks(lambda: (_ for _ in ()).throw(OSError("already gone"))):
        with cancel.unblocks(lambda: closed.append("second")):
            cancel.cancel()
    assert closed == ["second"]
    assert cancel.cancelled()
    with pytest.raises(TurnCancelled):
        cancel.check()


def test_stopping_an_idle_session_is_a_no_op():
    runner = TurnRunner()
    assert runner.stop("c_unknown") is False
    turn = runner.start("c_1", events("x"))
    assert wait_until(lambda: turn.done)
    assert runner.stop("c_1") is False


def test_a_generator_that_raises_ends_the_turn_with_a_terminal_error():
    # Headers are long gone by then: the viewer can only learn about it as an
    # event, and the turn must still be marked done or readers hang forever.
    def gen():
        yield "start", None
        yield "chunk", "un peu"
        raise RuntimeError("llama-server est parti")

    runner = TurnRunner()
    turn = runner.start("c_1", gen())
    assert wait_until(lambda: turn.done)
    assert turn.events == [("chunk", "un peu"),
                           ("error", "llama-server est parti")]


def test_the_turn_marks_where_it_starts_in_the_session():
    # A viewer that attaches after a refresh renders the transcript up to the
    # mark and lets the replay rebuild the rest. Without it, the tool messages
    # the turn already persisted would be drawn twice: once from the store,
    # once from the replayed events.
    calls = []
    g = Gated()
    runner = TurnRunner()
    turn = runner.start("c_1", g.gen(), mark_fn=lambda: calls.append(1) or 7)
    assert turn.mark == 7
    g.emit("apres")
    assert calls == [1]  # taken once, at the start, not per event
    g.finish()


def test_the_mark_is_taken_before_any_event_is_buffered():
    marks = []

    def gen():
        yield "start", None
        marks.append("event")
        yield "chunk", "x"

    runner = TurnRunner()
    runner.start("c_1", gen(), mark_fn=lambda: marks.append("mark") or 0)
    assert wait_until(lambda: len(marks) >= 2)
    assert marks[0] == "mark"


def test_a_late_reader_still_sees_a_finished_turn():
    runner = TurnRunner()
    turn = runner.start("c_1", events("a", "b"))
    assert wait_until(lambda: turn.done)
    assert list(turn.read(0)) == [("chunk", "a"), ("chunk", "b"),
                                  ("done", {"ok": True})]
