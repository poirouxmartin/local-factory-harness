"""The studio's activity line (`harness/web/activity.js`).

Pure functions, no DOM, driven through `node` -- the studio itself stays
Node-free (no build step, no dependency), exactly like markdown.js.
"""
import json
import shutil
import subprocess

import pytest

import factory_web

ACT_JS = factory_web.ROOT / "harness" / "web" / "activity.js"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def call(fn, *args):
    out = subprocess.run(
        [NODE, "-e",
         "const m = require(process.argv[1]);"
         "const a = JSON.parse(process.argv[3]);"
         "process.stdout.write(JSON.stringify(m[process.argv[2]](...a)))",
         str(ACT_JS), fn, json.dumps(args)],
        capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_read_file_reads_its_path():
    assert call("describeCall", "read_file", {"path": "harness/regress.py"}) == {
        "icon": "file", "verb": "lit", "target": "harness/regress.py"}


@needs_node
def test_run_command_shows_the_command_itself():
    d = call("describeCall", "run_command", {"command": "py -3 -m pytest -q"})
    assert d == {"icon": "terminal", "verb": "", "target": "py -3 -m pytest -q"}


@needs_node
def test_set_plan_counts_its_steps():
    d = call("describeCall", "set_plan",
             {"goal": "g", "steps": [{"step": "a"}, {"step": "b"}]})
    assert d == {"icon": "plan", "verb": "pose le plan", "target": "2 etapes"}


@needs_node
def test_an_unknown_tool_is_not_an_error():
    """Toolsets move. The view must not break when they move."""
    d = call("describeCall", "brand_new_tool", {"whatever": "x"})
    assert d == {"icon": "tool", "verb": "brand_new_tool", "target": "x"}


@needs_node
def test_a_long_target_is_clipped():
    d = call("describeCall", "run_command", {"command": "x" * 200})
    assert len(d["target"]) == 80
    assert d["target"].endswith("…")


@needs_node
def test_missing_arguments_never_throw():
    assert call("describeCall", "read_file", {}) == {
        "icon": "file", "verb": "lit", "target": ""}
    assert call("describeCall", "read_file", None) == {
        "icon": "file", "verb": "lit", "target": ""}


def call_raw_tools():
    out = subprocess.run(
        [NODE, "-e",
         "const {TOOLS} = require(process.argv[1]);"
         "const o = {};"
         "for (const [k, v] of Object.entries(TOOLS)) o[k] = {icon: v.icon};"
         "process.stdout.write(JSON.stringify(o))",
         str(ACT_JS)],
        capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return out.stdout


@needs_node
def test_every_icon_named_by_the_table_exists_in_the_sprite():
    """One sprite, one style. A table entry pointing at a missing symbol
    renders an empty box, and nothing says why."""
    sprite = (factory_web.WEB_DIR / "icons.svg").read_text(encoding="utf-8")
    table = json.loads(call_raw_tools())
    wanted = {e["icon"] for e in table.values()} | {"tool"}
    missing = [n for n in sorted(wanted) if 'id="ico-%s"' % n not in sprite]
    assert missing == []


@needs_node
def test_a_turn_runs_from_one_user_message_to_the_next():
    msgs = [{"role": "user", "content": "a", "ts": 1},
            {"role": "tool", "tool_name": "read_file", "ts": 2},
            {"role": "assistant", "content": "ok", "ts": 3},
            {"role": "user", "content": "b", "ts": 4},
            {"role": "assistant", "content": "ok2", "ts": 5}]
    turns = call("groupTurns", msgs)
    assert len(turns) == 2
    assert turns[0]["lead"]["content"] == "a"
    assert len(turns[0]["items"]) == 2
    assert turns[1]["lead"]["content"] == "b"


@needs_node
def test_messages_before_the_first_user_message_are_kept():
    """A resumed session, or a system message in front. Losing them would be
    silent, which is the one thing we do not accept."""
    msgs = [{"role": "system", "content": "s", "ts": 1},
            {"role": "user", "content": "a", "ts": 2}]
    turns = call("groupTurns", msgs)
    assert turns[0]["lead"] is None
    assert len(turns[0]["items"]) == 1
    assert turns[1]["lead"]["content"] == "a"


@needs_node
def test_an_empty_transcript_has_no_turn():
    assert call("groupTurns", []) == []


@needs_node
def test_recap_counts_tools_and_duration():
    turn = {"lead": {"role": "user", "ts": 100},
            "items": [{"role": "tool", "tool_name": "read_file", "ts": 101},
                      {"role": "tool", "tool_name": "read_file", "ts": 102},
                      {"role": "tool", "tool_name": "run_command", "ts": 105},
                      {"role": "assistant", "content": "ok", "ts": 118}]}
    assert call("formatRecap", turn) == "3 outils · 18 s · 2 lus, 1 commande"


@needs_node
def test_recap_singular_when_there_is_one_tool():
    turn = {"lead": {"role": "user", "ts": 10},
            "items": [{"role": "tool", "tool_name": "read_file", "ts": 11},
                      {"role": "assistant", "content": "ok", "ts": 13}]}
    assert call("formatRecap", turn) == "1 outil · 3 s · 1 lu"


@needs_node
def test_a_turn_with_no_tool_has_no_recap():
    turn = {"lead": {"role": "user", "ts": 10},
            "items": [{"role": "assistant", "content": "ok", "ts": 12}]}
    assert call("formatRecap", turn) == ""


@needs_node
def test_recap_clamps_negative_duration_to_zero():
    """Out-of-order timestamps (clock skew, resumed session) must not produce
    negative durations. A missing timestamp still omits the segment entirely."""
    turn = {"lead": {"ts": 100},
            "items": [{"role": "tool", "tool_name": "read_file", "ts": 50}]}
    assert call("formatRecap", turn) == "1 outil · 0 s · 1 lu"


@needs_node
def test_recap_breaks_down_delegation_and_job_tools():
    turn = {"lead": {"ts": 10},
            "items": [{"role": "tool", "tool_name": "delegate", "ts": 11},
                      {"role": "tool", "tool_name": "job_status", "ts": 12},
                      {"role": "tool", "tool_name": "job_result", "ts": 13},
                      {"role": "assistant", "content": "ok", "ts": 20}]}
    assert call("formatRecap", turn) == "3 outils · 10 s · 3 jobs"


@needs_node
def test_recap_breaks_down_plan_tools():
    turn = {"lead": {"ts": 10},
            "items": [{"role": "tool", "tool_name": "set_plan", "ts": 11},
                      {"role": "tool", "tool_name": "step_done", "ts": 12},
                      {"role": "assistant", "content": "ok", "ts": 13}]}
    assert call("formatRecap", turn) == "2 outils · 3 s · 2 etapes de plan"


@needs_node
def test_recap_breaks_down_memory_and_misc_tools():
    turn = {"lead": {"ts": 10},
            "items": [{"role": "tool", "tool_name": "remember", "ts": 11},
                      {"role": "assistant", "content": "ok", "ts": 12}]}
    assert call("formatRecap", turn) == "1 outil · 2 s · 1 note"


@needs_node
def test_recap_counts_unknown_tools_in_autres_bucket():
    turn = {"lead": {"ts": 10},
            "items": [{"role": "tool", "tool_name": "brand_new_tool", "ts": 11},
                      {"role": "tool", "tool_name": "another_new_one", "ts": 12},
                      {"role": "assistant", "content": "ok", "ts": 13}]}
    assert call("formatRecap", turn) == "2 outils · 3 s · 2 autres"
