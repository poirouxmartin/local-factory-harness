"""Server-side turns: a chat/agent turn runs in its own thread and publishes
its events into a buffer. HTTP connections are viewers over that buffer.

Measured 2026-07-22 (spec: docs/design/specs/2026-07-22-server-side-turns-design.md):
while the turn was the body of the POST response, a refresh killed it -- an
agent session lost its task mid-way, after its tool effects had landed.

Stop lands in two places, and it needs both. Between events it is a flag read
by `run()`, which closes the generator. But a turn spends most of its time NOT
between events: it is blocked inside a lane, on a socket that has not sent a
token yet or on the browser worker's queue -- and there a flag nobody reads
changes nothing. `Cancellation` is the same stop, carried down to those waits:
the lane polls it and, because a flag cannot wake a read already blocked,
closes what it is blocked on.
"""
import contextlib
import threading


class TurnBusy(Exception):
    """A turn is already running for this session."""


class TurnCancelled(Exception):
    """Stop was pressed while a lane was waiting for its server.

    Raised inside the lane, so the turn unwinds through the code that owns
    what it took: the facade's `finally` persists the partial and arms the
    idle timer that gives the GPU back. Not an error -- the runner ends the
    turn on it exactly as it ends one closed between two events.
    """


class Cancellation:
    """The Stop button, as a lane sees it.

    Two operations, because a wait can be in two states. `check()`/`wait()`
    are for a lane about to block or polling in a loop. `unblocks()` is for
    the span it is ALREADY inside a blocking read: a flag cannot interrupt
    `recv`, closing the socket under it can, so the lane leaves here the way
    to do that and the stopping thread calls it.
    """

    POLL_S = 0.25       # how sharp a poll-driven lane feels the button

    def __init__(self):
        self._flag = threading.Event()
        self._lock = threading.Lock()
        self._closers = []

    def cancelled(self):
        return self._flag.is_set()

    def check(self):
        """Raise where the lane still knows how to unwind."""
        if self._flag.is_set():
            raise TurnCancelled("stopped by the operator")

    def wait(self, timeout):
        """Sleep up to `timeout`, waking early if Stop lands. True when it
        did -- so a backoff reads as `if cancel.wait(delay): give up`. The
        timeout is required: an unbounded wait on this flag is a turn that
        only ends if someone presses the button."""
        return self._flag.wait(timeout)

    def cancel(self):
        with self._lock:
            self._flag.set()
            closers = list(self._closers)
        for close in closers:
            try:
                close()
            except Exception:
                # Unblocking is best effort -- a socket that dies of being
                # closed twice must not swallow the stop. The polls still fire.
                pass

    @contextlib.contextmanager
    def unblocks(self, close):
        """Register `close` for the span of a blocking read."""
        with self._lock:
            self._closers.append(close)
            already = self._flag.is_set()
        if already:
            # Stopped between the caller's check and this registration: the
            # read is about to block on something nobody would ever close.
            close()
        try:
            yield
        finally:
            with self._lock:
                if close in self._closers:
                    self._closers.remove(close)


class Turn:
    """One running generator, its buffered events, and its readers."""

    def __init__(self, session_id, gen, mark=0, cancel=None):
        self.session_id = session_id
        # How much of the session predates this turn. A viewer that attaches
        # renders the stored transcript up to here and lets the replay rebuild
        # the rest -- otherwise the messages the turn already persisted (tool
        # results) would be drawn twice.
        self.mark = mark
        self.events = []
        self.done = False
        self.stopped = False
        self._gen = gen
        # The same token the generator was built with, when the caller made
        # one: without it Stop only lands between two events.
        self._cancel = cancel
        self._stop_requested = False
        self._cond = threading.Condition()

    def _append(self, event):
        with self._cond:
            self.events.append(event)
            self._cond.notify_all()

    def _finish(self):
        with self._cond:
            self.done = True
            self._cond.notify_all()

    def read(self, cursor=0):
        """Yield buffered events from `cursor`, then block on the live edge
        until the turn is done. One cursor per reader: any number of viewers
        can follow the same turn."""
        while True:
            with self._cond:
                while cursor >= len(self.events) and not self.done:
                    self._cond.wait()
                if cursor >= len(self.events):
                    return
                batch = self.events[cursor:]
                cursor = len(self.events)
            for event in batch:
                yield event

    def request_stop(self):
        self._stop_requested = True
        if self._cancel is not None:
            # Reaches the turn where it actually is most of the time: blocked
            # in a lane, between two events rather than at a yield.
            self._cancel.cancel()

    def run(self):
        try:
            for event in self._gen:
                self._append(event)
                if self._stop_requested:
                    # Closed from THIS thread, while the generator is suspended
                    # at its yield: closing it from the HTTP thread would race
                    # ("generator already executing"). GeneratorExit lands in
                    # the facade's finally, which persists the partial.
                    self.stopped = True
                    self._gen.close()
                    break
        except TurnCancelled:
            # The other half of the same button: the lane was blocked, saw the
            # token and unwound. It already ran the facade's finally on its way
            # out, so there is nothing to close and nothing to report -- an
            # ("error", ...) here would show the operator their own click as a
            # failure.
            self.stopped = True
        except Exception as e:
            # Nobody can turn this into an HTTP error anymore -- the viewer can
            # only learn about it as the last event of the stream.
            self._append(("error", str(e)))
        finally:
            self._finish()


class TurnRunner:
    """One turn at a time per session."""

    def __init__(self):
        self._turns = {}
        self._lock = threading.Lock()

    def get(self, session_id):
        """The session's current turn -- running, or the last finished one,
        which is kept until the next turn starts so a viewer attaching late
        still sees the tail and the terminal event."""
        with self._lock:
            return self._turns.get(session_id)

    def running(self, session_id):
        turn = self.get(session_id)
        return turn is not None and not turn.done

    def stop(self, session_id):
        """Ask the session's turn to stop. True when one was running.

        It ends at its next event, or -- when the turn was started with a
        cancellation token -- at the lane's next poll, which is what a turn
        blocked on a silent server has instead of a next event. A stop during
        a long tool call still waits for the tool: nothing here can interrupt
        a subprocess mid-write.
        """
        turn = self.get(session_id)
        if turn is None or turn.done:
            return False
        turn.request_stop()
        return True

    def start(self, session_id, gen, mark_fn=None, cancel=None):
        """Consume the generator's opening ("start", None) here, on the
        caller's thread, so a refusal is still an HTTP status code rather than
        an error buried in the stream. `mark_fn` is read at that same moment,
        before any event can be buffered. `cancel` is the token the caller
        already handed to the generator -- passing a different one, or none,
        leaves Stop working only between events."""
        with self._lock:
            current = self._turns.get(session_id)
            if current is not None and not current.done:
                raise TurnBusy("a turn is already running for " + session_id)
        kind, _ = next(gen)
        assert kind == "start", kind
        turn = Turn(session_id, gen, mark=mark_fn() if mark_fn else 0,
                    cancel=cancel)
        with self._lock:
            self._turns[session_id] = turn
        threading.Thread(target=turn.run, daemon=True).start()
        return turn
