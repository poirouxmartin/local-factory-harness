"""Adding a project = appending to factory.local.toml, never rewriting it.

The tracked factory.toml is never touched by this path -- only a human
promotes a project into it. The local file may carry its own doctrinal
comments; a round-trip through a TOML writer would erase them. So the block
is appended textually, and the candidate text is re-parsed before it
atomically replaces the original: an invalid TOML can never land on disk.
"""
import subprocess

import pytest

from factory_config import ConfigError, load_projects, local_config_path, resolve_project
from factory_mcp import Factory

HEADER = "# The delegation perimeter. Do not lose this comment.\n"
LOCAL_HEADER = "# This machine's coordinates. Do not lose this comment.\n"


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "newproj"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    return root


@pytest.fixture
def factory(tmp_path):
    cfg = tmp_path / "factory.toml"
    cfg.write_text(HEADER, encoding="utf-8")
    f = Factory(tmp_path / "jobs", cfg, spawn_fn=lambda job_id: 4242)
    f.cfg = cfg
    return f


def test_add_project_appends_a_resolvable_entry(factory, repo):
    out = factory.add_project("newproj", str(repo),
                              regression_cmd=["-m", "pytest", "-q"])

    assert out == {"project": "newproj", "status": "added"}
    projects = load_projects(factory.cfg)
    resolved = resolve_project(projects, "newproj")
    assert resolved.path == repo.resolve()
    assert resolved.runner == "pytest"
    assert resolved.regression_cmd == ["-m", "pytest", "-q"]


def test_add_project_keeps_the_existing_text_and_comments(factory, repo):
    local = local_config_path(factory.cfg)
    local.write_text(LOCAL_HEADER, encoding="utf-8")

    factory.add_project("newproj", str(repo))

    text = local.read_text(encoding="utf-8")
    assert text.startswith(LOCAL_HEADER)
    assert "[projects.newproj]" in text


def test_a_second_project_lands_after_the_first(factory, repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")

    factory.add_project("newproj", str(repo))
    factory.add_project("other", str(other))

    assert sorted(load_projects(factory.cfg)) == ["newproj", "other"]


def test_an_existing_key_is_refused_and_the_file_untouched(factory, repo):
    factory.add_project("newproj", str(repo))
    local = local_config_path(factory.cfg)
    before = local.read_text(encoding="utf-8")

    with pytest.raises(ConfigError, match="already"):
        factory.add_project("newproj", str(repo))

    assert local.read_text(encoding="utf-8") == before


def test_a_name_that_is_not_a_key_is_refused(factory, repo):
    for bad in ("has space", "a/b", "", "é", "[projects.evil]"):
        with pytest.raises(ConfigError):
            factory.add_project(bad, str(repo))


def test_a_path_that_is_not_a_git_repo_is_refused(factory, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(ConfigError, match="git"):
        factory.add_project("plain", str(plain))
    assert "plain" not in load_projects(factory.cfg)


def test_regression_cmd_must_be_a_list_of_strings(factory, repo):
    with pytest.raises(ConfigError, match="regression_cmd"):
        factory.add_project("newproj", str(repo), regression_cmd="pytest -q")


def test_the_new_project_lands_in_the_local_file(factory, repo):
    factory.add_project("newproj", str(repo))

    tracked = factory.cfg.read_text(encoding="utf-8")
    local = local_config_path(factory.cfg).read_text(encoding="utf-8")

    assert "newproj" not in tracked      # nothing the agent writes is committed
    assert "[projects.newproj]" in local
    assert repo.resolve().as_posix() in local


def test_a_name_already_in_the_tracked_file_is_still_refused(factory, repo):
    # The duplicate check reads the MERGED view: otherwise the agent shadows a
    # versioned project by reusing its key, and the tracked runner silently
    # loses to a generated one.
    factory.cfg.write_text('[projects.taken]\nrunner = "vitest"\n',
                           encoding="utf-8")

    with pytest.raises(ConfigError):
        factory.add_project("taken", str(repo))
