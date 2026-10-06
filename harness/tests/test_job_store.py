"""JobStore contract.

Three invariants carry the whole design:

  spec.json    is immutable  -- the job's input can never be rewritten
  result.json  is write-once -- a job cannot revise its own verdict
  status()     is derived    -- "running" is a claim about a heartbeat, not a
                               field somebody remembered to update. There is no
                               daemon, so nothing marks a killed job as dead.
"""
import re

import pytest

from job_store import HEARTBEAT_TTL, JobExists, JobNotFound, JobStore, ResultExists

SPEC = {"project": "lucena", "goal": "fix ELO", "tests": {"test_elo.py": "def test_x(): pass"}}


@pytest.fixture
def store(tmp_path):
    return JobStore(tmp_path / "jobs")


def test_create_returns_a_job_id(store):
    job_id = store.create(SPEC)

    assert re.fullmatch(r"j_[0-9a-f]{8}", job_id)


def test_create_writes_the_spec_verbatim(store):
    job_id = store.create(SPEC)

    assert store.read_spec(job_id) == SPEC


def test_create_starts_the_job_queued(store):
    job_id = store.create(SPEC)

    assert store.read_state(job_id)["status"] == "queued"


def test_create_seeds_a_heartbeat_so_a_slow_spawn_is_not_read_as_dead(store):
    job_id = store.create(SPEC, now=1000.0)

    assert store.status(job_id, now=1000.0 + HEARTBEAT_TTL - 1)["status"] == "queued"


def test_spec_is_immutable_a_second_create_on_the_same_id_is_refused(store):
    store.create(SPEC, job_id="j_00000001")

    with pytest.raises(JobExists):
        store.create({"project": "evil"}, job_id="j_00000001")

    assert store.read_spec("j_00000001") == SPEC


def test_reading_an_unknown_job_raises(store):
    with pytest.raises(JobNotFound):
        store.read_state("j_deadbeef")


def test_update_state_merges_rather_than_replaces(store):
    job_id = store.create(SPEC)
    store.update_state(job_id, status="running", stage=1)

    store.update_state(job_id, attempt=3)

    state = store.read_state(job_id)
    assert state["status"] == "running"
    assert state["stage"] == 1
    assert state["attempt"] == 3


def test_heartbeat_advances_the_timestamp(store):
    job_id = store.create(SPEC, now=1000.0)

    store.heartbeat(job_id, now=1234.0)

    assert store.read_heartbeat(job_id) == 1234.0


def test_the_heartbeat_lives_outside_state_json(store):
    """The beat comes from a thread while the main loop rewrites progress. If both
    read-modify-wrote state.json, one of them would lose."""
    job_id = store.create(SPEC, now=1000.0)
    store.update_state(job_id, status="running", stage=2)

    store.heartbeat(job_id, now=1234.0)

    state = store.read_state(job_id)
    assert state["stage"] == 2
    assert "heartbeat" not in state


def test_result_is_write_once(store):
    job_id = store.create(SPEC)
    store.write_result(job_id, {"status": "succeeded", "diff": "d1"})

    with pytest.raises(ResultExists):
        store.write_result(job_id, {"status": "failed", "diff": "d2"})

    assert store.read_result(job_id)["diff"] == "d1"


def test_a_running_job_with_a_fresh_heartbeat_is_running(store):
    job_id = store.create(SPEC, now=1000.0)
    store.update_state(job_id, status="running")
    store.heartbeat(job_id, now=1000.0)

    assert store.status(job_id, now=1000.0 + HEARTBEAT_TTL - 1)["status"] == "running"


def test_a_running_job_with_a_stale_heartbeat_is_dead(store):
    job_id = store.create(SPEC, now=1000.0)
    store.update_state(job_id, status="running")
    store.heartbeat(job_id, now=1000.0)

    assert store.status(job_id, now=1000.0 + HEARTBEAT_TTL + 1)["status"] == "dead"


def test_a_job_queued_for_the_gpu_still_heartbeats_and_is_not_dead(store):
    """flock() blocks; the runner must beat before it waits, or the queue looks dead."""
    job_id = store.create(SPEC, now=1000.0)
    store.update_state(job_id, status="waiting_gpu")
    store.heartbeat(job_id, now=2000.0)

    status = store.status(job_id, now=2000.0 + 10)
    assert status["status"] == "waiting_gpu"


def test_a_finished_result_beats_a_stale_running_state(store):
    """The job wrote result.json then died before rewriting state.json. Not dead: done."""
    job_id = store.create(SPEC, now=1000.0)
    store.update_state(job_id, status="running")
    store.heartbeat(job_id, now=1000.0)
    store.write_result(job_id, {"status": "succeeded", "diff": "d"})

    assert store.status(job_id, now=9999.0)["status"] == "succeeded"


def test_status_exposes_progress_from_the_state(store):
    job_id = store.create(SPEC, now=1000.0)
    store.update_state(job_id, status="running", stage=2, tests_passed=11, tests_failed=3)
    store.heartbeat(job_id, now=1000.0)

    status = store.status(job_id, now=1001.0)
    assert (status["stage"], status["tests_passed"], status["tests_failed"]) == (2, 11, 3)


def test_status_reports_heartbeat_age(store):
    job_id = store.create(SPEC, now=1000.0)
    store.heartbeat(job_id, now=1000.0)

    assert store.status(job_id, now=1042.0)["heartbeat_age"] == 42.0


def test_append_log_accumulates(store):
    job_id = store.create(SPEC)

    store.append_log(job_id, "stage 1\n")
    store.append_log(job_id, "stage 2\n")

    assert store.read_log(job_id) == "stage 1\nstage 2\n"


def test_list_jobs_returns_every_created_job(store):
    a = store.create(SPEC, job_id="j_00000001")
    b = store.create(SPEC, job_id="j_00000002")

    assert sorted(store.list_jobs()) == sorted([a, b])
