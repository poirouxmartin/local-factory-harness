"""One GPU, one job.

An advisory whole-file lock that queues jobs without a scheduler. Acquisition is
non-blocking + polled rather than a blocking flock(), so the caller keeps control
between attempts and can go on writing its heartbeat while it waits. A job that
blocks silently on the GPU is indistinguishable from a job that died.

The OS drops the lock when the holding process exits, however it exits. That is
what lets the design get away with having no daemon: a killed job un-queues itself.
"""
import os
import time
from pathlib import Path

if os.name == "nt":
    import msvcrt

    def _try_lock(fh):
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(fh):
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class GpuLock:
    def __init__(self, path):
        self.path = Path(path)
        self._fh = None

    @property
    def acquired(self):
        return self._fh is not None

    def acquire(self, timeout=0, poll=0.5, on_wait=None):
        """Try to take the lock, polling until `timeout`. Returns whether we got it.

        `on_wait` is called once per failed attempt -- that is the runner's slot to
        emit a heartbeat.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        deadline = time.monotonic() + timeout
        while True:
            try:
                _try_lock(fh)
                self._fh = fh
                return True
            except OSError:
                pass
            if time.monotonic() >= deadline:
                fh.close()
                return False
            if on_wait is not None:
                on_wait()
            time.sleep(poll)

    def release(self):
        if self._fh is None:
            return
        try:
            _unlock(self._fh)
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self):
        if not self.acquire(timeout=0):
            raise RuntimeError("GPU lock is held by another job")
        return self

    def __exit__(self, *exc):
        self.release()
