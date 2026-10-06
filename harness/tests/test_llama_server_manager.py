"""One llama-server holds one model; the manager swaps servers so the ladder
can keep speaking model NAMES. Spawn/health/kill are injected: what is under
test is the lifecycle contract, not llama-server itself."""
import pytest

import llama_server_manager
from conftest import write_config
from factory_config import load_llama_server
from llama_server_manager import LlamaServerManager, UnknownServerModel

CFG = {
    "exe": "C:/tools/llama-server.exe",
    "port": 8091,
    "ctx": 32768,
    "models": {
        "qwen2.5-coder:7b": {"path": "C:/blobs/7b.gguf", "args": []},
        "qwen3-coder:30b": {"path": "C:/blobs/30b.gguf",
                            "args": ["--n-cpu-moe", "24", "-ub", "2048"]},
    },
}


def test_load_llama_server_returns_none_without_the_section(tmp_path):
    p = write_config(tmp_path, '[projects.demo]\nrunner = "pytest"\n',
                     paths={"demo": "x"}, name="f.toml")
    assert load_llama_server(p) is None


def test_load_llama_server_parses_models_and_defaults(tmp_path):
    p = write_config(
        tmp_path,
        '[llama_server]\nport = 8091\nctx = 32768\n'
        '[llama_server.models."m:a"]\nargs = ["--n-cpu-moe", "24"]\n',
        exe="C:/l.exe", models={"m:a": "C:/a.gguf"}, name="f.toml")
    cfg = load_llama_server(p)
    assert cfg["exe"] == "C:/l.exe"
    assert cfg["models"]["m:a"]["args"] == ["--n-cpu-moe", "24"]
    assert cfg["chat_idle_s"] == 600  # default: the chat gives the GPU back


def test_load_llama_server_reads_chat_idle_s(tmp_path):
    p = write_config(
        tmp_path,
        '[llama_server]\nport = 8091\nctx = 32768\nchat_idle_s = 120\n',
        exe="C:/l.exe", name="f.toml")
    assert load_llama_server(p)["chat_idle_s"] == 120


class Fakes:
    def __init__(self):
        self.spawned, self.killed = [], []
        self.pid = 100

    def spawn(self, argv):
        self.spawned.append(argv)
        self.pid += 1
        return self.pid

    def health(self, url, timeout):
        return True

    def kill(self, pid):
        self.killed.append(pid)

    def probe(self, port):
        return None  # hermetic: the unit tests never ask the real netstat


@pytest.fixture
def fakes():
    return Fakes()


def manager(fakes):
    return LlamaServerManager(CFG, spawn_fn=fakes.spawn,
                              health_fn=fakes.health, kill_fn=fakes.kill,
                              probe_fn=fakes.probe)


def test_ensure_spawns_the_model_server_with_the_guardrail_flags(fakes):
    url = manager(fakes).ensure("qwen3-coder:30b")
    assert url == "http://127.0.0.1:8091"
    argv = fakes.spawned[0]
    assert argv[0] == CFG["exe"]
    assert "C:/blobs/30b.gguf" in argv and "--n-cpu-moe" in argv
    # The two measured bench traps, baked in: one slot (a split context
    # truncates job prompts) and the full flag set the bench validated.
    assert argv[argv.index("--parallel") + 1] == "1"
    assert argv[argv.index("-c") + 1] == "32768"
    assert "-fa" in argv and "q8_0" in argv
    assert "--jinja" in argv
    assert argv[argv.index("--reasoning-format") + 1] == "deepseek"
    assert argv[argv.index("--cache-reuse") + 1] == "256"
    # default lane: q8 KV, ctx from cfg
    assert argv[argv.index("-ctk") + 1] == "q8_0"
    assert argv[argv.index("-ctv") + 1] == "q8_0"


def test_cache_reuse_can_be_dropped_by_env_for_the_ab(fakes, monkeypatch):
    """The 2026-07-23 A/B needs an off arm. Env-gated so nothing production
    changes: without the var the flag is present (asserted above)."""
    monkeypatch.setenv("LLAMA_NO_CACHE_REUSE", "1")
    manager(fakes).ensure("qwen3-coder:30b")
    assert "--cache-reuse" not in fakes.spawned[0]


def test_chat_lane_overrides_kv_quant_and_ctx(fakes):
    # On 12 GB, q8 KV at 64k spills to system RAM (measured 30b: 31 vs 59 tok/s
    # in q4). The chat lane runs a bigger window cheaply; jobs keep q8/32k.
    m = LlamaServerManager(CFG, spawn_fn=fakes.spawn, health_fn=fakes.health,
                           kill_fn=fakes.kill, kv_cache_type="q4_0", ctx=65536,
                           probe_fn=fakes.probe)
    m.ensure("qwen3-coder:30b")
    argv = fakes.spawned[0]
    assert argv[argv.index("-ctk") + 1] == "q4_0"
    assert argv[argv.index("-ctv") + 1] == "q4_0"
    assert argv[argv.index("-c") + 1] == "65536"


def test_ensure_is_idempotent_for_the_same_model(fakes):
    m = manager(fakes)
    m.ensure("qwen3-coder:30b")
    m.ensure("qwen3-coder:30b")
    assert len(fakes.spawned) == 1 and fakes.killed == []


def test_ensure_swaps_servers_between_models(fakes):
    m = manager(fakes)
    m.ensure("qwen2.5-coder:7b")
    m.ensure("qwen3-coder:30b")
    assert len(fakes.spawned) == 2
    assert fakes.killed == [101]  # the 7b server died before the 30b started


def test_unknown_model_is_a_loud_error(fakes):
    with pytest.raises(UnknownServerModel) as e:
        manager(fakes).ensure("qwen3.6:35b")
    assert "qwen3.6:35b" in str(e.value)


def test_shutdown_kills_the_current_server_and_unload_is_scoped(fakes):
    m = manager(fakes)
    m.ensure("qwen2.5-coder:7b")
    m.unload("qwen3-coder:30b")   # not the resident model: no-op
    assert fakes.killed == []
    m.unload("qwen2.5-coder:7b")
    assert fakes.killed == [101]
    m.ensure("qwen3-coder:30b")
    m.shutdown()
    assert fakes.killed == [101, 102]


def test_chat_ensures_then_delegates_to_llama_client(fakes, monkeypatch):
    seen = {}

    def fake_chat(url, system, user, temperature=0.1, num_ctx=None, options=None):
        seen.update(url=url, system=system, user=user,
                    temperature=temperature, options=options)
        return {"content": "ok"}

    monkeypatch.setattr(llama_server_manager.llama_client, "chat", fake_chat)
    m = manager(fakes)
    resp = m.chat("qwen3-coder:30b", "sys", "usr", temperature=0.3,
                  num_ctx=32768, options={"top_p": 0.8})
    assert resp == {"content": "ok"}
    assert seen["url"] == "http://127.0.0.1:8091"
    assert seen["options"] == {"top_p": 0.8}


def test_ensure_reclaims_the_port_from_a_stale_server(fakes):
    """Measured 2026-07-22 on the operator's box: a studio that died left its
    llama-server alive, holding port 8091 and 11.8 GB of VRAM. The next studio
    spawned a second server that could NOT bind, stayed loaded for nothing, and
    every request went to the stranger -- with the stranger's ctx/KV settings,
    silently defeating the per-lane tuning. Health-checking the URL cannot see
    this: the stale server answers /health perfectly."""
    m = LlamaServerManager(CFG, spawn_fn=fakes.spawn, health_fn=fakes.health,
                           kill_fn=fakes.kill, probe_fn=lambda port: 4242)
    m.ensure("qwen2.5-coder:7b")
    assert fakes.killed == [4242], "the squatter must go before we spawn"
    assert len(fakes.spawned) == 1


def test_ensure_does_not_kill_anything_when_the_port_is_free(fakes):
    m = LlamaServerManager(CFG, spawn_fn=fakes.spawn, health_fn=fakes.health,
                           kill_fn=fakes.kill, probe_fn=lambda port: None)
    m.ensure("qwen2.5-coder:7b")
    assert fakes.killed == []


def test_ensure_does_not_reclaim_its_own_running_server(fakes):
    # Swapping models: the port is held by the server we are about to kill
    # ourselves, and it must be killed once, not twice.
    owner = {"pid": None}
    m = LlamaServerManager(CFG, spawn_fn=fakes.spawn, health_fn=fakes.health,
                           kill_fn=fakes.kill, probe_fn=lambda port: owner["pid"])
    m.ensure("qwen2.5-coder:7b")
    owner["pid"] = m.current[1]
    m.ensure("qwen3-coder:30b")
    assert fakes.killed == [101]


def _netstat(lines):
    def run(argv, **kw):
        import types
        if argv[0] == "netstat":
            return types.SimpleNamespace(stdout="\n".join(lines))
        pid = argv[2].split()[-1]
        name = {"4242": "llama-server.exe", "77": "Code.exe"}.get(pid, "x.exe")
        return types.SimpleNamespace(stdout='"{}","{}"\n'.format(name, pid))
    return run


def test_port_owner_finds_the_llama_server_holding_the_port(monkeypatch):
    monkeypatch.setattr(llama_server_manager.subprocess, "run", _netstat([
        "  TCP    0.0.0.0:8091           0.0.0.0:0              LISTENING       4242",
    ]))
    assert llama_server_manager._port_owner(8091) == 4242


def test_port_owner_leaves_a_stranger_alone(monkeypatch):
    # The blast radius of this feature is "we kill a process": it must only
    # ever be one of our own servers, never the editor that happens to listen.
    monkeypatch.setattr(llama_server_manager.subprocess, "run", _netstat([
        "  TCP    0.0.0.0:8091           0.0.0.0:0              LISTENING       77",
    ]))
    assert llama_server_manager._port_owner(8091) is None


def test_port_owner_ignores_another_port_and_non_listening_rows(monkeypatch):
    monkeypatch.setattr(llama_server_manager.subprocess, "run", _netstat([
        "  TCP    0.0.0.0:8787           0.0.0.0:0              LISTENING       4242",
        "  TCP    127.0.0.1:8091         127.0.0.1:5500         ESTABLISHED     4242",
    ]))
    assert llama_server_manager._port_owner(8091) is None
