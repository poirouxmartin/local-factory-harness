"""Turning a model's reply into file writes.

The model names the files it wants to write. That is an instruction from an
untrusted party, so the target list in the spec is an allowlist, not a hint.
"""
import pytest

from patch import (AnchorNotFound, NoCode, UnknownTarget, apply_edits,
                   extract_edits, extract_files)


def test_a_single_target_with_a_bare_code_block_is_mapped_to_it():
    reply = "Here you go:\n```python\ndef add(a, b):\n    return a + b\n```\n"

    assert extract_files(reply, ["calc.py"]) == {"calc.py": "def add(a, b):\n    return a + b\n"}


def test_the_largest_block_wins_when_the_model_also_shows_the_failing_snippet():
    reply = "```python\nassert add(2,2)==4\n```\nFixed:\n```python\ndef add(a, b):\n    return a + b\n```\n"

    assert extract_files(reply, ["calc.py"])["calc.py"].startswith("def add")


def test_file_headers_split_a_multi_file_reply():
    reply = (
        "### FILE: calc.py\n```python\ndef add(a, b):\n    return a + b\n```\n"
        "### FILE: elo.py\n```python\nK = 32\n```\n"
    )

    assert extract_files(reply, ["calc.py", "elo.py"]) == {
        "calc.py": "def add(a, b):\n    return a + b\n",
        "elo.py": "K = 32\n",
    }


def test_a_partial_reply_writes_only_the_files_it_named():
    reply = "### FILE: elo.py\n```python\nK = 32\n```\n"

    assert list(extract_files(reply, ["calc.py", "elo.py"])) == ["elo.py"]


# ---- extract_edits (SEARCH/REPLACE mode) ----


def sr(search, replace):
    return "<<<<<<< SEARCH\n{}=======\n{}>>>>>>> REPLACE".format(search, replace)


def test_extract_edits_single_target_needs_no_header():
    reply = sr("a = 1\n", "a = 2\n")
    assert extract_edits(reply, ["m.py"]) == {"m.py": [("a = 1\n", "a = 2\n")]}


def test_extract_edits_orders_blocks_per_file():
    reply = ("### FILE: m.py\n" + sr("a = 1\n", "a = 2\n") + "\n"
             + sr("b = 1\n", "b = 2\n") + "\n"
             "### FILE: n.py\n" + sr("c = 1\n", "c = 2\n"))
    assert extract_edits(reply, ["m.py", "n.py"]) == {
        "m.py": [("a = 1\n", "a = 2\n"), ("b = 1\n", "b = 2\n")],
        "n.py": [("c = 1\n", "c = 2\n")],
    }


def test_extract_edits_headers_required_for_several_targets():
    with pytest.raises(NoCode):
        extract_edits(sr("a\n", "b\n"), ["m.py", "n.py"])


def test_extract_edits_rejects_a_path_outside_the_targets():
    reply = "### FILE: evil.py\n" + sr("a\n", "b\n")
    with pytest.raises(UnknownTarget):
        extract_edits(reply, ["m.py"])


def test_extract_edits_tolerates_fences_and_crlf():
    reply = ("### FILE: m.py\n```python\n"
             + sr("a = 1\n", "a = 2\n") + "\n```").replace("\n", "\r\n")
    assert extract_edits(reply, ["m.py"]) == {"m.py": [("a = 1\n", "a = 2\n")]}


def test_extract_edits_without_blocks_is_nocode():
    with pytest.raises(NoCode):
        extract_edits("I would change the loop to be faster.", ["m.py"])


def test_extract_edits_empty_search_means_new_file():
    reply = "### FILE: m.py\n" + sr("", "print('new')\n")
    assert extract_edits(reply, ["m.py"]) == {"m.py": [("", "print('new')\n")]}


# ---- apply_edits ----

from patch import AmbiguousAnchor, AnchorNotFound, BrokenResult, apply_edits  # noqa: E402

CALC = "def add(a, b):\n    return a - b\n"


def test_apply_edits_replaces_the_anchored_span():
    out = apply_edits({"m.py": CALC},
                      {"m.py": [("    return a - b\n", "    return a + b\n")]})
    assert out == {"m.py": "def add(a, b):\n    return a + b\n"}


def test_apply_edits_applies_blocks_in_order():
    out = apply_edits({"m.py": "a = 1\nb = 1\n"},
                      {"m.py": [("a = 1\n", "a = 2\n"), ("b = 1\n", "b = a\n")]})
    assert out == {"m.py": "a = 2\nb = a\n"}


def test_apply_edits_missing_anchor_names_the_closest_region():
    with pytest.raises(AnchorNotFound) as e:
        apply_edits({"m.py": CALC}, {"m.py": [("    return a % b\n", "x\n")]})
    assert "return a - b" in str(e.value)  # the closest real line is shown
    assert "EXACTLY" in str(e.value)


def test_apply_edits_ambiguous_anchor_asks_for_more_context():
    text = "x = 1\ny = 2\nx = 1\n"
    with pytest.raises(AmbiguousAnchor) as e:
        apply_edits({"m.py": text}, {"m.py": [("x = 1\n", "x = 3\n")]})
    assert "2 times" in str(e.value)


def test_apply_edits_empty_search_only_creates_new_files():
    assert apply_edits({}, {"m.py": [("", "a = 1\n")]}) == {"m.py": "a = 1\n"}
    with pytest.raises(AnchorNotFound):
        apply_edits({"m.py": CALC}, {"m.py": [("", "a = 1\n")]})


def test_apply_edits_empty_replace_deletes_the_span():
    out = apply_edits({"m.py": "a = 1\nb = 2\n"}, {"m.py": [("a = 1\n", "")]})
    assert out == {"m.py": "b = 2\n"}


def test_apply_edits_tolerates_missing_final_newline_at_eof():
    out = apply_edits({"m.py": "a = 1\nb = 2"},
                      {"m.py": [("b = 2\n", "b = 3\n")]})
    assert out == {"m.py": "a = 1\nb = 3\n"}


# ---- anchor-matching tiers (audit 2026-07-17: exact anchors lost every edit;
# miss #2 landed on accent-stripped French comments, whitespace drift is the
# other invisible killer). Exact stays first; each looser tier only fires when
# every stricter one found nothing, and the tier used is recorded.


def test_anchor_matches_despite_trailing_whitespace_drift():
    text = "a = 1  \nb = 2\n"  # file has trailing spaces the model won't copy
    tiers = []
    out = apply_edits({"m.py": text}, {"m.py": [("a = 1\n", "a = 3\n")]},
                      tier_log=tiers)
    assert out == {"m.py": "a = 3\nb = 2\n"}
    assert tiers == ["whitespace"]


def test_anchor_matches_despite_uniform_indent_drift_and_reindents():
    text = "def f():\n    if x:\n        return 1\n"
    tiers = []
    out = apply_edits(
        {"m.py": text},
        {"m.py": [("if x:\n    return 1\n", "if y:\n    return 2\n")]},
        tier_log=tiers)
    # The replacement inherits the file's real indentation, not the model's.
    assert out == {"m.py": "def f():\n    if y:\n        return 2\n"}
    assert tiers == ["indent"]


def test_anchor_matches_despite_stripped_accents():
    # The observed failure: the model "fixes" the accents of French comments
    # while copying the anchor (j_b0b7fd13, types/scanner.ts).
    text = "// clé différenciateur\nexport const x = 1;\n"
    tiers = []
    out = apply_edits(
        {"m.ts": text},
        {"m.ts": [("// cle differenciateur\nexport const x = 1;\n",
                   "// cle differenciateur\nexport const x = 2;\n")]},
        tier_log=tiers)
    assert out["m.ts"].endswith("export const x = 2;\n")
    assert tiers == ["unicode"]


def test_exact_match_wins_even_when_a_looser_tier_would_be_ambiguous():
    # "a = 1" appears twice modulo whitespace, once exactly: exact must decide.
    text = "a = 1\nz = 0\na = 1  \n"
    tiers = []
    out = apply_edits({"m.py": text}, {"m.py": [("a = 1\n", "a = 2\n")]},
                      tier_log=tiers)
    assert out == {"m.py": "a = 2\nz = 0\na = 1  \n"}
    assert tiers == ["exact"]


def test_ambiguity_at_the_first_matching_tier_is_refused():
    text = "a = 1  \nz = 0\na = 1\t\n"  # no exact match, two whitespace matches
    with pytest.raises(AmbiguousAnchor):
        apply_edits({"m.py": text}, {"m.py": [("a = 1\n", "a = 2\n")]})


def test_no_tier_matching_still_names_the_closest_region():
    with pytest.raises(AnchorNotFound) as e:
        apply_edits({"m.py": CALC}, {"m.py": [("    return a * b\n", "x\n")]})
    assert "return a - b" in str(e.value)


def test_tier_log_records_one_entry_per_applied_edit_in_order():
    text = "a = 1\nb = 2  \n"
    tiers = []
    apply_edits({"m.py": text},
                {"m.py": [("a = 1\n", "a = 3\n"), ("b = 2\n", "b = 4\n")]},
                tier_log=tiers)
    assert tiers == ["exact", "whitespace"]


def test_apply_edits_keeps_the_compile_gate():
    with pytest.raises(BrokenResult):
        apply_edits({"m.py": CALC},
                    {"m.py": [("def add(a, b):\n", "def add(a, b:\n")]})


def test_apply_edits_compile_gate_is_python_only():
    out = apply_edits({"m.js": "let a = (1;\n"}, {"m.js": [("(1;\n", "(1);\n")]})
    assert out == {"m.js": "let a = (1);\n"}


def test_a_reply_with_no_code_block_is_refused():
    with pytest.raises(NoCode):
        extract_files("I would rather not.", ["calc.py"])


def test_a_header_naming_a_file_outside_the_spec_is_refused():
    reply = "### FILE: secrets.py\n```python\nX = 1\n```\n"

    with pytest.raises(UnknownTarget):
        extract_files(reply, ["calc.py"])


@pytest.mark.parametrize("evil", ["../../.bashrc", "/etc/passwd", "C:/Windows/evil.py"])
def test_a_header_escaping_the_worktree_is_refused(evil):
    reply = "### FILE: {}\n```python\nX = 1\n```\n".format(evil)

    with pytest.raises(UnknownTarget):
        extract_files(reply, ["calc.py", evil])


def test_a_bare_block_with_several_targets_is_ambiguous_and_refused():
    reply = "```python\nX = 1\n```"

    with pytest.raises(NoCode):
        extract_files(reply, ["calc.py", "elo.py"])


# A target that itself contains ``` (loop.py's extract_code regex, patch.py's
# _FENCE...) is split to pieces by a non-greedy fence match: the first real
# delegation on harness code truncated loop.py at its regex line, 10 attempts
# running (job j_bc119315). The greedy span — first fence to the LAST closing
# fence — is the only candidate that survives, and compiling tells them apart.

FENCE_BEARING_FILE = (
    'import re\n'
    '\n'
    'def extract_code(text):\n'
    '    blocks = re.findall(r"```(?:python|py)?\\s*\\n(.*?)```", text, re.DOTALL)\n'
    '    return max(blocks, key=len)\n'
)


def test_a_file_that_itself_contains_code_fences_survives_extraction():
    reply = "```python\n" + FENCE_BEARING_FILE + "```\n"

    assert extract_files(reply, ["loop.py"]) == {"loop.py": FENCE_BEARING_FILE}


def test_prose_after_the_closing_fence_is_not_swallowed_by_the_greedy_span():
    reply = ("```python\n" + FENCE_BEARING_FILE + "```\n"
             "That regex is why the file kept breaking.")

    assert extract_files(reply, ["loop.py"]) == {"loop.py": FENCE_BEARING_FILE}


def test_fence_bearing_files_work_under_file_headers_too():
    reply = ("### FILE: loop.py\n```python\n" + FENCE_BEARING_FILE + "```\n"
             "### FILE: elo.py\n```python\nK = 32\n```\n")

    assert extract_files(reply, ["loop.py", "elo.py"]) == {
        "loop.py": FENCE_BEARING_FILE,
        "elo.py": "K = 32\n",
    }


def test_a_python_target_with_no_syntactically_valid_candidate_is_refused():
    # Better an explicit NoCode the model can act on than a SyntaxError from
    # deep inside the spec suite ten attempts running.
    reply = "```python\ndef broken(:\n```\n"

    with pytest.raises(NoCode):
        extract_files(reply, ["calc.py"])


def test_a_typescript_fence_is_extracted_for_a_ts_target():
    from patch import extract_files
    code = "export const add = (a: number, b: number) => a + b;\n"
    reply = "Here is the fix:\n```typescript\n" + code + "```\n"
    assert extract_files(reply, ["src/add.ts"]) == {"src/add.ts": code}


def test_a_ts_target_skips_the_python_compile_gate():
    from patch import extract_files
    # Valid TS, invalid Python: must pass through untouched.
    code = "export function f(): number { return 1; }\n"
    reply = "```ts\n" + code + "```"
    assert extract_files(reply, ["f.ts"])["f.ts"] == code


def test_a_truncated_last_search_line_is_named_as_such():
    # j_bcf15a3c (banc B): the 35b wrote `  }` where the file reads
    # `  } else {`, twice running -- "closest region" never told it what was
    # wrong. Refusing stays right; being vague about why does not.
    text = "a\n  if (x) {\n    go();\n  } else {\n    stop();\n  }\n"
    reply = ("<<<<<<< SEARCH\n    go();\n  }\n=======\n    go2();\n  }\n"
             ">>>>>>> REPLACE\n")
    try:
        apply_edits({"f.ts": text}, extract_edits(reply, ["f.ts"]))
    except AnchorNotFound as e:
        assert "incomplete" in str(e)
        assert "} else {" in str(e)
    else:
        raise AssertionError("a mid-line anchor end must not be accepted")


def test_a_genuinely_absent_anchor_still_reports_the_closest_region():
    text = "a\n  if (x) {\n    go();\n  }\n"
    reply = ("<<<<<<< SEARCH\n    nowhere();\n=======\n    x();\n"
             ">>>>>>> REPLACE\n")
    try:
        apply_edits({"f.ts": text}, extract_edits(reply, ["f.ts"]))
    except AnchorNotFound as e:
        assert "Closest region" in str(e)
    else:
        raise AssertionError("expected AnchorNotFound")
