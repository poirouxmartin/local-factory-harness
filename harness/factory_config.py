"""factory.toml -- the delegation perimeter.

Only projects named here can be delegated to. A project name is a *key*, never a
filesystem path: that is what stops `delegate("../../etc")` from meaning anything.
"""
import re
from collections import namedtuple
from pathlib import Path

import providers
from worktree import forbidden_paths, head_blob_size

try:  # 3.11+
    import tomllib
except ImportError:  # the factory's Python 3.9
    import tomli as tomllib

SUPPORTED_RUNNERS = ("pytest", "vitest", "node")

# The harness writes these into the tests dir. A spec that supplied its own would
# be handing the judge's pen to whoever wrote the spec.
RESERVED_TEST_FILES = ("conftest.py", "pytest.ini", "vitest.config.mjs")

# The 7b runs at num_ctx 16384. Silently overflowing it reproduces ADR-008's
# truncation bug, so we refuse the delegation instead.
CONTEXT_BUDGET = 60000

Project = namedtuple("Project", "name path runner regression_cmd")
McpServerConfig = namedtuple("McpServerConfig", "name command tools readonly env")

# The six tools agent_tools.py always ships. A toolset's `native` list may
# only subset these -- typos or removed tools must fail loudly, not silently
# expose nothing.
NATIVE_TOOLS = ("list_dir", "read_file", "search", "write_file", "edit_file",
               "run_command")

Toolset = namedtuple("Toolset", "name native mcp")


class ConfigError(Exception):
    pass


class ProjectNotAllowed(ConfigError):
    pass


class ProjectNotOnThisMachine(ProjectNotAllowed):
    """Declared in the tracked perimeter, but this computer has no path for it."""


class UnsupportedRunner(ConfigError):
    pass


class UnsafePath(ConfigError):
    pass


class NoTests(ConfigError):
    pass


class NoTargets(ConfigError):
    pass


class ContextTooLarge(ConfigError):
    pass


# The machine-local sibling of factory.toml. Same idea as secrets.toml
# (credentials.path_for): the tracked file says WHAT the factory may touch,
# this one says WHERE it lives on this computer. Never committed.
LOCAL_FILENAME = "factory.local.toml"


def local_config_path(config_path):
    """The machine-local file that sits beside `config_path`."""
    return Path(config_path).resolve().parent / LOCAL_FILENAME


def _merge(base, over):
    """Deep-merge `over` onto `base`.

    Tables merge recursively; scalars and arrays are replaced wholesale. An
    `args` or a `regression_cmd` has to be rewritable entirely -- unioning
    `["--n-cpu-moe", "24"]` with a smaller card's flags would produce a command
    line neither machine asked for.
    """
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _reject_local_only(raw):
    """A machine coordinate in the tracked file is how this broke the first time.

    On 2026-08-04 factory.toml held nine absolute paths; all nine resolved to
    nothing on a second computer, while every project sat right there under
    Projects/. Refusing the key is what keeps the split honest -- documentation
    did not, and `add_project` wrote paths into the tracked file on its own.
    """
    def refuse(key):
        raise ConfigError(
            "{} is a machine coordinate and must not be committed: move it to "
            "{}".format(key, LOCAL_FILENAME))

    for name, entry in (raw.get("projects") or {}).items():
        if isinstance(entry, dict) and "path" in entry:
            refuse("projects.{}.path".format(name))
    server = raw.get("llama_server") or {}
    if "exe" in server:
        refuse("llama_server.exe")
    for name, entry in (server.get("models") or {}).items():
        if isinstance(entry, dict) and "path" in entry:
            refuse('llama_server.models."{}".path'.format(name))


def _load_merged(config_path):
    """The tracked perimeter with this machine's coordinates merged on top.

    A missing local file is not an error: the tracked file alone must stay a
    valid file, or the fragility has only moved. A malformed one is an error --
    swallowing it would silently strip every path on the machine, and the
    failure would surface as "project not a git repo" pointing at nothing.
    """
    with open(config_path, "rb") as f:
        raw = tomllib.load(f)
    _reject_local_only(raw)
    try:
        handle = open(local_config_path(config_path), "rb")
    except OSError:
        return raw
    with handle as f:
        return _merge(raw, tomllib.load(f))


def is_unsafe_path(relpath):
    """True if the path could resolve anywhere but under the directory we hand it."""
    parts = re.split(r"[\\/]", relpath)
    return ":" in relpath or relpath[:1] in ("/", "\\") or ".." in parts or not relpath.strip()


def load_projects(config_path):
    return _load_merged(config_path).get("projects", {})


# Ollama sampling options a [models] profile may set. A typo like `top-p`
# must fail loudly, not silently sample greedy (the July 14 failure mode).
SAMPLING_KEYS = ("temperature", "top_p", "top_k", "min_p",
                 "repeat_penalty", "presence_penalty")


def load_models(config_path):
    """Per-model sampling profiles from [models."name"] tables.

    Quasi-greedy decoding on thinking models is documented (Qwen model
    cards) to cause infinite repetition; profiles carry the card's values
    to every call site instead of hard-coded temperatures.
    """
    raw = _load_merged(config_path).get("models", {})
    profiles = {}
    for name, entry in raw.items():
        bad = set(entry) - set(SAMPLING_KEYS)
        if bad:
            raise ConfigError("[models.{!r}]: unknown option(s): {}".format(
                name, ", ".join(sorted(bad))))
        for key, value in entry.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError("[models.{!r}].{} must be a number".format(
                    name, key))
        profiles[name] = dict(entry)
    return profiles


def model_options(profiles, model):
    """Copy of the model's sampling profile; {} when it has none."""
    return dict(profiles.get(model, {}))


def load_llama_server(config_path):
    """The [llama_server] runtime section, or None when the ladder should
    stay on Ollama. Model keys are the LADDER names (qwen3-coder:30b...);
    each maps a name to a GGUF path plus its measured flags, so switching
    runtime never touches DEFAULT_LADDER or the sampling profiles."""
    raw = _load_merged(config_path).get("llama_server")
    if raw is None:
        return None
    for field in ("exe", "port", "ctx"):
        if field not in raw:
            raise ConfigError("[llama_server] is missing {!r}".format(field))
    models = {}
    for name, entry in (raw.get("models") or {}).items():
        if "path" not in entry:
            raise ConfigError("[llama_server.models.{!r}] is missing 'path'".format(name))
        # tools: can this model actually call a tool? Default yes -- the
        # exception is declared, not the rule. qwen2.5-coder:7b has the Hermes
        # tools block in its template and still answers garbage (E2E audit
        # 2026-07-22 §3), and an agent session on it dies as an empty bubble.
        models[name] = {"path": entry["path"], "args": list(entry.get("args", [])),
                        "tools": bool(entry.get("tools", True))}
    return {"exe": raw["exe"], "port": int(raw["port"]), "ctx": int(raw["ctx"]),
            "chat_idle_s": int(raw.get("chat_idle_s", 600)),
            # 0 = no cap: the RepetitionGuard (degenerate output) is the real
            # runaway brake; this only bounds a long run of unproductive
            # calls. The Stop button is the manual escape. Identical repeats
            # are not braked at all -- the carnet surfaces them instead.
            "agent_max_iterations": int(raw.get("agent_max_iterations", 0)),
            "models": models}


def load_cloud_models(config_path):
    """[cloud.models."<vendor>/<model>"] -- ids the chat lane may address over
    the network. Absent section = no cloud lane: it is opt-in, because unlike a
    local rung every turn on it costs money.

    Keys carry the vendor prefix on purpose: that prefix is what makes an id
    cloud everywhere else (`cloud_lane.is_cloud`), so factory.toml must spell
    it the same way. A key without it would create a model the catalogue lists
    and the router sends to llama-server.
    """
    return load_cloud_catalog(config_path)[0]


def load_cloud_catalog(config_path, catalog_fn=None):
    """The cloud models AND how we came to know them: `(models, meta)`.

    Two sources, merged. `[cloud.models."vendor/id"]` is the hand-written pin
    list -- an operator's explicit choice, always honoured. `[cloud.catalog]`
    opts into the live OpenRouter registry, which is the only thing that knows
    a model's real context window, whether it takes tools, and whether it
    still exists; a hand-written list can only ever guess those.

    A pinned id wins over its catalogue twin for the fields the operator set
    (the label), and inherits the measured ones. The network is never allowed
    to empty the list: on an outage the pins remain, and `meta` says the
    catalogue is stale rather than pretending the missing models were never
    there.
    """
    cloud = _load_merged(config_path).get("cloud") or {}
    raw = cloud.get("models") or {}
    for name in raw:
        if "/" not in name:
            raise ConfigError(
                "[cloud.models] keys must be `vendor/model`, got {!r}".format(name))

    out, meta, known = {}, {"catalog": False}, {}
    cat_cfg = cloud.get("catalog") or {}
    if cat_cfg.get("enabled"):
        out, meta, known = _from_catalog(config_path, cat_cfg, catalog_fn)

    for name, entry in raw.items():
        # A pin the filters dropped (a paid model, say) still gets the facts
        # the registry knows about it -- otherwise pinning a model is how you
        # lose its real context window.
        merged = dict(out.get(name) or known.get(name) or {})
        merged.update({"tools": bool(entry.get("tools",
                                               merged.get("tools", True))),
                       "label": entry.get("label") or merged.get("label")
                       or name,
                       "pinned": True})
        # A direct route has no registry to ask, so its window is only ever
        # what the operator writes down. Without this, `@gemini/...` inherited
        # the 0 below and got budgeted at the local GPU's 64k -- compacting a
        # 1M-token model for nothing, the exact loss `ctx_ceiling` documents.
        if entry.get("context_length"):
            merged["context_length"] = int(entry["context_length"])
        merged.setdefault("context_length", 0)
        merged.setdefault("free", None)      # unknown, not "free"
        # A pin the registry has never heard of is a typo waiting to become a
        # 404 mid-session: `anthropic/claude-haiku-4-5` sat in this file for
        # days when the served id is `...-4.5`. Only claimable when the
        # catalogue actually answered -- and only about ids it could answer
        # for: OpenRouter cannot vouch for `@gemini/...`, which is served by
        # Google, so flagging those would make every direct route look broken.
        merged["unknown"] = bool(meta.get("catalog") and not meta.get("stale")
                                 and not name.startswith(providers.SIGIL)
                                 and name not in known)
        out[name] = merged
    return out, meta


def _from_catalog(config_path, cfg, catalog_fn=None):
    """The registry, filtered by what the operator is willing to address.

    Defaults are deliberately narrow: free and tool-capable. A studio that
    silently offered 367 models -- most of them billable -- would turn one
    mis-click into a bill.
    """
    import openrouter_catalog

    catalog_fn = catalog_fn or (
        lambda: openrouter_catalog.load(config_path))
    try:
        models, meta = catalog_fn()
    except RuntimeError as e:
        return {}, {"catalog": True, "stale": True, "error": str(e),
                    "count": 0}, {}
    free_only = cfg.get("free_only", True)
    require_tools = cfg.get("require_tools", True)
    vendors = cfg.get("vendors") or []
    out, known = {}, {}
    for m in models:
        entry = {"tools": bool(m.get("tools", True)),
                 "label": m.get("label") or m["id"],
                 "context_length": m.get("context_length") or 0,
                 "max_output": m.get("max_output") or 0,
                 "free": bool(m.get("free")),
                 "usd_per_mtok_in": m.get("usd_per_mtok_in", 0.0),
                 "usd_per_mtok_out": m.get("usd_per_mtok_out", 0.0),
                 "pinned": False}
        known[m["id"]] = entry            # everything the registry knows
        if free_only and not m.get("free"):
            continue
        if require_tools and not m.get("tools"):
            continue
        if vendors and m["id"].split("/")[0] not in vendors:
            continue
        out[m["id"]] = entry              # what the operator may address
    meta = dict(meta or {})
    meta.update({"catalog": True, "count": len(out)})
    return out, meta, known


def load_mcp_servers(config_path):
    """[mcp.servers.<name>] tables: the perimeter of external tools, same
    philosophy as [projects] -- only what is named here can ever be spawned."""
    raw = _load_merged(config_path).get("mcp", {}).get("servers", {})
    out = {}
    for name, entry in raw.items():
        command = entry.get("command")
        if not isinstance(command, list) or not command:
            raise ConfigError("[mcp.servers.{}]: command must be a non-empty "
                              "array".format(name))
        tools = list(entry.get("tools") or [])
        readonly = list(entry.get("readonly") or [])
        stray = set(readonly) - set(tools)
        if stray:
            raise ConfigError("[mcp.servers.{}]: readonly lists tools outside "
                              "the allowlist: {}".format(
                                  name, ", ".join(sorted(stray))))
        out[name] = McpServerConfig(name, list(command), tools, readonly,
                                    dict(entry.get("env") or {}))
    return out


def load_toolsets(config_path, mcp_servers=None):
    """[toolsets.<name>]: which native tools and which MCP servers a session
    exposes. "base" (all natives, no MCP) always exists -- it is the current
    behavior and the default."""
    raw = _load_merged(config_path).get("toolsets", {})
    out = {"base": Toolset("base", list(NATIVE_TOOLS), [])}
    for name, entry in raw.items():
        native = list(entry.get("native", NATIVE_TOOLS))
        bad = set(native) - set(NATIVE_TOOLS)
        if bad:
            raise ConfigError("[toolsets.{}]: unknown native tool(s): {}".format(
                name, ", ".join(sorted(bad))))
        mcp = list(entry.get("mcp") or [])
        if mcp_servers is not None:
            missing = set(mcp) - set(mcp_servers)
            if missing:
                raise ConfigError("[toolsets.{}]: unknown mcp server(s): {}".format(
                    name, ", ".join(sorted(missing))))
        out[name] = Toolset(name, native, mcp)
    return out


def resolve_project(projects, name):
    if name not in projects:
        raise ProjectNotAllowed(
            "{!r} is not in factory.toml (allowed: {})".format(name, ", ".join(sorted(projects)) or "none"))
    entry = projects[name]
    if "path" not in entry:
        # The perimeter is tracked, the coordinates are not: a clone knows this
        # project is allowed before it knows where it lives. Raised here rather
        # than filtered out at load, so a forgotten path is a project that says
        # what to write instead of a project that vanished.
        raise ProjectNotOnThisMachine(
            "{!r} is declared in factory.toml but has no path on this machine "
            "-- add [projects.{}] path = \"...\" to {}".format(
                name, name, LOCAL_FILENAME))
    path = Path(entry["path"]).resolve()
    if not (path / ".git").exists():
        raise ProjectNotAllowed("{!r} resolves to {}, which is not a git repo".format(name, path))
    runner = entry.get("runner", "pytest")
    if runner not in SUPPORTED_RUNNERS:
        raise UnsupportedRunner("runner {!r} is not supported yet (have: {})".format(
            runner, ", ".join(SUPPORTED_RUNNERS)))
    return Project(name, path, runner, entry.get("regression_cmd"))


_COLLECTABLE = {
    "pytest": r"test_.+\.py|.+_test\.py",
    "vitest": r".+\.(test|spec)\.[cm]?[jt]sx?",
    "node": r".+\.test\.[cm]?js",
}


def _is_collectable(relpath, runner="pytest"):
    name = re.split(r"[\\/]", relpath)[-1]
    return bool(re.fullmatch(_COLLECTABLE[runner], name))


def validate_tests(tests, runner="pytest"):
    """Test paths must land inside the job directory. No exceptions, no cleverness."""
    if not tests:
        raise NoTests("a delegation must ship tests: they are the only judge")
    for relpath in tests:
        if is_unsafe_path(relpath):
            raise UnsafePath("test path escapes the job directory: {!r}".format(relpath))
        name = re.split(r"[\\/]", relpath)[-1]
        if name in RESERVED_TEST_FILES:
            raise UnsafePath("{!r} is owned by the harness, not by the spec".format(name))
    if not any(_is_collectable(p, runner) for p in tests):
        hints = {"pytest": "test_*.py", "vitest": "*.test.ts", "node": "*.test.js"}
        raise NoTests("none of {} would be collected by {} (need {})".format(
            sorted(tests), runner, hints.get(runner, runner)))


def validate_targets(target_files):
    """The loop may not aim at the thing that grades it."""
    if not target_files:
        raise NoTargets("a delegation must name the files the model may write")
    for relpath in target_files:
        if is_unsafe_path(relpath):
            raise UnsafePath("target escapes the worktree: {!r}".format(relpath))
    judged = forbidden_paths(target_files)
    if judged:
        raise UnsafePath("targets grade the job and cannot be written: {}".format(judged))


def validate_context_files(project, context_files, target_files):
    """Reference files are read-only by construction; here we only check they are sane."""
    total = 0
    for relpath in context_files or []:
        if is_unsafe_path(relpath):
            raise UnsafePath("context path escapes the repo: {!r}".format(relpath))
        if forbidden_paths([relpath]):
            raise UnsafePath("a judge file is not reference material: {!r}".format(relpath))
        if relpath in target_files:
            raise UnsafePath("{!r} cannot be both reference and target".format(relpath))
        size = head_blob_size(project.path, relpath)
        if size is None:
            raise UnsafePath("{!r} is not in HEAD of {}".format(relpath, project.name))
        total += size
    if total > CONTEXT_BUDGET:
        raise ContextTooLarge("context is {} bytes, budget is {}".format(total, CONTEXT_BUDGET))
