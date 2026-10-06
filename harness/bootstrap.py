"""What a fresh clone needs before the factory can do anything.

Cloning this repo onto a second computer produced a factory that loaded, said
nothing, and could not delegate: every path in the tracked config pointed at
the first machine, the git hooks were unarmed, and git had no identity to
commit with. Documentation covered two of those three and none of them held.

Idempotent by construction: arms what is not armed, creates only what is
absent, and reports the rest instead of guessing. Always succeeds -- this is a
setup helper, not a gate. Nothing here may raise: the command that explains a
broken clone is the one command a broken clone has to be able to run.
"""
import subprocess
from pathlib import Path

from factory_config import (LOCAL_FILENAME, ConfigError, load_llama_server,
                            load_projects, local_config_path, resolve_project)

# Every way the config can fail to yield a project table: a coordinate left in
# the tracked file (ConfigError), a TOML typo in either half (TOMLDecodeError,
# which is a ValueError), a file that cannot be opened (OSError). All three are
# ordinary states of the clone this command exists to repair, so all three are
# reported rather than raised.
UNREADABLE = (ConfigError, ValueError, OSError)

HEADER = """# This machine's coordinates: where things live on this computer.
# Never committed -- factory.toml carries the perimeter and the doctrine,
# this file carries the paths. Uncomment and fill what this machine has.
"""


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)


def _identity_warning(repo_root):
    """Missing git identity, resolved the way git itself resolves it.

    Read with `--local` this fired on every machine whose identity is merely
    global, which is most of them: it told a host with perfectly working
    commits that its commits would be refused. A warning nobody believes is a
    warning nobody reads, so the effective value is what counts.
    """
    missing = [key for key in ("user.name", "user.email")
               if not _git(repo_root, "config", key).stdout.strip()]
    if not missing:
        return None
    return ("git has no {} on this clone; commits will be refused "
            "(git config user.email \"...\" and user.name \"...\")".format(
                " or ".join(missing)))


def run(config_path, repo_root):
    """Arm the hooks, scaffold the local config, report what is missing."""
    config_path, repo_root = Path(config_path), Path(repo_root)
    warnings = []

    _git(repo_root, "config", "core.hooksPath", ".githooks")

    identity = _identity_warning(repo_root)
    if identity:
        warnings.append(identity)

    try:
        projects = load_projects(config_path)
    except UNREADABLE as e:
        # Stop here rather than scaffold: a stub written from a config we could
        # not read would be a file the never-overwrite rule then forbids a
        # later, working run from replacing.
        warnings.append("{} did not load, so nothing under it could be "
                        "checked: {}".format(config_path.name, e))
        return {"hooks_path": ".githooks", "local_created": False,
                "warnings": warnings}

    local = local_config_path(config_path)
    created = not local.exists()
    if created:
        body = [HEADER]
        for name in sorted(projects):
            body.append("[projects.{}]\n# path = \"C:/path/to/{}\"\n".format(
                name, name))
        body.append("[llama_server]\n# exe = \"C:/path/to/llama-server.exe\"\n")
        local.write_text("\n".join(body), encoding="utf-8")

    for name in sorted(projects):
        warnings.extend(_project_warnings(projects, name))

    warnings.extend(_runtime_warnings(config_path))
    return {"hooks_path": ".githooks", "local_created": created,
            "warnings": warnings}


def _project_warnings(projects, name):
    """What this machine is missing for one declared project."""
    entry = projects[name] or {}
    path = entry.get("path")
    if not path:
        # The expected state of a fresh clone, not a failure: the perimeter
        # names the project, this machine has yet to say where it lives.
        # Checked before resolve_project because that one raises a bare
        # KeyError('path') here, which names the key and not the project.
        # Nothing further is reported for it -- an absent path is the cause of
        # everything else that would be wrong with it, and one cause is one line.
        return ["{}: no path on this machine -- add it to {}".format(
            name, LOCAL_FILENAME)]

    out = []
    try:
        resolve_project(projects, name)
    except Exception as e:                    # not a git repo, bad runner...
        out.append("{}: {}".format(name, e))
    if entry.get("runner") == "vitest" and not (Path(path) / "node_modules").is_dir():
        # A vitest project that has never been installed fails its first
        # delegation on `vitest: not found`, which reads as a broken factory
        # rather than as a clone one `npm install` away from working.
        out.append("{}: no node_modules under {} -- run `npm install` there".format(
            name, path))
    return out


def _runtime_warnings(config_path):
    """llama-server and its weights: gigabytes this cannot install."""
    out = []
    try:
        server = load_llama_server(config_path)
    except ConfigError as e:
        # An incomplete [llama_server] is precisely what the stub above writes,
        # so this is the fresh-clone path, not an exotic one. Swallowing it
        # here made the two warnings below unreachable on the only machine that
        # needed them: bootstrap run twice on a clone reported nothing at all
        # about the runtime (review of 07d6dcb). A section that is simply
        # absent returns None below and stays silent -- that is the Ollama
        # lane, and it is not the same thing as a section that is half-written.
        return ["llama_server: {}".format(e)]
    except Exception as e:
        # Not every bad section is a ConfigError: load_llama_server coerces
        # port and ctx with int() and indexes each model entry, so a
        # non-numeric port or a model that is not a table arrives here as a
        # ValueError, a TypeError or an AttributeError. Narrowing this handler
        # to ConfigError to fix the dead code above closed one exit-0 hole and
        # left those doors open (re-review of f635736), so the catch is broad
        # again -- but it reports instead of returning silently, which is what
        # made the previous bare `except` a bug. Kept below the ConfigError
        # case so the fresh-clone wording above survives.
        return ["llama_server: section is malformed ({}: {})".format(
            type(e).__name__, e)]
    if server is None:
        return out                            # no [llama_server]: Ollama lane
    if not Path(server["exe"]).exists():
        out.append("llama_server.exe not found: {}".format(server["exe"]))
    for name, entry in sorted(server["models"].items()):
        if not Path(entry["path"]).exists():
            out.append("model {}: weights not found at {}".format(
                name, entry["path"]))
    return out
