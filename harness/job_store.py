"""The filesystem is the database.

A job is a directory. Every write goes through a temp file plus os.replace, so a
reader never observes a half-written JSON -- even if the job is killed mid-write.

Windows caveat, measured not assumed: os.replace raises PermissionError
[WinError 5] when the destination is open by *anyone*, because CPython does not
open files with FILE_SHARE_DELETE. A concurrent job_status() read is enough to
break a heartbeat write. Hence the retry loop.
"""
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPLACE_TIMEOUT = 10.0  # seconds a writer will spin waiting for a reader to let go
_RETRY_SLEEP = 0.01

# A runner beats every 30 s. Three missed beats and we call it dead: nothing else
# will, because there is no daemon.
HEARTBEAT_TTL = 90.0

# States in which a live process is expected to be beating.
ACTIVE_STATES = ("queued", "waiting_gpu", "running")


class JobStoreError(Exception):
    pass


def seek_stdout_to_end():
    """Point fd 1 at the file's real end before a runner's final print.

    A spawned job inherits log.txt as a raw OS handle whose position is byte 0,
    and Windows keeps no append flag on an inherited handle -- meanwhile
    append_log has been growing the file through its own handles. Printing
    without this seek overwrites the run narrative with the result JSON
    (observed on j_d85cd85b: the AnchorNotFound diagnostics were destroyed).
    A console or pipe stdout is unseekable: nothing to do there.
    """
    try:
        sys.stdout.flush()
        os.lseek(1, 0, os.SEEK_END)
    except OSError:
        pass


class JobExists(JobStoreError):
    pass


class JobNotFound(JobStoreError):
    pass


class ResultExists(JobStoreError):
    pass


def _replace_with_retry(src, dst, timeout):
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_RETRY_SLEEP)


def atomic_write_bytes(path, data, timeout=REPLACE_TIMEOUT):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(tmp, str(path), timeout)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def atomic_write_json(path, obj, timeout=REPLACE_TIMEOUT):
    payload = json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8")
    atomic_write_bytes(path, payload, timeout=timeout)


def read_json(path):
    """Read and close immediately: a lingering handle would break a concurrent write."""
    return json.loads(Path(path).read_bytes().decode("utf-8"))


class JobStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def job_dir(self, job_id):
        return self.root / job_id

    def create(self, spec, job_id=None, now=None):
        job_id = job_id or "j_" + uuid.uuid4().hex[:8]
        now = time.time() if now is None else now
        try:
            # mkdir is the exclusion primitive: it fails if the job already exists,
            # so spec.json is written exactly once and never rewritten.
            self.job_dir(job_id).mkdir(parents=True)
        except FileExistsError:
            raise JobExists(job_id)
        atomic_write_json(self.job_dir(job_id) / "spec.json", spec)
        atomic_write_json(self.job_dir(job_id) / "state.json",
                          {"status": "queued", "created_at": now})
        self.heartbeat(job_id, now=now)  # a slow spawn must not read as a dead job
        return job_id

    def _read(self, job_id, name):
        try:
            return read_json(self.job_dir(job_id) / name)
        except FileNotFoundError:
            raise JobNotFound(job_id)

    def read_spec(self, job_id):
        return self._read(job_id, "spec.json")

    def read_state(self, job_id):
        return self._read(job_id, "state.json")

    def update_state(self, job_id, **fields):
        state = self.read_state(job_id)
        state.update(fields)
        atomic_write_json(self.job_dir(job_id) / "state.json", state)
        return state

    def heartbeat(self, job_id, now=None):
        """Its own file, on purpose.

        A background thread beats while the main loop rewrites progress into
        state.json. Sharing one document between them would mean a read-modify-write
        race and a silently lost update. Two files, two writers, no lock.
        """
        now = time.time() if now is None else now
        atomic_write_bytes(self.job_dir(job_id) / "heartbeat", repr(now).encode("ascii"))
        return now

    def read_heartbeat(self, job_id):
        try:
            return float((self.job_dir(job_id) / "heartbeat").read_bytes())
        except (FileNotFoundError, ValueError):
            return None

    def write_result(self, job_id, result):
        path = self.job_dir(job_id) / "result.json"
        if path.exists():
            raise ResultExists(job_id)
        atomic_write_json(path, result)

    def read_result(self, job_id):
        return self._read(job_id, "result.json")

    def status(self, job_id, now=None):
        now = time.time() if now is None else now
        state = self.read_state(job_id)
        beat = self.read_heartbeat(job_id)
        age = now - (beat if beat is not None else state.get("created_at", now))

        result_path = self.job_dir(job_id) / "result.json"
        if result_path.exists():
            # A job can die between writing its result and rewriting its state.
            # The result is the verdict; the state is only a progress hint.
            status = read_json(result_path)["status"]
        elif state["status"] in ACTIVE_STATES and age > HEARTBEAT_TTL:
            status = "dead"
        else:
            status = state["status"]

        return dict(state, job_id=job_id, status=status, heartbeat_age=age)

    def append_log(self, job_id, text):
        with open(self.job_dir(job_id) / "log.txt", "a", encoding="utf-8") as f:
            f.write(text)

    def write_attempt(self, job_id, n, text):
        """The raw model reply, one file per attempt. log.txt keeps the
        narrative; this keeps the evidence (audit 2026-07-17: no reply
        survived j_d85cd85b, so the anchor drift could only be guessed)."""
        path = self.job_dir(job_id) / "attempt_{}.txt".format(n)
        path.write_text(text, encoding="utf-8")

    def read_log(self, job_id):
        path = self.job_dir(job_id) / "log.txt"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def list_jobs(self):
        return [p.name for p in self.root.iterdir()
                if p.is_dir() and p.name.startswith("j_")]
