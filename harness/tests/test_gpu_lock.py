"""GPU lock contract.

Two jobs cannot share the GPU: the 35b takes ~20 of the machine's 31 GB. The lock
serializes them. Two properties matter:

  - acquire() must NOT block the thread, or a job queued behind the GPU stops
    heartbeating and job_status() reports it as dead (it isn't -- it is waiting).
  - the lock must be released by the OS when the holder dies, because there is no
    daemon to clean up after a killed job.
"""
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from gpu_lock import GpuLock

HARNESS = str(Path(__file__).resolve().parent.parent)


def test_acquire_succeeds_when_the_lock_is_free(tmp_path):
    lock = GpuLock(tmp_path / "gpu.lock")

    assert lock.acquire(timeout=0) is True
    lock.release()


def test_a_second_holder_is_refused(tmp_path):
    first = GpuLock(tmp_path / "gpu.lock")
    first.acquire(timeout=0)

    second = GpuLock(tmp_path / "gpu.lock")
    assert second.acquire(timeout=0) is False

    first.release()


def test_release_hands_the_lock_to_the_next_holder(tmp_path):
    first = GpuLock(tmp_path / "gpu.lock")
    first.acquire(timeout=0)
    first.release()

    second = GpuLock(tmp_path / "gpu.lock")
    assert second.acquire(timeout=0) is True
    second.release()


def test_context_manager_releases_on_exit(tmp_path):
    with GpuLock(tmp_path / "gpu.lock"):
        pass

    assert GpuLock(tmp_path / "gpu.lock").acquire(timeout=0) is True


def test_waiting_calls_back_so_the_caller_can_keep_heartbeating(tmp_path):
    held = GpuLock(tmp_path / "gpu.lock")
    held.acquire(timeout=0)
    beats = []

    waiter = GpuLock(tmp_path / "gpu.lock")
    acquired = waiter.acquire(timeout=0.3, poll=0.05, on_wait=lambda: beats.append(1))

    assert acquired is False
    assert len(beats) >= 2, "the waiter must get control back while it waits"
    held.release()


def test_waiting_acquires_once_the_holder_lets_go(tmp_path):
    lock_path = tmp_path / "gpu.lock"
    held = GpuLock(lock_path)
    held.acquire(timeout=0)
    released_at = [None]

    def release_once():
        if released_at[0] is None:
            released_at[0] = time.monotonic()
            held.release()

    waiter = GpuLock(lock_path)
    assert waiter.acquire(timeout=2.0, poll=0.05, on_wait=release_once) is True
    waiter.release()


def test_the_lock_survives_nothing_when_the_holder_is_killed(tmp_path):
    """No daemon reaps dead jobs; the OS must drop the lock, or the queue wedges."""
    lock_path = tmp_path / "gpu.lock"
    ready = tmp_path / "ready"
    child_src = textwrap.dedent(f"""
        import pathlib, sys, time
        sys.path.insert(0, {HARNESS!r})
        from gpu_lock import GpuLock
        lock = GpuLock({str(lock_path)!r})
        assert lock.acquire(timeout=0)
        pathlib.Path({str(ready)!r}).write_text("held")
        time.sleep(60)
    """)
    child = subprocess.Popen([sys.executable, "-c", child_src])
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "child never took the lock"
        assert GpuLock(lock_path).acquire(timeout=0) is False, "child should hold it"

        child.kill()
        child.wait(timeout=10)

        survivor = GpuLock(lock_path)
        assert survivor.acquire(timeout=5.0, poll=0.05) is True
        survivor.release()
    finally:
        if child.poll() is None:
            child.kill()
