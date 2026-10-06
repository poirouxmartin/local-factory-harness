"""factory.toml carries the perimeter; factory.local.toml carries this machine.

Spec: docs/design/specs/2026-08-04-machine-local-config-design.md
"""
import pytest

from factory_config import ConfigError, _load_merged, local_config_path


def write(tmp_path, tracked, local=None):
    cfg = tmp_path / "factory.toml"
    cfg.write_text(tracked, encoding="utf-8")
    if local is not None:
        (tmp_path / "factory.local.toml").write_text(local, encoding="utf-8")
    return cfg


def test_the_local_file_is_a_sibling_of_the_tracked_one(tmp_path):
    cfg = tmp_path / "factory.toml"

    assert local_config_path(cfg) == tmp_path / "factory.local.toml"


def test_a_missing_local_file_leaves_the_tracked_file_untouched(tmp_path):
    cfg = write(tmp_path, '[projects.demo]\nrunner = "pytest"\n')

    assert _load_merged(cfg) == {"projects": {"demo": {"runner": "pytest"}}}


def test_a_local_scalar_overrides_the_tracked_one(tmp_path):
    cfg = write(tmp_path,
                '[llama_server]\nctx = 32768\nport = 8091\n',
                '[llama_server]\nctx = 65536\n')

    merged = _load_merged(cfg)

    assert merged["llama_server"]["ctx"] == 65536
    assert merged["llama_server"]["port"] == 8091


def test_a_local_array_replaces_the_tracked_one_wholesale(tmp_path):
    cfg = write(tmp_path,
                '[llama_server.models."m"]\nargs = ["--n-cpu-moe", "24"]\n',
                '[llama_server.models."m"]\nargs = ["--n-cpu-moe", "8"]\n')

    assert _load_merged(cfg)["llama_server"]["models"]["m"]["args"] == \
        ["--n-cpu-moe", "8"]


def test_a_key_only_in_the_local_file_is_added(tmp_path):
    cfg = write(tmp_path, '[projects.demo]\nrunner = "pytest"\n',
                '[projects.demo]\npath = "C:/somewhere"\n')

    demo = _load_merged(cfg)["projects"]["demo"]

    assert demo == {"runner": "pytest", "path": "C:/somewhere"}


def test_tables_merge_recursively(tmp_path):
    cfg = write(tmp_path, '[projects.a]\nrunner = "pytest"\n',
                '[projects.b]\npath = "C:/b"\n')

    projects = _load_merged(cfg)["projects"]

    assert sorted(projects) == ["a", "b"]
    assert projects["a"] == {"runner": "pytest"}


def test_a_malformed_local_file_is_not_swallowed(tmp_path):
    cfg = write(tmp_path, '[projects.demo]\nrunner = "pytest"\n', "this is not toml")

    # A typo in the local file must be read out loud. Only a MISSING file is
    # the silent case; a broken one that loaded as {} would strip every path
    # on the machine and blame the projects.
    with pytest.raises(ValueError):
        _load_merged(cfg)


def test_a_local_path_reaches_load_projects(tmp_path):
    cfg = write(tmp_path,
                '[projects.demo]\nrunner = "vitest"\n',
                '[projects.demo]\npath = "C:/elsewhere"\n')

    from factory_config import load_projects

    assert load_projects(cfg)["demo"] == {"runner": "vitest",
                                          "path": "C:/elsewhere"}


def test_a_local_override_reaches_load_llama_server(tmp_path):
    cfg = write(tmp_path,
                '[llama_server]\nport = 8091\nctx = 32768\n'
                '[llama_server.models."m"]\nargs = ["--n-cpu-moe", "24"]\n',
                '[llama_server]\nexe = "C:/llama-server.exe"\nctx = 16384\n'
                '[llama_server.models."m"]\npath = "C:/m.gguf"\n')

    from factory_config import load_llama_server

    cfg_out = load_llama_server(cfg)

    assert cfg_out["exe"] == "C:/llama-server.exe"
    assert cfg_out["ctx"] == 16384          # local wins
    assert cfg_out["port"] == 8091          # tracked default survives
    assert cfg_out["models"]["m"] == {"path": "C:/m.gguf",
                                      "args": ["--n-cpu-moe", "24"],
                                      "tools": True}


def test_a_local_file_can_override_a_sampling_profile(tmp_path):
    cfg = write(tmp_path, '[models."q"]\ntemperature = 0.7\n',
                '[models."q"]\ntemperature = 0.2\n')

    from factory_config import load_models

    assert load_models(cfg)["q"]["temperature"] == 0.2


def test_a_project_path_in_the_tracked_file_is_refused(tmp_path):
    cfg = write(tmp_path, '[projects.demo]\npath = "C:/demo"\nrunner = "pytest"\n')

    with pytest.raises(ConfigError) as e:
        _load_merged(cfg)

    assert "projects.demo.path" in str(e.value)
    assert "factory.local.toml" in str(e.value)


def test_the_llama_server_exe_in_the_tracked_file_is_refused(tmp_path):
    cfg = write(tmp_path, '[llama_server]\nexe = "C:/llama-server.exe"\n')

    with pytest.raises(ConfigError) as e:
        _load_merged(cfg)

    assert "llama_server.exe" in str(e.value)


def test_a_model_path_in_the_tracked_file_is_refused(tmp_path):
    cfg = write(tmp_path, '[llama_server.models."m"]\npath = "C:/m.gguf"\n')

    with pytest.raises(ConfigError) as e:
        _load_merged(cfg)

    assert 'llama_server.models."m".path' in str(e.value)


def test_the_same_keys_are_welcome_in_the_local_file(tmp_path):
    cfg = write(tmp_path, '[projects.demo]\nrunner = "pytest"\n',
                '[projects.demo]\npath = "C:/demo"\n'
                '[llama_server]\nexe = "C:/llama-server.exe"\n')

    merged = _load_merged(cfg)

    assert merged["projects"]["demo"]["path"] == "C:/demo"
    assert merged["llama_server"]["exe"] == "C:/llama-server.exe"


def test_a_project_with_no_local_path_says_what_to_write(tmp_path):
    from factory_config import (ProjectNotOnThisMachine, load_projects,
                                resolve_project)

    cfg = write(tmp_path, '[projects.blitzvolley]\nrunner = "node"\n')

    with pytest.raises(ProjectNotOnThisMachine) as e:
        resolve_project(load_projects(cfg), "blitzvolley")

    message = str(e.value)
    assert "blitzvolley" in message
    assert "factory.local.toml" in message


def test_it_is_still_a_config_error_for_every_existing_handler(tmp_path):
    from factory_config import ProjectNotAllowed, ProjectNotOnThisMachine

    # Ten `except ConfigError` sites outside factory_config.py rely on this
    # (verified 2026-08-04); none catches ProjectNotAllowed by name.
    assert issubclass(ProjectNotOnThisMachine, ProjectNotAllowed)
    assert issubclass(ProjectNotOnThisMachine, ConfigError)
