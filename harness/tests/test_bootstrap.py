"""bootstrap: what a fresh clone needs before it can do anything.

Spec: docs/design/specs/2026-08-04-machine-local-config-design.md

The spec mandates five warning kinds. Three of them shipped in 07d6dcb with no
test at all, and two of those three were dead code -- unreachable on exactly
the fresh clone they exist for. Every kind now has a test that builds its
condition and asserts its text.
"""
import subprocess

import pytest

import bootstrap
from conftest import git

TRACKED_LLAMA = """[llama_server]
port = 8080
ctx = 32768

[llama_server.models."qwen3-coder:30b"]
args = []
"""

LOCAL_LLAMA = """[llama_server]
exe = "C:/nope/llama-server.exe"

[llama_server.models."qwen3-coder:30b"]
path = "C:/nope/w.gguf"
"""


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "clone"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / ".githooks").mkdir()
    return root


@pytest.fixture
def no_identity(tmp_path, monkeypatch):
    """A git that resolves no user.name/user.email at any level.

    Built here rather than borrowed from the machine: the first version of this
    condition was a `--local` read inside bootstrap, which passed on every host
    precisely because it ignored the global identity the host commits with.
    """
    nowhere = tmp_path / "no-such-gitconfig"
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(nowhere))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(nowhere))


def test_it_arms_the_hooks_path(repo):
    (repo / "factory.toml").write_text('[projects.demo]\nrunner = "pytest"\n',
                                       encoding="utf-8")

    bootstrap.run(repo / "factory.toml", repo)

    armed = subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath"],
                           capture_output=True, text=True)
    assert armed.stdout.strip() == ".githooks"


def test_it_writes_a_stub_per_declared_project(repo):
    (repo / "factory.toml").write_text(
        '[projects.demo]\nrunner = "pytest"\n[projects.other]\nrunner = "vitest"\n',
        encoding="utf-8")

    result = bootstrap.run(repo / "factory.toml", repo)

    local = (repo / "factory.local.toml").read_text(encoding="utf-8")
    assert result["local_created"] is True
    assert "[projects.demo]" in local
    assert "[projects.other]" in local
    # Stubs are commented: an empty path is a path, and it would resolve to the
    # current directory rather than announcing itself as unset.
    assert "# path =" in local


def test_it_never_overwrites_an_existing_local_file(repo):
    (repo / "factory.toml").write_text('[projects.demo]\nrunner = "pytest"\n',
                                       encoding="utf-8")
    (repo / "factory.local.toml").write_text("# mine\n", encoding="utf-8")

    result = bootstrap.run(repo / "factory.toml", repo)

    assert result["local_created"] is False
    assert (repo / "factory.local.toml").read_text(encoding="utf-8") == "# mine\n"


def test_a_project_with_no_path_is_a_warning_naming_it(repo):
    (repo / "factory.toml").write_text('[projects.blitzvolley]\nrunner = "node"\n',
                                       encoding="utf-8")

    warnings = bootstrap.run(repo / "factory.toml", repo)["warnings"]

    # The whole line, not merely the project name: a bare KeyError('path') out
    # of resolve_project also stringifies to something containing
    # "blitzvolley", and it names neither the cause nor the file to edit.
    assert ("blitzvolley: no path on this machine -- add it to "
            "factory.local.toml") in warnings


def test_an_unset_git_identity_is_a_warning(repo, no_identity):
    # Found the hard way on 2026-08-04: git refused the commit carrying the
    # spec for this very feature.
    (repo / "factory.toml").write_text('[projects.demo]\nrunner = "pytest"\n',
                                       encoding="utf-8")

    warnings = bootstrap.run(repo / "factory.toml", repo)["warnings"]

    assert any("user.email" in w for w in warnings)


def test_a_global_git_identity_satisfies_the_check(repo, tmp_path, monkeypatch):
    """The common case: no local identity, a global one, commits that work.

    Reading `--local` warned here, telling a host whose commits succeed that
    they would be refused. A warning nobody believes is a warning nobody reads.
    """
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[user]\n\tname = factory\n\temail = f@f.f\n",
                         encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "no-such-gitconfig"))
    (repo / "factory.toml").write_text('[projects.demo]\nrunner = "pytest"\n',
                                       encoding="utf-8")

    warnings = bootstrap.run(repo / "factory.toml", repo)["warnings"]

    assert not any("user.email" in w for w in warnings)


def test_a_vitest_project_without_node_modules_is_a_warning(repo, project):
    (repo / "factory.toml").write_text('[projects.web]\nrunner = "vitest"\n',
                                       encoding="utf-8")
    (repo / "factory.local.toml").write_text(
        '[projects.web]\npath = "{}"\n'.format(project.as_posix()),
        encoding="utf-8")

    warnings = bootstrap.run(repo / "factory.toml", repo)["warnings"]

    assert any("web" in w and "node_modules" in w for w in warnings)


def test_an_installed_vitest_project_is_not_a_warning(repo, project):
    (project / "node_modules").mkdir()
    (repo / "factory.toml").write_text('[projects.web]\nrunner = "vitest"\n',
                                       encoding="utf-8")
    (repo / "factory.local.toml").write_text(
        '[projects.web]\npath = "{}"\n'.format(project.as_posix()),
        encoding="utf-8")

    warnings = bootstrap.run(repo / "factory.toml", repo)["warnings"]

    assert not any("node_modules" in w for w in warnings)


def test_a_pathless_vitest_project_reports_only_the_missing_path(repo):
    """One cause, one line. With no path there is nowhere to look for
    node_modules, and two warnings would read as two separate problems."""
    (repo / "factory.toml").write_text('[projects.web]\nrunner = "vitest"\n',
                                       encoding="utf-8")

    warnings = bootstrap.run(repo / "factory.toml", repo)["warnings"]

    assert [w for w in warnings if w.startswith("web:")] == [
        "web: no path on this machine -- add it to factory.local.toml"]


def test_a_missing_llama_server_binary_is_a_warning(repo):
    (repo / "factory.toml").write_text(TRACKED_LLAMA, encoding="utf-8")
    (repo / "factory.local.toml").write_text(LOCAL_LLAMA, encoding="utf-8")

    warnings = bootstrap.run(repo / "factory.toml", repo)["warnings"]

    assert any("llama_server.exe not found: C:/nope/llama-server.exe" in w
               for w in warnings)


def test_missing_model_weights_are_a_warning(repo):
    (repo / "factory.toml").write_text(TRACKED_LLAMA, encoding="utf-8")
    (repo / "factory.local.toml").write_text(LOCAL_LLAMA, encoding="utf-8")

    warnings = bootstrap.run(repo / "factory.toml", repo)["warnings"]

    assert any("qwen3-coder:30b" in w and "weights not found" in w
               for w in warnings)


def test_the_llama_server_stub_reports_itself_unconfigured(repo):
    """Regression for the dead code found reviewing 07d6dcb.

    `run()` writes a `[llama_server]` stub with `exe` commented out -- before
    it checks the runtime, so the section is present and incomplete from the
    very first run onwards. A bare `except Exception` swallowed that as if the
    section were absent, and took the two warnings above down with it: on a
    clone, bootstrap said nothing about the runtime however many times it ran.
    """
    (repo / "factory.toml").write_text('[projects.demo]\nrunner = "pytest"\n',
                                       encoding="utf-8")

    first = bootstrap.run(repo / "factory.toml", repo)["warnings"]
    second = bootstrap.run(repo / "factory.toml", repo)["warnings"]

    assert any("llama_server" in w and "exe" in w for w in first)
    assert any("llama_server" in w and "exe" in w for w in second)


def test_a_malformed_llama_server_section_is_a_warning_not_a_crash(repo):
    """Regression for the re-review of f635736.

    Fixing the swallowed section above narrowed the handler to ConfigError,
    which is not the only way that section fails: load_llama_server coerces
    the port with int(), so a non-numeric one crashed the command outright --
    the same exit-0 hole as before, through a different door.
    """
    # The bad value goes in the tracked half: `port` is doctrine, not a
    # coordinate, so it is the guard of Task 4 that would otherwise catch this
    # first and we would be testing that instead.
    (repo / "factory.toml").write_text(
        '[llama_server]\nport = "eight"\nctx = 32768\n', encoding="utf-8")
    (repo / "factory.local.toml").write_text(
        '[llama_server]\nexe = "C:/nope/llama-server.exe"\n', encoding="utf-8")

    result = bootstrap.run(repo / "factory.toml", repo)

    assert any("llama_server" in w and "malformed" in w
               for w in result["warnings"])


def test_a_coordinate_in_the_tracked_file_is_a_warning_not_a_crash(repo):
    """Always exits 0. The clone whose config does not load is the clone that
    most needs to hear why, and a traceback is not a reason."""
    (repo / "factory.toml").write_text(
        '[projects.demo]\npath = "C:/somewhere"\n', encoding="utf-8")

    result = bootstrap.run(repo / "factory.toml", repo)

    assert result["local_created"] is False
    # Nothing scaffolded out of a config we could not read: never-overwrite
    # would then forbid a later, working run from replacing it.
    assert not (repo / "factory.local.toml").exists()
    assert any("did not load" in w and "machine coordinate" in w
               for w in result["warnings"])


def test_a_typo_in_the_local_file_is_a_warning_not_a_crash(repo):
    (repo / "factory.toml").write_text('[projects.demo]\nrunner = "pytest"\n',
                                       encoding="utf-8")
    (repo / "factory.local.toml").write_text("[projects.demo\npath = 'x'\n",
                                             encoding="utf-8")

    result = bootstrap.run(repo / "factory.toml", repo)

    assert any("did not load" in w for w in result["warnings"])
