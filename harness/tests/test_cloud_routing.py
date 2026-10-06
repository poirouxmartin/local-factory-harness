"""Routing a session to the cloud lane.

The invariant this file protects: a cloud model has no VRAM. It must not take
the GPU lock, must not start a server, must not arm the idle timer -- and must
not be offered a Load button either.
"""
import json

import pytest

import cloud_lane
import factory_config
import factory_mcp
from conftest import write_config


CLOUD = "anthropic/claude-haiku-4-5"
LOCAL = "qwen3-coder:30b"


def _config(tmp_path, cloud_section=True):
    body = """
[llama_server]
port = 8091
ctx = 32768

[llama_server.models."qwen3-coder:30b"]
args = []
"""
    if cloud_section:
        body += """
[cloud.models."anthropic/claude-haiku-4-5"]
label = "Haiku 4.5"

[cloud.models."nvidia/nemotron-3-super-120b-a12b:free"]
tools = false
"""
    return write_config(tmp_path, body, exe="llama-server.exe",
                        models={"qwen3-coder:30b": "model.gguf"})


# ---- the catalogue ----

def test_cloud_models_load_from_the_config(tmp_path):
    models = factory_config.load_cloud_models(_config(tmp_path))
    assert set(models) == {CLOUD, "nvidia/nemotron-3-super-120b-a12b:free"}
    assert models[CLOUD]["label"] == "Haiku 4.5"
    assert models[CLOUD]["tools"] is True
    assert models["nvidia/nemotron-3-super-120b-a12b:free"]["tools"] is False


def test_no_cloud_section_means_no_cloud_lane(tmp_path):
    assert factory_config.load_cloud_models(_config(tmp_path, False)) == {}


def test_a_cloud_id_must_carry_its_vendor(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text('[cloud.models."claude-haiku-4-5"]\n', encoding="utf-8")
    with pytest.raises(factory_config.ConfigError) as excinfo:
        factory_config.load_cloud_models(path)
    assert "vendor/model" in str(excinfo.value)


# ---- the catalogue built from the API ----

def _catalog_config(tmp_path, extra="", **cfg):
    body = """
[llama_server]
port = 8091
ctx = 32768

[llama_server.models."qwen3-coder:30b"]
args = []

[cloud.catalog]
enabled = true
"""
    for k, v in cfg.items():
        body += "{} = {}\n".format(k, json.dumps(v))
    return write_config(tmp_path, body + extra, exe="llama-server.exe",
                        models={"qwen3-coder:30b": "model.gguf"})


def _entry(mid, free=True, tools=True, ctx=262144, label=None):
    return {"id": mid, "label": label or mid, "context_length": ctx,
            "max_output": 8192, "tools": tools, "free": free,
            "usd_per_mtok_in": 0.0, "usd_per_mtok_out": 0.0}


def _catalog(*entries):
    return lambda: (list(entries), {"fetched_at": 1.0, "stale": False,
                                    "source": "network"})


def test_the_catalogue_carries_the_real_window(tmp_path):
    models, meta = factory_config.load_cloud_catalog(
        _catalog_config(tmp_path),
        catalog_fn=_catalog(_entry("nvidia/ultra:free", ctx=1000000)))
    assert models["nvidia/ultra:free"]["context_length"] == 1000000
    assert meta["catalog"] is True and meta["count"] == 1


def test_paid_and_toolless_models_are_filtered_out_by_default(tmp_path):
    """367 models, most of them billable: a studio that offered all of them
    would turn one mis-click into a bill."""
    models, _ = factory_config.load_cloud_catalog(
        _catalog_config(tmp_path),
        catalog_fn=_catalog(_entry("free/withtools"),
                            _entry("paid/model", free=False),
                            _entry("free/notools", tools=False)))
    assert set(models) == {"free/withtools"}


def test_the_filters_are_the_operators_to_relax(tmp_path):
    models, _ = factory_config.load_cloud_catalog(
        _catalog_config(tmp_path, free_only=False, require_tools=False),
        catalog_fn=_catalog(_entry("paid/model", free=False),
                            _entry("free/notools", tools=False)))
    assert set(models) == {"paid/model", "free/notools"}


def test_vendors_can_be_narrowed(tmp_path):
    models, _ = factory_config.load_cloud_catalog(
        _catalog_config(tmp_path, vendors=["nvidia"]),
        catalog_fn=_catalog(_entry("nvidia/a:free"), _entry("cohere/b:free")))
    assert set(models) == {"nvidia/a:free"}


def test_a_pinned_model_survives_a_filter_that_would_drop_it(tmp_path):
    """An explicit choice by the operator outranks a default."""
    path = _catalog_config(tmp_path, extra="""
[cloud.models."anthropic/claude-haiku-4-5"]
label = "Haiku 4.5 (payant)"
""")
    models, _ = factory_config.load_cloud_catalog(
        path, catalog_fn=_catalog(_entry("free/withtools")))
    assert set(models) == {"free/withtools", "anthropic/claude-haiku-4-5"}
    assert models["anthropic/claude-haiku-4-5"]["pinned"] is True
    assert models["free/withtools"]["pinned"] is False


def test_a_pin_keeps_its_label_and_inherits_the_measured_fields(tmp_path):
    path = _catalog_config(tmp_path, extra="""
[cloud.models."nvidia/ultra:free"]
label = "Nemotron 550B (gratuit)"
""")
    models, _ = factory_config.load_cloud_catalog(
        path, catalog_fn=_catalog(_entry("nvidia/ultra:free", ctx=1000000,
                                         label="NVIDIA: Nemotron 3 Ultra")))
    entry = models["nvidia/ultra:free"]
    assert entry["label"] == "Nemotron 550B (gratuit)"   # the operator's word
    assert entry["context_length"] == 1000000            # the network's fact


def test_a_pin_the_filters_dropped_still_gets_its_facts(tmp_path):
    """Pinning a paid model must not be how you lose its real window: the
    filters decide what is offered, not what is known."""
    path = _catalog_config(tmp_path, extra="""
[cloud.models."anthropic/claude-haiku-4-5"]
label = "Haiku 4.5 (payant)"
""")
    models, _ = factory_config.load_cloud_catalog(
        path, catalog_fn=_catalog(_entry("anthropic/claude-haiku-4-5",
                                         free=False, ctx=200000)))
    entry = models["anthropic/claude-haiku-4-5"]
    assert entry["context_length"] == 200000
    assert entry["free"] is False
    assert entry["pinned"] is True


def test_a_pin_the_registry_never_heard_of_is_flagged(tmp_path):
    """`anthropic/claude-haiku-4-5` sat in factory.toml for days; the served id
    is `...-4.5`. A typo must be visible before it becomes a mid-session 404."""
    path = _catalog_config(tmp_path, extra="""
[cloud.models."anthropic/claude-haiku-4-5"]
label = "typo"
""")
    models, _ = factory_config.load_cloud_catalog(
        path, catalog_fn=_catalog(_entry("anthropic/claude-haiku-4.5",
                                         free=False)))
    assert models["anthropic/claude-haiku-4-5"]["unknown"] is True


def test_a_direct_route_pin_is_never_flagged_unknown(tmp_path):
    """OpenRouter's registry cannot vouch for a model Google serves. Judging
    `@gemini/...` by it would mark every direct route as a typo -- exactly the
    ids the registry is structurally unable to list."""
    path = _catalog_config(tmp_path, extra="""
[cloud.models."@gemini/gemini-3.6-flash"]
label = "Gemini 3.6 Flash"
""")
    models, _ = factory_config.load_cloud_catalog(
        path, catalog_fn=_catalog(_entry("anthropic/claude-haiku-4.5")))
    assert models["@gemini/gemini-3.6-flash"]["unknown"] is False


def test_a_direct_route_pin_carries_its_own_window(tmp_path):
    """No registry knows it, so the operator's number is the only one there is;
    without it the model gets budgeted at the local GPU's ceiling."""
    path = _catalog_config(tmp_path, extra="""
[cloud.models."@gemini/gemini-3.6-flash"]
label = "Gemini 3.6 Flash"
context_length = 1048576
""")
    models, _ = factory_config.load_cloud_catalog(
        path, catalog_fn=_catalog(_entry("anthropic/claude-haiku-4.5")))
    assert models["@gemini/gemini-3.6-flash"]["context_length"] == 1048576


def test_a_pin_is_not_called_unknown_when_the_catalogue_is_silent(tmp_path):
    """Offline, we cannot tell a typo from a model we simply cannot see. Saying
    "unknown" there would cry wolf on every plane trip."""
    def down():
        raise RuntimeError("OpenRouter catalogue unreachable: boom")

    path = _catalog_config(tmp_path, extra="""
[cloud.models."anthropic/claude-haiku-4.5"]
label = "Haiku"
""")
    models, _ = factory_config.load_cloud_catalog(path, catalog_fn=down)
    assert models["anthropic/claude-haiku-4.5"]["unknown"] is False


def test_an_outage_keeps_the_pins_and_admits_the_catalogue_is_gone(tmp_path):
    def down():
        raise RuntimeError("OpenRouter catalogue unreachable: boom")

    path = _catalog_config(tmp_path, extra="""
[cloud.models."anthropic/claude-haiku-4-5"]
label = "Haiku 4.5"
""")
    models, meta = factory_config.load_cloud_catalog(path, catalog_fn=down)
    assert set(models) == {"anthropic/claude-haiku-4-5"}
    assert meta["stale"] is True and "boom" in meta["error"]


# ---- the transport choice ----

def test_transport_follows_the_model():
    assert factory_mcp._transport(CLOUD) is cloud_lane
    assert factory_mcp._transport(LOCAL) is factory_mcp.llama_client


# ---- how the turn asks to be accounted for ----

def _body_of(model, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    req = cloud_lane._request(model, [{"role": "user", "content": "hi"}],
                              0.1, None, None)
    return json.loads(req.data.decode("utf-8"))


def test_openrouter_is_asked_for_the_price(monkeypatch):
    body = _body_of(CLOUD, monkeypatch)
    assert body["usage"] == {"include": True}
    assert "stream_options" not in body


def test_a_direct_route_is_asked_the_standard_way(monkeypatch):
    """`usage: {include}` is OpenRouter's own. Google's shim validates the
    payload strictly and answers `Unknown name "usage"` with a 400 -- which
    killed every Gemini session on its first turn."""
    body = _body_of("@gemini/gemini-3.6-flash", monkeypatch)
    assert body["stream_options"] == {"include_usage": True}
    assert "usage" not in body


# ---- the runtime contract ----

@pytest.fixture
def factory(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    f = factory_mcp.Factory(jobs_root=tmp_path / "jobs",
                            config_path=_config(tmp_path))
    return f


def test_a_cloud_model_takes_no_gpu_lock(factory):
    target = factory._acquire_runtime(CLOUD)
    # The model id IS the endpoint for the cloud lane.
    assert target == CLOUD
    assert not factory._runtime_lock.acquired
    assert factory._llama is None      # no server was ever built


def test_a_cloud_model_arms_no_idle_timer(factory):
    factory._acquire_runtime(CLOUD)
    factory._arm_idle()
    assert factory._idle_timer is None


def test_a_cloud_session_is_refused_without_a_key(factory, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        factory._acquire_runtime(CLOUD)
    assert "OPENROUTER_API_KEY" in str(excinfo.value)


def test_the_gpu_stays_free_for_a_job_while_the_cloud_talks(factory):
    """The whole point: a cloud turn and a local job can run at once."""
    factory._acquire_runtime(CLOUD)
    probe = factory_mcp.GpuLock(factory.jobs_root / ".gpu.lock")
    assert probe.acquire(timeout=0)
    probe.release()


# ---- what the UI is told ----

def test_runtime_models_lists_cloud_entries_as_cloud(factory):
    names = {m["name"]: m for m in factory.runtime_models()["models"]}
    assert names[LOCAL]["cloud"] is False
    assert names[CLOUD]["cloud"] is True
    # Nothing to size, nothing to miss, nothing to load: a cloud entry has no
    # file on disk, and the UI keys its Load button off `cloud`.
    assert names[CLOUD]["size"] is None
    assert names[CLOUD]["missing"] is False
    assert names[CLOUD]["loaded"] is False


def test_loading_a_cloud_model_is_refused_in_plain_words(factory):
    with pytest.raises(factory_mcp.ToolError) as excinfo:
        factory.runtime_load(CLOUD)
    assert "no VRAM" in str(excinfo.value)


# ---- whose window is it ----

def test_a_local_model_keeps_the_machines_ceiling(factory):
    assert factory.ctx_ceiling(LOCAL) == factory_mcp.CHAT_NUM_CTX


def test_a_cloud_model_gets_its_own_window(factory, monkeypatch):
    """64k was calibrated for 12 GB of VRAM this model does not use. Applying
    it compacted a 262k window at 64k and paid for summaries nobody needed."""
    monkeypatch.setattr(factory, "_cloud_models",
                        lambda: {CLOUD: {"context_length": 262144}})
    assert factory.ctx_ceiling(CLOUD) == 262144


def test_an_unknown_cloud_window_falls_back_to_the_local_ceiling(factory,
                                                                monkeypatch):
    # Guessing high on an unknown model makes the provider truncate in silence;
    # the conservative number is the honest default.
    monkeypatch.setattr(factory, "_cloud_models", lambda: {CLOUD: {}})
    assert factory.ctx_ceiling(CLOUD) == factory_mcp.CHAT_NUM_CTX


def test_num_ctx_is_bounded_by_the_models_own_ceiling(factory, monkeypatch):
    monkeypatch.setattr(factory, "_cloud_models",
                        lambda: {CLOUD: {"context_length": 262144}})
    session = factory.chat_create(CLOUD)
    sid = session["session_id"]
    factory.chat_set_num_ctx(sid, 131072)          # fine on a cloud model
    with pytest.raises(factory_mcp.ToolError) as excinfo:
        factory.chat_set_num_ctx(sid, 400000)      # above the provider's
    assert "262144" in str(excinfo.value)


def test_a_local_session_cannot_ask_for_more_than_the_gpu_holds(factory):
    sid = factory.chat_create(LOCAL)["session_id"]
    with pytest.raises(factory_mcp.ToolError):
        factory.chat_set_num_ctx(sid, 131072)


def test_a_cloud_model_declared_toolless_cannot_be_an_agent(factory):
    with pytest.raises(factory_mcp.ToolError):
        factory._check_tool_capable("nvidia/nemotron-3-super-120b-a12b:free")
    factory._check_tool_capable(CLOUD)      # tools = true by default
