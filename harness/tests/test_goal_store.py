"""Goal v1 (spec 2026-07-17): the manager's durable bookkeeping.

The contract's teeth live here: append-only backlog, validated item statuses,
and "done" refusing while work is in flight.
"""
import pytest

from goal_store import (GoalExists, GoalNotFound, GoalStateError, GoalStore,
                        ItemNotFound)

BRIEF = "# Ship X\n\n## Objective\nX works.\n\n## Acceptance criteria\n- [ ] X\n"


@pytest.fixture
def store(tmp_path):
    return GoalStore(tmp_path / "goals")


def test_create_writes_brief_and_open_state(store):
    gid = store.create("Ship X", "demo", BRIEF, now=1.0)
    assert gid.startswith("g_")
    assert store.read_goal_md(gid) == BRIEF
    state = store.read_state(gid)
    assert state == {"title": "Ship X", "project": "demo", "status": "open",
                     "created_at": 1.0, "items": []}


def test_create_refuses_an_existing_goal_id(store):
    store.create("a", "demo", BRIEF, goal_id="g_fixed")
    with pytest.raises(GoalExists):
        store.create("b", "demo", BRIEF, goal_id="g_fixed")


def test_missing_goal_raises_goal_not_found(store):
    with pytest.raises(GoalNotFound):
        store.read_state("g_missing")
    with pytest.raises(GoalNotFound):
        store.read_goal_md("g_missing")


def test_items_are_sequential_and_start_pending(store):
    gid = store.create("g", "demo", BRIEF)
    assert store.add_item(gid, "first", now=2.0) == "it_1"
    assert store.add_item(gid, "second", now=3.0) == "it_2"
    items = store.read_state(gid)["items"]
    assert [i["id"] for i in items] == ["it_1", "it_2"]
    assert items[0] == {"id": "it_1", "title": "first", "status": "pending",
                        "job_ids": [], "note": "", "updated_at": 2.0}


def test_update_item_sets_status_note_and_accumulates_job_ids(store):
    gid = store.create("g", "demo", BRIEF)
    store.add_item(gid, "work")
    store.update_item(gid, "it_1", status="delegated", job_id="j_1", now=4.0)
    item = store.update_item(gid, "it_1", job_id="j_2", note="retry on 14b", now=5.0)
    assert item["status"] == "delegated"      # unchanged by the second call
    assert item["job_ids"] == ["j_1", "j_2"]  # every delegation attempt kept
    assert item["note"] == "retry on 14b"
    assert item["updated_at"] == 5.0


def test_update_item_deduplicates_job_ids(store):
    gid = store.create("g", "demo", BRIEF)
    store.add_item(gid, "work")
    store.update_item(gid, "it_1", job_id="j_1")
    item = store.update_item(gid, "it_1", job_id="j_1")
    assert item["job_ids"] == ["j_1"]


def test_update_item_refuses_unknown_status_and_unknown_item(store):
    gid = store.create("g", "demo", BRIEF)
    store.add_item(gid, "work")
    with pytest.raises(GoalStateError):
        store.update_item(gid, "it_1", status="shipped")
    with pytest.raises(ItemNotFound):
        store.update_item(gid, "it_99", status="merged")


def test_done_refuses_while_work_is_in_flight(store):
    gid = store.create("g", "demo", BRIEF)
    store.add_item(gid, "a")
    store.add_item(gid, "b")
    store.update_item(gid, "it_1", status="merged")
    for in_flight in ("pending", "delegated", "review"):
        store.update_item(gid, "it_2", status=in_flight)
        with pytest.raises(GoalStateError):
            store.finish(gid, "done")
    store.update_item(gid, "it_2", status="dropped")
    state = store.finish(gid, "done", now=9.0)
    assert state["status"] == "done"
    assert state["finished_at"] == 9.0


def test_abandoned_is_always_allowed_and_writes_debrief(store):
    gid = store.create("g", "demo", BRIEF)
    store.add_item(gid, "half-done")  # stays pending
    store.finish(gid, "abandoned", debrief="ran out of road")
    assert store.read_state(gid)["status"] == "abandoned"
    assert (store.goal_dir(gid) / "debrief.md").read_text(encoding="utf-8") == \
        "ran out of road"


def test_finish_takes_only_done_or_abandoned(store):
    gid = store.create("g", "demo", BRIEF)
    with pytest.raises(GoalStateError):
        store.finish(gid, "paused")


def test_add_item_refuses_a_finished_goal(store):
    gid = store.create("g", "demo", BRIEF)
    store.finish(gid, "abandoned")
    with pytest.raises(GoalStateError):
        store.add_item(gid, "too late")


def test_status_counts_items_per_status(store):
    gid = store.create("g", "demo", BRIEF)
    store.add_item(gid, "a")
    store.add_item(gid, "b")
    store.update_item(gid, "it_2", status="merged")
    s = store.status(gid)
    assert s["goal_id"] == gid
    assert s["item_counts"] == {"pending": 1, "delegated": 0, "review": 0,
                                "merged": 1, "dropped": 0}


def test_list_goals_returns_goal_dirs_only(store, tmp_path):
    a = store.create("a", "demo", BRIEF)
    b = store.create("b", "demo", BRIEF)
    (store.root / "not-a-goal").mkdir()
    assert store.list_goals() == sorted([a, b])
