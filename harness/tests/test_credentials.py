"""The credential store. The property under test is negative: a secret must
never come back out of this module in a form anything else could log."""
import os

import pytest

import credentials


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    for name in credentials.KNOWN:
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "factory.toml"
    path.write_text("", encoding="utf-8")
    return str(path)


def test_nothing_stored_means_nothing_set(cfg):
    assert credentials.get("OPENROUTER_API_KEY", cfg) is None
    assert all(not s["set"] for s in credentials.status(cfg))


def test_a_stored_key_round_trips(cfg):
    credentials.put(cfg, "MISTRAL_API_KEY", "sk-mistral-abcdef123")
    assert credentials.get("MISTRAL_API_KEY", cfg) == "sk-mistral-abcdef123"


def test_the_environment_outranks_the_file(cfg, monkeypatch):
    """A key exported before launch must keep working: scripts and CI must not
    change behaviour because the studio wrote a file once."""
    credentials.put(cfg, "OPENAI_API_KEY", "from-file")
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    assert credentials.get("OPENAI_API_KEY", cfg) == "from-env"
    row = _row(credentials.status(cfg), "OPENAI_API_KEY")
    assert row["source"] == "env"


def test_status_never_carries_the_secret(cfg):
    credentials.put(cfg, "ANTHROPIC_API_KEY", "sk-ant-supersecretvalue")
    blob = repr(credentials.status(cfg))
    assert "supersecretvalue" not in blob
    row = _row(credentials.status(cfg), "ANTHROPIC_API_KEY")
    assert row["set"] is True
    assert row["source"] == "file"
    assert row["hint"] == "sk-a...lue"


def test_a_short_key_is_fully_masked(cfg):
    assert credentials.hint("abc") == "***"
    assert credentials.hint("") == ""


def test_clearing_a_key_removes_it(cfg):
    credentials.put(cfg, "DEEPSEEK_API_KEY", "sk-deepseek-1")
    credentials.put(cfg, "DEEPSEEK_API_KEY", "")
    assert credentials.get("DEEPSEEK_API_KEY", cfg) is None
    body = open(credentials.path_for(cfg), encoding="utf-8").read()
    assert "sk-deepseek-1" not in body


def test_an_unknown_credential_is_refused(cfg):
    """A typo must fail at the door, not store a secret under a name nothing
    reads."""
    with pytest.raises(ValueError):
        credentials.put(cfg, "TOTALLY_MADE_UP_KEY", "x")


def test_keys_survive_characters_that_would_break_the_file(cfg):
    nasty = 'sk-with"quote-and\\backslash'
    credentials.put(cfg, "OPENAI_API_KEY", nasty)
    assert credentials.get("OPENAI_API_KEY", cfg) == nasty


def test_several_keys_coexist(cfg):
    credentials.put(cfg, "OPENAI_API_KEY", "sk-openai-value")
    credentials.put(cfg, "MISTRAL_API_KEY", "sk-mistral-value")
    assert credentials.get("OPENAI_API_KEY", cfg) == "sk-openai-value"
    assert credentials.get("MISTRAL_API_KEY", cfg) == "sk-mistral-value"


def test_a_corrupt_file_reads_as_empty_not_as_a_crash(cfg):
    with open(credentials.path_for(cfg), "w", encoding="utf-8") as f:
        f.write("{not toml at all")
    assert credentials.get("OPENAI_API_KEY", cfg) is None


def test_require_names_the_variable_and_the_way_out(cfg):
    with pytest.raises(RuntimeError) as excinfo:
        credentials.require("GEMINI_API_KEY", cfg, purpose="the Gemini lane")
    msg = str(excinfo.value)
    assert "GEMINI_API_KEY" in msg
    assert "Providers" in msg          # tells the operator where to fix it
    assert "Gemini lane" in msg


def test_the_file_is_written_atomically(cfg):
    credentials.put(cfg, "OPENAI_API_KEY", "sk-openai-value")
    leftovers = [p for p in os.listdir(os.path.dirname(cfg))
                 if p.endswith(".tmp")]
    assert leftovers == []


def _row(rows, name):
    return next(r for r in rows if r["name"] == name)
