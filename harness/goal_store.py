"""Goal v1: the manager's bookkeeping (spec 2026-07-17).

A goal is a directory, like a job. goal.md is the brief (written once),
state.json carries the backlog, debrief.md closes the loop. The manager --
a Claude session today, a scheduled process later -- is the single writer;
the same atomic-write idioms as job_store keep readers safe anyway.
"""
import time
import uuid
from pathlib import Path

from job_store import atomic_write_json, read_json

GOAL_STATUSES = ("open", "done", "abandoned")
ITEM_STATUSES = ("pending", "delegated", "review", "merged", "dropped")
# Work in flight: a goal cannot be finished as "done" over these.
OPEN_ITEM_STATUSES = ("pending", "delegated", "review")


class GoalStoreError(Exception):
    pass


class GoalExists(GoalStoreError):
    pass


class GoalNotFound(GoalStoreError):
    pass


class ItemNotFound(GoalStoreError):
    pass


class GoalStateError(GoalStoreError):
    pass


class GoalStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def goal_dir(self, goal_id):
        return self.root / goal_id

    def create(self, title, project, goal_md, goal_id=None, now=None):
        goal_id = goal_id or "g_" + uuid.uuid4().hex[:8]
        now = time.time() if now is None else now
        try:
            # mkdir is the exclusion primitive: goal.md is written exactly once.
            self.goal_dir(goal_id).mkdir(parents=True)
        except FileExistsError:
            raise GoalExists(goal_id)
        (self.goal_dir(goal_id) / "goal.md").write_text(goal_md, encoding="utf-8")
        atomic_write_json(self.goal_dir(goal_id) / "state.json", {
            "title": title, "project": project, "status": "open",
            "created_at": now, "items": [],
        })
        return goal_id

    def read_state(self, goal_id):
        try:
            return read_json(self.goal_dir(goal_id) / "state.json")
        except FileNotFoundError:
            raise GoalNotFound(goal_id)

    def read_goal_md(self, goal_id):
        try:
            return (self.goal_dir(goal_id) / "goal.md").read_text(encoding="utf-8")
        except FileNotFoundError:
            raise GoalNotFound(goal_id)

    def _write_state(self, goal_id, state):
        atomic_write_json(self.goal_dir(goal_id) / "state.json", state)
        return state

    def add_item(self, goal_id, title, now=None):
        now = time.time() if now is None else now
        state = self.read_state(goal_id)
        if state["status"] != "open":
            raise GoalStateError("{} is {}, not open".format(goal_id, state["status"]))
        # Sequential ids: the backlog is ordered and the manager cites them by hand.
        item_id = "it_{}".format(len(state["items"]) + 1)
        state["items"].append({
            "id": item_id, "title": title, "status": "pending",
            "job_ids": [], "note": "", "updated_at": now,
        })
        self._write_state(goal_id, state)
        return item_id

    def update_item(self, goal_id, item_id, status=None, job_id=None, note=None, now=None):
        now = time.time() if now is None else now
        if status is not None and status not in ITEM_STATUSES:
            raise GoalStateError("unknown item status {!r} (have: {})".format(
                status, ", ".join(ITEM_STATUSES)))
        state = self.read_state(goal_id)
        for item in state["items"]:
            if item["id"] == item_id:
                if status is not None:
                    item["status"] = status
                if job_id is not None:
                    # Accumulate every delegation attempt: re-delegations are
                    # the routing data the debrief wants.
                    if job_id not in item["job_ids"]:
                        item["job_ids"].append(job_id)
                if note is not None:
                    item["note"] = note
                item["updated_at"] = now
                self._write_state(goal_id, state)
                return item
        raise ItemNotFound("{} has no item {!r}".format(goal_id, item_id))

    def finish(self, goal_id, status="done", debrief=None, now=None):
        now = time.time() if now is None else now
        if status not in ("done", "abandoned"):
            raise GoalStateError("finish() takes done or abandoned, not {!r}".format(status))
        state = self.read_state(goal_id)
        if status == "done":
            in_flight = [i["id"] for i in state["items"]
                         if i["status"] in OPEN_ITEM_STATUSES]
            if in_flight:
                raise GoalStateError("{} still has work in flight: {}".format(
                    goal_id, ", ".join(in_flight)))
        state["status"] = status
        state["finished_at"] = now
        if debrief:
            (self.goal_dir(goal_id) / "debrief.md").write_text(debrief, encoding="utf-8")
        return self._write_state(goal_id, state)

    def status(self, goal_id):
        state = self.read_state(goal_id)
        counts = {s: 0 for s in ITEM_STATUSES}
        for item in state["items"]:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        return dict(state, goal_id=goal_id, item_counts=counts)

    def list_goals(self):
        return sorted(p.name for p in self.root.iterdir()
                      if p.is_dir() and p.name.startswith("g_"))
