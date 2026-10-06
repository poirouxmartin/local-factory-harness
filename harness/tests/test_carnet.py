import carnet


def _assistant(name, args):
    return {"role": "assistant",
            "tool_calls": [{"function": {"name": name,
                                         "arguments": args}}]}


def _tool(name, content):
    return {"role": "tool", "tool_name": name, "content": content}


def test_target_collapses_command_by_script():
    a = carnet.target_of("run_command", {"command": "python scripts/find_vae.py --x"})
    b = carnet.target_of("run_command", {"command": "python  scripts/find_vae.py"})
    assert a == b == "cmd:python:scripts/find_vae.py"


def test_target_dir_listing_ignores_flags():
    t = carnet.target_of("run_command", {"command": r"dir models\ /s /b"})
    assert t == "cmd:dir:models"


def test_target_file_tools_key_on_path():
    assert carnet.target_of("read_file", {"path": "a/b.txt"}) == "read_file:a/b.txt"
    assert carnet.target_of("edit_file", {"path": r".\a\b.txt"}) == "edit_file:a/b.txt"


def test_target_keeps_parent_and_bare_paths_distinct():
    a = carnet.target_of("read_file", {"path": "../secrets.txt"})
    b = carnet.target_of("read_file", {"path": "secrets.txt"})
    assert a != b
    assert a == "read_file:../secrets.txt"
    assert carnet.target_of("read_file", {"path": "./a/b.txt"}) == "read_file:a/b.txt"


def test_project_collapses_repeated_failing_target():
    msgs = []
    for _ in range(5):
        msgs.append(_assistant("run_command", {"command": "python scripts/find_vae.py"}))
        msgs.append(_tool("run_command", "exit 1\nHTTPError: 403 gated"))
    c = carnet.project(msgs)
    t = c["targets"]["cmd:python:scripts/find_vae.py"]
    assert t["n"] == 5 and t["fail"] == 5
    assert "403" in t["last"]


def test_project_reads_notes_and_walls():
    msgs = [_tool("note", "VAE gated -> need HF token"),
            _tool("wall", '{"wall": "VAE gated", "cause": "auth",'
                          ' "tried": ["official"], "remaining": ["mirror"]}')]
    c = carnet.project(msgs)
    assert c["notes"] == ["VAE gated -> need HF token"]
    assert c["walls"][0]["wall"] == "VAE gated"


def test_render_empty_is_blank():
    assert carnet.render(carnet.project([])) == ""


def test_render_flags_repeated_target():
    msgs = []
    for _ in range(3):
        msgs.append(_assistant("run_command", {"command": "dir models"}))
        msgs.append(_tool("run_command", "exit 1\nnot found"))
    out = carnet.render(carnet.project(msgs))
    assert "cmd:dir:models" in out
    assert "3" in out  # attempt count surfaced


def test_target_distinguishes_pytest_files():
    a = carnet.target_of("run_command", {"command": "python -m pytest tests/test_a.py"})
    b = carnet.target_of("run_command", {"command": "python -m pytest tests/test_b.py"})
    assert a != b
    assert a == "cmd:python:tests/test_a.py"


def test_target_bare_arg_fallback_still_works():
    assert carnet.target_of("run_command", {"command": "dir models"}) == "cmd:dir:models"


def test_carnet_tools_are_read_tools_named():
    names = {t["function"]["name"] for t in carnet.CARNET_TOOLS}
    assert names == {"remember", "recall", "log_wall", "set_plan",
                     "step_done", "ask_operator",
                     "request_resource"} == carnet.CARNET_TOOL_NAMES


def test_resource_justified_needs_a_dead_end_wall():
    assert carnet.resource_justified([]) is False
    assert carnet.resource_justified([{"remaining": ["try X"]}]) is False
    assert carnet.resource_justified([{"remaining": []}]) is True
    # a wall logged without a remaining field is a dead end too
    assert carnet.resource_justified([{"wall": "gated"}]) is True


# ---- what the c_d2a43fd5 replay exposed ------------------------------------

def test_target_looks_past_a_leading_cd():
    """cmd.exe has no persistent cwd, so the agent prefixes almost every call
    with `cd <workspace> &&`. Keying on the head collapsed 12 unrelated
    commands onto one target -- the carnet then reported a loop that was not
    one, and hid the real repeats."""
    a = carnet.target_of("run_command", {
        "command": r"cd C:\Users\me\lf && python scripts/find_vae.py"})
    b = carnet.target_of("run_command", {
        "command": r"cd C:\Users\me\lf\other && python scripts/find_vae.py"})
    assert a == b == "cmd:python:scripts/find_vae.py"


def test_target_ignores_a_trailing_fallback_branch():
    """`dir X 2>nul || echo missing` is one action with a fallback, not two."""
    t = carnet.target_of("run_command", {
        "command": r'dir C:\lf\models 2>nul || echo "models missing"'})
    assert t == "cmd:dir:C:/lf/models"


def test_target_of_a_bare_cd_still_keys_on_the_directory():
    assert carnet.target_of("run_command", {"command": r"cd C:\lf"}) == \
        "cmd:cd:C:/lf"


def test_render_shows_failures_before_mere_repeats():
    msgs = []
    for _ in range(9):
        msgs.append(_assistant("list_dir", {"path": "a"}))
        msgs.append(_tool("list_dir", "ok"))
    msgs.append(_assistant("run_command", {"command": "python boom.py"}))
    msgs.append(_tool("run_command", "exit 1\nImportError: torch"))
    out = carnet.render(carnet.project(msgs))
    lines = out.splitlines()
    assert lines[1].startswith("- cmd:python:boom.py")   # failure first
    assert any("list_dir:a" in l for l in lines)


def test_render_drops_whole_rows_never_cuts_a_line():
    msgs = []
    for i in range(60):
        msgs.append(_assistant("read_file", {"path": "dir/file_%d.py" % i}))
        msgs.append(_tool("read_file", "exit 1\nerror: no such file"))
    out = carnet.render(carnet.project(msgs))
    assert len(out) <= carnet.HOT_BUDGET
    assert out.endswith(carnet.FOOTER)
    assert all(l.startswith(("- ", "---")) for l in out.splitlines())


def test_render_keeps_walls_and_notes_when_targets_are_many():
    """Rows are the cheap signal; the agent's own words are not squeezed out."""
    msgs = []
    for i in range(60):
        msgs.append(_assistant("read_file", {"path": "dir/file_%d.py" % i}))
        msgs.append(_tool("read_file", "exit 1\nerror: no such file"))
    msgs.append(_tool("wall", '{"wall": "VAE gated", "cause": "auth",'
                              ' "tried": ["officiel"], "remaining": ["mirror"]}'))
    msgs.append(_tool("note", "le token HF est requis"))
    out = carnet.render(carnet.project(msgs))
    assert "MUR VAE gated" in out
    assert "le token HF est requis" in out
    assert len(out) <= carnet.HOT_BUDGET


# ---- plan (phase 2) --------------------------------------------------------

import json


def _plan(goal, steps):
    return _tool("plan", json.dumps({"goal": goal, "steps": steps}))


TWO_STEPS = [{"step": "install ComfyUI", "done_when": "file:ComfyUI/main.py"},
             {"step": "fetch the VAE",
              "done_when": "file:models/vae/ae.safetensors",
              "walls": ["may be gated"]}]


def test_plan_projected_with_ticks():
    msgs = [_plan("generate an image", TWO_STEPS),
            _tool("progress", json.dumps({"i": 1,
                                          "evidence": "file:ComfyUI/main.py"}))]
    plan = carnet.project(msgs)["plan"]
    assert plan["goal"] == "generate an image"
    assert plan["steps"][0]["done"] is True
    assert plan["steps"][0]["evidence"] == "file:ComfyUI/main.py"
    assert plan["steps"][1]["done"] is False
    assert plan["steps"][1]["walls"] == ["may be gated"]


def test_plan_revision_replaces_and_keeps_ticks():
    msgs = [_plan("old", [{"step": "a", "done_when": "a done"}]),
            _tool("step", json.dumps({"i": 1, "evidence": "did a"})),
            _plan("new", [{"step": "a", "done_when": "a done"},
                          {"step": "b", "done_when": "b done"}])]
    plan = carnet.project(msgs)["plan"]
    assert plan["goal"] == "new" and plan["revisions"] == 2
    assert len(plan["steps"]) == 2
    assert plan["steps"][0]["done"] is True   # the tick survives the revision
    assert plan["steps"][1]["done"] is False


def test_plan_steps_written_as_a_string_are_not_read_letter_by_letter():
    # The ChatGPT lane speaks a text protocol: every parameter arrives as a
    # string, `steps` included. Iterating it gave one step per character --
    # live c_14f20c89 (2026-08-06) showed a 4-step plan as "0/36". The parser
    # at the boundary types it now; this is the second layer, and the only one
    # that helps a session already on disk.
    steps = '[{"step": "read a.py", "done_when": "file:a.py"}, {"step": "build"}]'
    plan = carnet.project([_plan("g", steps)])
    assert [s["step"] for s in plan["plan"]["steps"]] == ["read a.py", "build"]
    assert plan["plan"]["steps"][0]["done_when"] == "file:a.py"


def test_plan_steps_written_as_prose_split_on_their_separators():
    steps = "1. Inspecter Application.h;2. Compiler et executer un test"
    plan = carnet.project([_plan("g", steps)])
    # numbered by the model, and the row numbers itself: "1. 1. Inspecter"
    assert [s["step"] for s in plan["plan"]["steps"]] == [
        "Inspecter Application.h", "Compiler et executer un test"]


def test_no_plan_is_none():
    assert carnet.project([])["plan"] is None


def test_plan_messages_do_not_eat_the_target_pairing():
    # a plan/step/progress message must not drain the pending call queue: its
    # stored tool_name is not the call name (same trap as note/wall).
    msgs = [_assistant("run_command", {"command": "python go.py"}),
            _plan("g", [{"step": "a", "done_when": "x"}]),
            _tool("run_command", "exit 1\nboom")]
    c = carnet.project(msgs)
    assert c["targets"]["cmd:python:go.py"]["fail"] == 1


def test_file_condition_detects_explicit_and_bare_paths():
    assert carnet.file_condition("file:models/vae/ae.safetensors") == \
        "models/vae/ae.safetensors"
    assert carnet.file_condition("FILE: out/*.png") == "out/*.png"
    assert carnet.file_condition("models/vae/ae.safetensors") == \
        "models/vae/ae.safetensors"
    assert carnet.file_condition("generate.py") == "generate.py"


def test_file_condition_survives_prose_glued_after_the_path():
    # Observed live 2026-07-25, on the first GPU run of the loop: the 30b wrote
    # `file:backend/matchHistory.js has been updated with new delta fields`.
    # Anchored at the end, the regex called that prose -- so `newly_satisfied`
    # never ticked the milestone and `verify_claim` waved every claim through,
    # turning A.2 off in silence on the very first live plan.
    assert carnet.file_condition(
        "file:backend/matchHistory.js has been updated with new delta fields") \
        == "backend/matchHistory.js"
    assert carnet.file_condition("file: out/*.png once the render lands") == \
        "out/*.png"


def test_file_condition_refuses_a_prefix_with_no_path_behind_it():
    # The token after `file:` still has to LOOK like a path. Promoting prose to
    # a pattern is worse than ignoring it: verify_claim refuses a step whose
    # pattern is absent, so `file: it compiles` would lock that step shut
    # forever -- and A.2 is a signal, never a block.
    assert carnet.file_condition("file: it compiles") is None
    assert carnet.file_condition("file: the suite is green") is None


def test_file_condition_ignores_prose():
    assert carnet.file_condition("the tests pass") is None
    assert carnet.file_condition("ComfyUI answers on port 8188") is None
    assert carnet.file_condition("") is None


def _projected(msgs):
    return carnet.project(msgs)["plan"]


def test_plan_carries_the_timestamp_it_was_set_at():
    # The harness needs it to tell a file the plan produced from one that was
    # already on disk when the plan was written.
    msgs = [_plan("g", TWO_STEPS)]
    msgs[0]["ts"] = 1234.5
    assert carnet.project(msgs)["plan"]["ts"] == 1234.5


def test_plan_timestamp_follows_the_revision_that_wins():
    old, new = _plan("old", TWO_STEPS), _plan("new", TWO_STEPS)
    old["ts"], new["ts"] = 100.0, 200.0
    assert carnet.project([old, new])["plan"]["ts"] == 200.0


def test_newly_satisfied_skips_ticked_steps():
    plan = _projected([_plan("g", TWO_STEPS),
                       _tool("progress", json.dumps({"i": 1}))])
    got = carnet.newly_satisfied(plan, lambda p: True)
    assert got == [(2, "file:models/vae/ae.safetensors")]


def test_newly_satisfied_ignores_prose_conditions():
    plan = _projected([_plan("g", [{"step": "a", "done_when": "it works"}])])
    assert carnet.newly_satisfied(plan, lambda p: True) == []


def test_verify_claim_refuses_a_missing_file():
    plan = _projected([_plan("g", TWO_STEPS)])
    ok, reason = carnet.verify_claim(plan, 2, lambda p: False)
    assert ok is False and "models/vae/ae.safetensors" in reason


def test_verify_claim_accepts_prose_and_present_files():
    plan = _projected([_plan("g", [{"step": "a", "done_when": "it works"}])])
    assert carnet.verify_claim(plan, 1, lambda p: False)[0] is True
    plan = _projected([_plan("g", TWO_STEPS)])
    assert carnet.verify_claim(plan, 1, lambda p: True)[0] is True


def test_verify_claim_refuses_an_unknown_step():
    plan = _projected([_plan("g", TWO_STEPS)])
    assert carnet.verify_claim(plan, 9, lambda p: True)[0] is False
    assert carnet.verify_claim(None, 1, lambda p: True)[0] is False


def _churn(n):
    out = []
    for i in range(n):
        out.append(_assistant("run_command", {"command": "python try%d.py" % i}))
        out.append(_tool("run_command", "exit 1\nboom"))
    return out


def test_stagnation_counts_actions_since_the_plan():
    msgs = [_plan("g", TWO_STEPS)] + _churn(carnet.STAGNATION_AT + 1)
    assert carnet.stagnation(msgs) == carnet.STAGNATION_AT + 1


def test_stagnation_resets_on_a_checkpoint():
    msgs = ([_plan("g", TWO_STEPS)] + _churn(9) +
            [_tool("progress", json.dumps({"i": 1}))] + _churn(2))
    assert carnet.stagnation(msgs) == 2


def test_stagnation_without_a_plan_is_zero():
    assert carnet.stagnation(_churn(9)) == 0


def test_actions_ignores_bookkeeping_messages():
    msgs = _churn(3) + [_tool("system", "nudge"), _tool("note", "x"),
                        _plan("g", TWO_STEPS)]
    assert carnet.actions(msgs) == 3


def test_render_shows_the_plan_with_checkboxes():
    out = carnet.render(carnet.project(
        [_plan("generate an image", TWO_STEPS),
         _tool("progress", json.dumps({"i": 1}))]))
    assert "PLAN: generate an image" in out
    assert "[x] 1. install ComfyUI" in out
    assert "[ ] 2. fetch the VAE" in out
    assert "may be gated" in out


def test_render_warns_on_stagnation():
    msgs = [_plan("g", TWO_STEPS)] + _churn(carnet.STAGNATION_AT)
    out = carnet.render(carnet.project(msgs), stalled=carnet.stagnation(msgs))
    assert "STAGNATION" in out
    assert str(carnet.STAGNATION_AT) in out


def test_render_does_not_warn_below_the_threshold():
    msgs = [_plan("g", TWO_STEPS)] + _churn(2)
    out = carnet.render(carnet.project(msgs), stalled=carnet.stagnation(msgs))
    assert "STAGNATION" not in out


def test_render_nudges_for_a_plan_only_when_expected():
    msgs = _churn(carnet.PLAN_NUDGE_AT)
    c = carnet.project(msgs)
    assert "PAS DE PLAN" in carnet.render(c, plan_expected=True)
    assert "PAS DE PLAN" not in carnet.render(c)


def test_render_keeps_the_plan_when_the_carnet_is_crowded():
    steps = [{"step": "etape %d" % i, "done_when": "file:out/%d.txt" % i}
             for i in range(40)]
    msgs = [_plan("un but tres long " * 10, steps)] + _churn(30)
    out = carnet.render(carnet.project(msgs))
    assert "PLAN:" in out
    assert len(out) <= carnet.HOT_BUDGET


def test_revision_drops_a_tick_whose_step_changed():
    # A tick names the step it was granted for: a revision that replaces step 1
    # must not inherit its checkmark.
    msgs = [_plan("g", [{"step": "download the VAE", "done_when": "x"}]),
            _tool("step", json.dumps({"i": 1, "step": "download the VAE"})),
            _plan("g", [{"step": "build from source", "done_when": "x"}])]
    assert carnet.project(msgs)["plan"]["steps"][0]["done"] is False


def test_unfinished_counts_the_steps_the_plan_still_owes():
    plan = carnet.parse_plan(json.dumps({
        "goal": "g", "steps": [{"step": "un"}, {"step": "deux"}]}))

    assert carnet.unfinished(plan) == 2
    plan["steps"][0]["done"] = True
    assert carnet.unfinished(plan) == 1
    plan["steps"][1]["done"] = True
    assert carnet.unfinished(plan) == 0


def test_unfinished_is_zero_without_a_plan():
    # Progress is defined against the plan (spec C.2), like carnet.stagnation.
    # So is continuation: a planless session is not owed a relance.
    assert carnet.unfinished(None) == 0
