"""The studio's Markdown parser (`harness/web/markdown.js`).

Measured on the 44 archived sessions before writing it: 50 % of non-empty
assistant messages carry Markdown -- inline code 41 %, bold 23 %, ordered
lists 13 %, headings 9 %, bullets 8 %, fences 3 %, tables 2 %, quotes 0 %. The
parser covers that head and nothing else.

The parser is a PURE function (string -> block tree, no DOM) precisely so it
can be tested here: the studio itself stays Node-free -- no build step, no
dependency -- but the test harness may drive `node` when it is installed.
Rendering that tree into elements is separate and never uses innerHTML: the
text comes from a model and from files on disk.
"""
import json
import shutil
import subprocess

import pytest

import factory_web

MD_JS = factory_web.ROOT / "harness" / "web" / "markdown.js"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def parse(text):
    out = subprocess.run(
        [NODE, "-e",
         "const {mdParse} = require(process.argv[1]);"
         "process.stdout.write(JSON.stringify(mdParse(process.argv[2])))",
         str(MD_JS), text],
        capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def text(v):
    return {"t": "text", "v": v}


@needs_node
def test_plain_text_is_one_paragraph():
    assert parse("bonjour") == [{"type": "para", "spans": [text("bonjour")]}]


@needs_node
def test_blank_line_separates_paragraphs():
    assert parse("un\n\ndeux") == [
        {"type": "para", "spans": [text("un")]},
        {"type": "para", "spans": [text("deux")]},
    ]


@needs_node
def test_a_single_newline_stays_inside_the_paragraph():
    # Models wrap prose; a lone newline is not a new idea.
    assert parse("un\ndeux") == [
        {"type": "para", "spans": [text("un\ndeux")]}]


@needs_node
def test_headings_carry_their_level():
    assert parse("## Titre") == [
        {"type": "heading", "level": 2, "spans": [text("Titre")]}]
    assert parse("###### Six") [0]["level"] == 6
    # seven hashes is not a heading, and neither is a bare hash
    assert parse("####### Sept")[0]["type"] == "para"
    assert parse("#pas un titre")[0]["type"] == "para"


@needs_node
def test_inline_code_and_bold_become_spans():
    assert parse("appelle `run()` en **auto**") == [{"type": "para", "spans": [
        text("appelle "), {"t": "code", "v": "run()"},
        text(" en "), {"t": "strong", "v": "auto"}]}]


@needs_node
def test_inline_code_wins_over_bold_inside_it():
    # `**x**` is code showing asterisks, not bold.
    assert parse("`**x**`") == [
        {"type": "para", "spans": [{"t": "code", "v": "**x**"}]}]


@needs_node
def test_an_unclosed_marker_stays_literal():
    assert parse("2 * 3 * 4") == [{"type": "para", "spans": [text("2 * 3 * 4")]}]
    assert parse("un `tick seul") == [
        {"type": "para", "spans": [text("un `tick seul")]}]


@needs_node
def test_fenced_code_keeps_its_text_verbatim():
    assert parse("```py\nx = 1\n\ny = 2\n```") == [
        {"type": "code", "lang": "py", "text": "x = 1\n\ny = 2"}]


@needs_node
def test_markdown_inside_a_fence_is_not_parsed():
    assert parse("```\n# pas un titre\n**pas gras**\n```") == [
        {"type": "code", "lang": "", "text": "# pas un titre\n**pas gras**"}]


@needs_node
def test_an_unclosed_fence_still_yields_its_code():
    # A reply cut by the context window ends mid-block; showing the code beats
    # showing three backticks and calling it a paragraph.
    assert parse("```\nx = 1") == [
        {"type": "code", "lang": "", "text": "x = 1"}]


@needs_node
def test_bullet_and_ordered_lists():
    assert parse("- un\n- deux") == [{"type": "list", "ordered": False, "items": [
        [text("un")], [text("deux")]]}]
    assert parse("1. un\n2. deux") == [{"type": "list", "ordered": True, "items": [
        [text("un")], [text("deux")]]}]


@needs_node
def test_list_items_carry_inline_spans():
    assert parse("- lance `pytest`") == [{"type": "list", "ordered": False,
                                          "items": [[text("lance "),
                                                     {"t": "code", "v": "pytest"}]]}]


@needs_node
def test_a_list_ends_at_a_blank_line():
    assert parse("- un\n\ntexte") == [
        {"type": "list", "ordered": False, "items": [[text("un")]]},
        {"type": "para", "spans": [text("texte")]},
    ]


@needs_node
def test_empty_input_yields_no_blocks():
    assert parse("") == []
    assert parse("   \n\n  ") == []
