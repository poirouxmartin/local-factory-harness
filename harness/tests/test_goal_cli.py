"""The goal verbs of the operator CLI (spec 2026-07-17).

Goals must work without a Factory: no jobs dir, no MCP registry, just the
config (for the project-perimeter check) and the goals root.
"""
import io
import json

import pytest

from conftest import write_config
from factory_cli import main


@pytest.fixture
def config(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / ".git").mkdir()  # resolve_project only checks the marker exists
    cfg = write_config(tmp_path, '[projects.demo]\nrunner = "pytest"\n',
                       paths={"demo": src})
    return cfg


def run_cli(tmp_path, config, *argv):
    out, err = io.StringIO(), io.StringIO()
    code = main(["--goals", str(tmp_path / "goals"), "--config", str(config),
                 "goal", *argv], out=out, err=err)
    text = out.getvalue()
    return code, (json.loads(text) if text.strip() else None), err.getvalue()


def new_goal(tmp_path, config, **kw):
    code, payload, err = run_cli(tmp_path, config, "new",
                                 "--project", kw.get("project", "demo"),
                                 "--title", kw.get("title", "Ship X"))
    assert code == 0, err
    return payload["goal_id"]


def test_new_creates_a_goal_with_a_template_brief(tmp_path, config):
    gid = new_goal(tmp_path, config, title="Ship the thing")
    code, payload, _ = run_cli(tmp_path, config, "show", gid)
    assert code == 0
    assert payload["goal_md"].startswith("# Ship the thing")
    assert "Acceptance criteria" in payload["goal_md"]


def test_new_reads_the_brief_from_a_file(tmp_path, config):
    brief = tmp_path / "brief.md"
    brief.write_text("# custom\n", encoding="utf-8")
    code, payload, err = run_cli(tmp_path, config, "new", "--project", "demo",
                                 "--title", "t", "--file", str(brief))
    assert code == 0, err
    _, shown, _ = run_cli(tmp_path, config, "show", payload["goal_id"])
    assert shown["goal_md"] == "# custom\n"


def test_new_refuses_a_project_outside_the_perimeter(tmp_path, config):
    code, _, err = run_cli(tmp_path, config, "new", "--project", "nope", "--title", "t")
    assert code == 1
    assert "nope" in err


def test_add_item_and_status_counts(tmp_path, config):
    gid = new_goal(tmp_path, config)
    code, payload, _ = run_cli(tmp_path, config, "add", gid, "--title", "first slice")
    assert code == 0
    assert payload["item_id"] == "it_1"
    _, status, _ = run_cli(tmp_path, config, "status", gid)
    assert status["item_counts"]["pending"] == 1


def test_item_update_records_job_and_status(tmp_path, config):
    gid = new_goal(tmp_path, config)
    run_cli(tmp_path, config, "add", gid, "--title", "slice")
    code, item, _ = run_cli(tmp_path, config, "item", gid, "it_1",
                            "--status", "delegated", "--job", "j_abc", "--note", "on 30b")
    assert code == 0
    assert (item["status"], item["job_ids"], item["note"]) == \
        ("delegated", ["j_abc"], "on 30b")


def test_done_refuses_in_flight_then_closes_with_debrief(tmp_path, config):
    gid = new_goal(tmp_path, config)
    run_cli(tmp_path, config, "add", gid, "--title", "slice")
    code, _, err = run_cli(tmp_path, config, "done", gid)
    assert code == 1 and "it_1" in err
    run_cli(tmp_path, config, "item", gid, "it_1", "--status", "merged")
    code, state, _ = run_cli(tmp_path, config, "done", gid, "--debrief", "worked")
    assert code == 0 and state["status"] == "done"
    assert (tmp_path / "goals" / gid / "debrief.md").read_text(encoding="utf-8") == "worked"


def test_list_returns_goal_ids(tmp_path, config):
    a = new_goal(tmp_path, config)
    b = new_goal(tmp_path, config)
    code, payload, _ = run_cli(tmp_path, config, "list")
    assert code == 0
    assert payload["goals"] == sorted([a, b])
