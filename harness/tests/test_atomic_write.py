"""Atomic write primitives.

The whole store rests on one claim: a reader never sees a half-written JSON.
On Windows that claim is not free -- os.replace raises PermissionError if the
destination is open by anyone, so the writer must retry. These tests pin both
halves: the happy path leaves no temp behind, and a transient reader does not
lose the write.
"""
import json
import os
import threading
import time

import pytest

from job_store import atomic_write_json, read_json

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")


def test_atomic_write_json_roundtrips(tmp_path):
    dst = tmp_path / "state.json"

    atomic_write_json(dst, {"status": "running", "attempt": 3})

    assert read_json(dst) == {"status": "running", "attempt": 3}


def test_atomic_write_json_leaves_no_temp_file_behind(tmp_path):
    dst = tmp_path / "state.json"

    atomic_write_json(dst, {"a": 1})

    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_atomic_write_json_overwrites_existing(tmp_path):
    dst = tmp_path / "state.json"
    atomic_write_json(dst, {"attempt": 1})

    atomic_write_json(dst, {"attempt": 2})

    assert read_json(dst) == {"attempt": 2}


def test_read_json_does_not_hold_the_file_open(tmp_path):
    dst = tmp_path / "state.json"
    atomic_write_json(dst, {"a": 1})

    read_json(dst)

    # If read_json leaked a handle, this replace would fail on Windows.
    atomic_write_json(dst, {"a": 2})
    assert read_json(dst) == {"a": 2}


@windows_only
def test_atomic_write_json_retries_while_a_reader_holds_the_destination(tmp_path):
    dst = tmp_path / "state.json"
    atomic_write_json(dst, {"attempt": 1})

    holder = open(dst, "r")
    done = []

    def writer():
        atomic_write_json(dst, {"attempt": 2}, timeout=5.0)
        done.append(True)

    t = threading.Thread(target=writer)
    t.start()
    time.sleep(0.2)  # writer is now spinning on PermissionError
    assert not done, "write should not have succeeded while the reader holds the file"
    holder.close()
    t.join(timeout=5)

    assert done == [True]
    assert read_json(dst) == {"attempt": 2}


@windows_only
def test_atomic_write_json_gives_up_and_raises_when_never_released(tmp_path):
    dst = tmp_path / "state.json"
    atomic_write_json(dst, {"attempt": 1})

    with open(dst, "r"):
        with pytest.raises(PermissionError):
            atomic_write_json(dst, {"attempt": 2}, timeout=0.3)


@windows_only
def test_failed_write_leaves_the_previous_value_intact_and_no_temp(tmp_path):
    dst = tmp_path / "state.json"
    atomic_write_json(dst, {"attempt": 1})

    with open(dst, "r"):
        with pytest.raises(PermissionError):
            atomic_write_json(dst, {"attempt": 2}, timeout=0.2)

    assert json.loads(dst.read_text()) == {"attempt": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]
