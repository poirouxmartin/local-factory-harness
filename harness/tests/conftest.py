import subprocess
import sys
from pathlib import Path

import pytest

# harness modules import each other by bare name (see llama_client.py usage in
# loop.py), so the harness dir must be importable when pytest drives them.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "browser: drives a real browser or a live studio; excluded from the "
        "push gate (.githooks/pre-push), run it with -m browser")


# pytest-playwright's own fixtures: asking for one of these IS asking for a
# browser, even in a module that never names playwright itself.
BROWSER_FIXTURES = frozenset(("page", "browser", "context", "playwright",
                              "browser_context", "new_page"))


def _drives_a_browser(module):
    """Does this module hold anything that came out of playwright?"""
    for value in vars(module).values():
        origin = getattr(value, "__module__", None) or getattr(value, "__name__", "")
        if isinstance(origin, str) and origin.startswith("playwright"):
            return True
    return False


def pytest_collection_modifyitems(items):
    """Mark browser tests, so the gate has no list to keep up to date.

    Forgetting the marker used to hang the push for ten minutes, which is worse
    than no gate -- nobody waits for it twice. Importing playwright is the one
    signal that cannot be forgotten: a module that drives a browser has to. A
    test that needs a live server without playwright still declares
    `pytestmark = pytest.mark.browser` itself.
    """
    for item in items:
        module = getattr(item, "module", None)
        wants = BROWSER_FIXTURES.intersection(getattr(item, "fixturenames", ()))
        if wants or (module is not None and _drives_a_browser(module)):
            item.add_marker(pytest.mark.browser)


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)


def write_config(tmp_path, tracked, paths=None, models=None, exe=None,
                 name="factory.toml"):
    """Write the two files the factory reads, return the tracked one.

    `tracked` is the perimeter: runners, regression commands, sampling, the
    things a repo carries. `paths` ({project: path}), `models` ({model: path})
    and `exe` are machine coordinates and go to factory.local.toml, which is
    where a coordinate is allowed to exist
    (spec 2026-08-04-machine-local-config-design.md).

    `name` exists because not every fixture calls its file factory.toml --
    test_llama_server_manager.py uses f.toml. The local sibling is always
    factory.local.toml regardless, which is what local_config_path computes.
    """
    cfg = tmp_path / name
    cfg.write_text(tracked, encoding="utf-8")
    body = []
    if exe is not None:
        body.append("[llama_server]")
        body.append('exe = "{}"'.format(exe))
    for project, path in (paths or {}).items():
        body.append("[projects.{}]".format(project))
        body.append('path = "{}"'.format(Path(path).as_posix()))
    for model, path in (models or {}).items():
        body.append('[llama_server.models."{}"]'.format(model))
        body.append('path = "{}"'.format(Path(path).as_posix()))
    (tmp_path / "factory.local.toml").write_text(
        "\n".join(body) + "\n", encoding="utf-8")
    return cfg


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "f@f.f")
    git(root, "config", "user.name", "factory")
    (root / "calc.py").write_text("x = 1\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "seed")
    return root
