"""factory.toml is the perimeter.

delegate() refuses anything not on the allowlist, and refuses any test path that
could escape the job directory. A delegation with no tests is refused outright:
tests are the judge, and a job with no judge cannot be verified.
"""
import subprocess

import pytest

import factory_config
from conftest import write_config
from factory_config import (
    ConfigError,
    ContextTooLarge,
    NoTests,
    NoTargets,
    ProjectNotAllowed,
    UnsafePath,
    UnsupportedRunner,
    load_mcp_servers,
    load_models,
    load_projects,
    model_options,
    resolve_project,
    validate_context_files,
    validate_targets,
    validate_tests,
)

TOML = """
[projects.local-factory]
runner = "pytest"
regression_cmd = ["-m", "pytest", "harness/tests", "-q"]

[projects.plain]
runner = "pytest"

[projects.ghost]
runner = "pytest"

[projects.webapp]
runner = "jest"
"""


@pytest.fixture
def config(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "-C", str(root), "init", "-q", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "f@f.f"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "factory"], check=True)
    (root / "calc.py").write_text("x = 1\n")
    (root / "elo.py").write_text("K = 32\n")
    (root / "test_calc.py").write_text("def test_x(): pass\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "seed"], check=True)
    cfg_path = write_config(
        tmp_path, TOML,
        paths={"local-factory": root, "plain": root,
               "ghost": root / "does-not-exist", "webapp": root})
    return load_projects(cfg_path)


def test_an_allowlisted_project_resolves_to_its_path(config, tmp_path):
    project = resolve_project(config, "local-factory")

    assert project.path == (tmp_path / "repo").resolve()
    assert project.runner == "pytest"


def test_a_project_absent_from_the_allowlist_is_refused(config):
    with pytest.raises(ProjectNotAllowed):
        resolve_project(config, "conformergpd")


def test_a_raw_filesystem_path_is_not_a_project_name(config):
    with pytest.raises(ProjectNotAllowed):
        resolve_project(config, "C:/Users/me/lucena")


def test_an_allowlisted_project_whose_path_is_missing_is_refused(config):
    with pytest.raises(ProjectNotAllowed):
        resolve_project(config, "ghost")


def test_a_project_with_an_unsupported_runner_is_refused(config):
    with pytest.raises(UnsupportedRunner):
        resolve_project(config, "webapp")


def test_plain_test_files_are_accepted():
    validate_tests({"test_elo.py": "def test_x(): pass"})


def test_a_helper_module_alongside_a_test_is_accepted():
    validate_tests({"test_elo.py": "def test_x(): pass", "helpers.py": "X = 1"})


def test_a_delegation_without_tests_is_refused():
    with pytest.raises(NoTests):
        validate_tests({})


def test_a_delegation_with_no_collectable_test_file_is_refused():
    with pytest.raises(NoTests):
        validate_tests({"helpers.py": "X = 1"})


@pytest.mark.parametrize("reserved", ["conftest.py", "pytest.ini", "unit/conftest.py"])
def test_a_spec_cannot_overwrite_the_harness_owned_judge_files(reserved):
    """conftest.py and pytest.ini in the tests dir belong to the harness."""
    with pytest.raises(UnsafePath):
        validate_tests({"test_ok.py": "def test_x(): pass", reserved: "boom"})


@pytest.mark.parametrize("bad", [
    "../escape.py",
    "tests/../../escape.py",
    "/etc/passwd",
    "C:/Windows/system32/evil.py",
    "C:\\Windows\\evil.py",
    "\\\\server\\share\\evil.py",
])
def test_a_test_path_that_escapes_the_job_directory_is_refused(bad):
    with pytest.raises(UnsafePath):
        validate_tests({"test_ok.py": "def test_x(): pass", bad: "boom"})


def test_a_nested_test_path_inside_the_job_directory_is_accepted():
    validate_tests({"unit/test_elo.py": "def test_x(): pass"})


def test_ordinary_source_targets_are_accepted():
    validate_targets(["calc.py", "pkg/elo.py"])


def test_a_delegation_with_no_target_is_refused():
    with pytest.raises(NoTargets):
        validate_targets([])


@pytest.mark.parametrize("judge", ["test_calc.py", "conftest.py", "pytest.ini", "src/test_x.py"])
def test_a_target_that_is_the_judge_is_refused(judge):
    """The loop must not be allowed to aim at the thing that grades it."""
    with pytest.raises(UnsafePath):
        validate_targets(["calc.py", judge])


@pytest.mark.parametrize("bad", ["../escape.py", "/etc/passwd", "C:/Windows/evil.py"])
def test_a_target_outside_the_worktree_is_refused(bad):
    with pytest.raises(UnsafePath):
        validate_targets([bad])


def test_a_project_carries_its_regression_command(config):
    project = resolve_project(config, "local-factory")

    assert project.regression_cmd == ["-m", "pytest", "harness/tests", "-q"]


def test_a_project_without_a_regression_command_has_none(config):
    assert resolve_project(config, "plain").regression_cmd is None


def test_context_files_from_head_are_accepted(config):
    project = resolve_project(config, "local-factory")

    validate_context_files(project, ["elo.py"], ["calc.py"])


def test_no_context_files_is_fine(config):
    project = resolve_project(config, "local-factory")

    validate_context_files(project, [], ["calc.py"])
    validate_context_files(project, None, ["calc.py"])


def test_a_context_file_absent_from_head_is_refused(config):
    project = resolve_project(config, "local-factory")

    with pytest.raises(UnsafePath):
        validate_context_files(project, ["typo.py"], ["calc.py"])


def test_a_context_file_that_is_also_a_target_is_refused(config):
    """A file is readable or writable, not both."""
    project = resolve_project(config, "local-factory")

    with pytest.raises(UnsafePath):
        validate_context_files(project, ["calc.py"], ["calc.py"])


def test_a_judge_file_is_not_reference_material(config):
    project = resolve_project(config, "local-factory")

    with pytest.raises(UnsafePath):
        validate_context_files(project, ["test_calc.py"], ["calc.py"])


@pytest.mark.parametrize("bad", ["../escape.py", "/etc/passwd", "C:/Windows/evil.py"])
def test_a_context_path_escaping_the_repo_is_refused(config, bad):
    project = resolve_project(config, "local-factory")

    with pytest.raises(UnsafePath):
        validate_context_files(project, [bad], ["calc.py"])


def test_context_over_the_budget_is_refused(config, tmp_path):
    project = resolve_project(config, "local-factory")
    big = tmp_path / "repo" / "big.py"
    big.write_text("x = 1\n" * 20000)
    subprocess.run(["git", "-C", str(project.path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(project.path), "commit", "-qm", "big"], check=True)

    with pytest.raises(ContextTooLarge):
        validate_context_files(project, ["big.py"], ["calc.py"])


def test_vitest_specs_are_collectable_and_pytest_ones_are_not():
    from factory_config import validate_tests, NoTests
    validate_tests({"add.test.ts": "x"}, runner="vitest")
    validate_tests({"add.spec.tsx": "x"}, runner="vitest")
    with pytest.raises(NoTests):
        validate_tests({"test_add.py": "x"}, runner="vitest")
    with pytest.raises(NoTests):
        validate_tests({"add.test.ts": "x"}, runner="pytest")


def test_the_vitest_config_is_reserved():
    from factory_config import validate_tests, UnsafePath
    with pytest.raises(UnsafePath):
        validate_tests({"vitest.config.mjs": "x", "a.test.ts": "y"},
                       runner="vitest")


def test_load_models_returns_profiles(tmp_path):
    cfg = tmp_path / "factory.toml"
    cfg.write_text('[models."qwen3.6:35b"]\n'
                   'temperature = 0.6\ntop_p = 0.95\ntop_k = 20\n'
                   'presence_penalty = 1.0\n', encoding="utf-8")

    models = load_models(cfg)

    assert models == {"qwen3.6:35b": {"temperature": 0.6, "top_p": 0.95,
                                      "top_k": 20, "presence_penalty": 1.0}}


def test_load_models_refuses_unknown_keys(tmp_path):
    cfg = tmp_path / "factory.toml"
    cfg.write_text('[models."m:1"]\ntop-p = 0.9\n', encoding="utf-8")

    with pytest.raises(ConfigError):
        load_models(cfg)


def test_load_models_refuses_non_numeric_values(tmp_path):
    cfg = tmp_path / "factory.toml"
    cfg.write_text('[models."m:1"]\ntemperature = "hot"\n', encoding="utf-8")

    with pytest.raises(ConfigError):
        load_models(cfg)


def test_load_models_refuses_boolean_values(tmp_path):
    cfg = tmp_path / "factory.toml"
    cfg.write_text('[models."m:1"]\ntemperature = true\n', encoding="utf-8")

    with pytest.raises(ConfigError):
        load_models(cfg)


def test_model_options_copies_and_defaults_empty():
    profiles = {"m:1": {"temperature": 0.6}}

    opts = model_options(profiles, "m:1")
    opts["temperature"] = 999  # mutating the copy must not touch the profile

    assert profiles["m:1"]["temperature"] == 0.6
    assert model_options(profiles, "unknown") == {}


def test_load_mcp_servers_parses_and_validates(tmp_path):
    cfg = tmp_path / "factory.toml"
    cfg.write_text('[mcp.servers.git]\n'
                   'command = ["uvx", "mcp-server-git"]\n'
                   'tools = ["git_status", "git_commit"]\n'
                   'readonly = ["git_status"]\n', encoding="utf-8")

    servers = load_mcp_servers(cfg)

    assert servers["git"].command == ["uvx", "mcp-server-git"]
    assert servers["git"].tools == ["git_status", "git_commit"]
    assert servers["git"].readonly == ["git_status"]


def test_load_mcp_servers_refuses_readonly_outside_tools(tmp_path):
    cfg = tmp_path / "factory.toml"
    cfg.write_text('[mcp.servers.x]\ncommand = ["x"]\ntools = ["a"]\n'
                   'readonly = ["b"]\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_mcp_servers(cfg)


def test_load_mcp_servers_refuses_a_missing_command(tmp_path):
    cfg = tmp_path / "factory.toml"
    cfg.write_text('[mcp.servers.x]\ntools = ["a"]\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_mcp_servers(cfg)


def test_load_toolsets_always_has_base_and_validates(tmp_path):
    cfg = tmp_path / "factory.toml"
    cfg.write_text('[mcp.servers.git]\ncommand = ["uvx", "mcp-server-git"]\n'
                   'tools = ["git_status"]\n'
                   '[toolsets.dev]\nmcp = ["git"]\n', encoding="utf-8")

    ts = factory_config.load_toolsets(
        cfg, mcp_servers=factory_config.load_mcp_servers(cfg))

    assert ts["base"].native == list(factory_config.NATIVE_TOOLS)
    assert ts["base"].mcp == []
    assert ts["dev"].native == list(factory_config.NATIVE_TOOLS)  # default
    assert ts["dev"].mcp == ["git"]


def test_load_toolsets_refuses_unknown_native_or_server(tmp_path):
    cfg = tmp_path / "factory.toml"
    cfg.write_text('[toolsets.bad]\nnative = ["rm_rf"]\n', encoding="utf-8")
    with pytest.raises(factory_config.ConfigError):
        factory_config.load_toolsets(cfg)

    cfg.write_text('[toolsets.bad]\nmcp = ["ghost"]\n', encoding="utf-8")
    with pytest.raises(factory_config.ConfigError):
        factory_config.load_toolsets(cfg, mcp_servers={})


def test_node_runner_is_supported_and_collects_test_js(tmp_path):
    assert "node" in factory_config.SUPPORTED_RUNNERS
    factory_config.validate_tests(["spec.test.js"], "node")  # no raise
    with pytest.raises(factory_config.NoTests):
        factory_config.validate_tests(["spec.js"], "node")
