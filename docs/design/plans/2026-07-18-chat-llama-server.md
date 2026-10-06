# Chat/studio -> llama-server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrer le chat et le mode agent du studio d'Ollama vers llama-server (zéro Ollama dans le code), avec cycle de vie « lock = VRAM » et prompt byte-stable.

**Architecture:** `llama_client` gagne un `chat_stream` SSE au contrat identique à `ollama_client.chat_stream` ; `factory_mcp` possède un `LlamaServerManager` dont la durée de vie est liée au GPU lock (idle-timeout) ; l'UI modèles bascule sur le catalogue `[llama_server.models]` de factory.toml. Spec : `docs/design/specs/2026-07-18-chat-llama-server-design.md`.

**Tech Stack:** Python 3.9 stdlib only (pas de deps tierces), pytest, vanilla JS (harness/web), llama.cpp `llama-server`.

## Global Constraints

- Python 3.9, stdlib uniquement (règle harness, cf. en-têtes de modules).
- Commits directs sur `main` (convention projet), conventionnels, ASCII, sans attribution IA.
- Textes UI en français (convention app.js existante).
- Une variable à la fois ; tout échec au banc -> autopsie avant conclusion.
- Runs GPU longs (regress, bench) : lancés détachés par l'opérateur, jamais via l'infra de tâches (CLAUDE.md projet).
- Lancer les tests : `py -3 -m pytest harness/tests -q` depuis la racine du repo.
- Le contrat de streaming consommé par `factory_mcp`/`factory_web` est intouchable : yields `("thinking", str)`, `("content", str)`, `("tool_call", {"name": str, "arguments": dict})`, retour StopIteration.value = dict métriques avec les clés `content, done_reason, eval_count, tokens_per_s, prefill_count, prefill_tokens_per_s, ttft_s, thinking_chars, content_chars, thinking_s, writing_s` et `stopped` optionnel (`"repetition"` | `"length"`).

---

### Task 1: Extraire RepetitionGuard dans un module neutre

`ollama_client.py` sera supprimé (Task 8) mais `RepetitionGuard` doit survivre : le nouveau `llama_client.chat_stream` s'en sert.

**Files:**
- Create: `harness/repetition_guard.py`
- Modify: `harness/ollama_client.py` (remplacer la classe par un ré-export)
- Create: `harness/tests/test_repetition_guard.py`
- Modify: `harness/tests/test_ollama_client.py` (supprimer les 4 tests guard, lignes ~252-280)

**Interfaces:**
- Produces: `repetition_guard.RepetitionGuard` — mêmes seuils/API qu'aujourd'hui : `feed(text, thinking=False) -> bool`, attributs de classe `WINDOW=40`, `MIN_UNIQUE=8`, `THINKING_CAP=120_000`.

- [ ] **Step 1: Créer le module**

Déplacer la classe `RepetitionGuard` (docstring de classe comprise, `ollama_client.py:142-177`) telle quelle dans `harness/repetition_guard.py`, avec en tête :

```python
"""Degenerate-generation detector, shared by every streaming client.

Lives outside any runtime client: it judges text, not transports.
"""
from collections import deque
```

- [ ] **Step 2: Ré-exporter depuis ollama_client**

Dans `harness/ollama_client.py`, remplacer la définition de classe par :

```python
from repetition_guard import RepetitionGuard  # noqa: F401 -- re-export until retirement
```

(supprimer `from collections import deque` devenu inutile).

- [ ] **Step 3: Déplacer les tests**

Créer `harness/tests/test_repetition_guard.py` avec les 4 tests guard de `test_ollama_client.py` (lignes ~252-280), en remplaçant `ollama_client.RepetitionGuard` par `RepetitionGuard` importé de `repetition_guard`. Supprimer ces 4 tests de `test_ollama_client.py`.

- [ ] **Step 4: Vérifier**

Run: `py -3 -m pytest harness/tests/test_repetition_guard.py harness/tests/test_ollama_client.py -q`
Expected: PASS (4 tests dans le nouveau fichier, aucun guard restant dans l'ancien).

- [ ] **Step 5: Commit**

```bash
git add harness/repetition_guard.py harness/ollama_client.py harness/tests/test_repetition_guard.py harness/tests/test_ollama_client.py
git commit -m "refactor: move RepetitionGuard to its own module"
```

---

### Task 2: `llama_client.chat_stream` (SSE, tools, thinking)

**Files:**
- Modify: `harness/llama_client.py`
- Test: `harness/tests/test_llama_client.py` (étendre le `FakeLlamaServer` existant au SSE)

**Interfaces:**
- Consumes: `repetition_guard.RepetitionGuard` (Task 1).
- Produces: `llama_client.chat_stream(base_url, messages, temperature=0.7, num_ctx=None, timeout=900, tools=None, options=None)` — générateur au contrat des Global Constraints. `num_ctx` accepté et ignoré (flag serveur), comme dans `chat()`. Les messages acceptés sont au format wire actuel de `factory_mcp` (assistant avec `tool_calls: [{"function": {"name", "arguments": dict}}]`, messages `{"role": "tool", "tool_name", "content"}`).

- [ ] **Step 1: Écrire les tests qui échouent**

Ajouter à `harness/tests/test_llama_client.py` un fake SSE et les tests. Le fake réutilise le pattern `FakeLlamaServer` (classe à part pour ne pas casser les tests non-stream) :

```python
class FakeSseServer(BaseHTTPRequestHandler):
    seen = None
    chunks = None  # list of dicts to stream as "data: ..." lines

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeSseServer.seen = {"path": self.path, "body": body}
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for c in FakeSseServer.chunks:
            self.wfile.write(b"data: " + json.dumps(c).encode("utf-8") + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *a):
        pass


@pytest.fixture
def sse_server():
    httpd = HTTPServer(("127.0.0.1", 0), FakeSseServer)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield "http://127.0.0.1:{}".format(httpd.server_port)
    httpd.shutdown()


def _drain(gen):
    events, metrics = [], None
    while True:
        try:
            events.append(next(gen))
        except StopIteration as stop:
            metrics = stop.value
            break
    return events, metrics


def test_stream_yields_deltas_and_maps_timings(sse_server):
    FakeSseServer.chunks = [
        {"choices": [{"delta": {"reasoning_content": "hmm"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "hel"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}],
         "timings": {"predicted_per_second": 60.0, "prompt_per_second": 900.0,
                     "prompt_ms": 500.0, "predicted_n": 2, "prompt_n": 40}},
    ]
    events, m = _drain(llama_client.chat_stream(
        sse_server, [{"role": "user", "content": "hi"}]))
    assert ("thinking", "hmm") in events
    assert ("content", "hel") in events and ("content", "lo") in events
    assert m["content"] == "hello"
    assert m["done_reason"] == "stop"
    assert m["tokens_per_s"] == 60.0
    assert m["prefill_tokens_per_s"] == 900.0
    assert m["prefill_count"] == 40
    assert m["eval_count"] == 2
    assert m["ttft_s"] == pytest.approx(0.5)
    assert m["thinking_chars"] == 3 and m["content_chars"] == 5
    assert FakeSseServer.seen["body"]["stream"] is True


def test_stream_accumulates_fragmented_tool_calls(sse_server):
    FakeSseServer.chunks = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"name": "read_file", "arguments": "{\"pa"}}]},
            "finish_reason": None}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "th\": \"a.py\"}"}}]},
            "finish_reason": "tool_calls"}],
         "timings": {"predicted_n": 5}},
    ]
    events, m = _drain(llama_client.chat_stream(
        sse_server, [{"role": "user", "content": "go"}],
        tools=[{"type": "function", "function": {"name": "read_file"}}]))
    assert ("tool_call", {"name": "read_file",
                          "arguments": {"path": "a.py"}}) in events
    assert m["done_reason"] == "tool_calls"
    assert FakeSseServer.seen["body"]["tools"][0]["function"]["name"] == "read_file"


def test_stream_maps_wire_history_to_openai(sse_server):
    FakeSseServer.chunks = [
        {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}]
    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "run_command",
                                      "arguments": {"cmd": "dir"}}}]},
        {"role": "tool", "tool_name": "run_command", "content": "listing"},
        {"role": "tool", "tool_name": "system", "content": "notice"},
    ]
    _drain(llama_client.chat_stream(sse_server, history))
    sent = FakeSseServer.seen["body"]["messages"]
    assert sent[2]["tool_calls"][0]["function"]["arguments"] == "{\"cmd\": \"dir\"}"
    assert sent[2]["tool_calls"][0]["id"] == sent[3]["tool_call_id"]
    # orphan tool note (no outstanding call id): becomes a plain user note,
    # strict jinja templates reject tool messages without a matching call
    assert sent[4] == {"role": "user", "content": "[system] notice"}


def test_stream_marks_length_and_repetition_stops(sse_server):
    FakeSseServer.chunks = [
        {"choices": [{"delta": {"content": "cut"}, "finish_reason": "length"}],
         "timings": {"predicted_n": 4096}}]
    _, m = _drain(llama_client.chat_stream(
        sse_server, [{"role": "user", "content": "x"}]))
    assert m["stopped"] == "length" and m["done_reason"] == "length"

    line = "Je vais lancer le test.\n"
    FakeSseServer.chunks = [
        {"choices": [{"delta": {"content": line * 60}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "never seen"}, "finish_reason": "stop"}]},
    ]
    _, m = _drain(llama_client.chat_stream(
        sse_server, [{"role": "user", "content": "x"}]))
    assert m["stopped"] == "repetition"


def test_stream_forwards_sampling_and_num_predict(sse_server):
    FakeSseServer.chunks = [
        {"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]}]
    _drain(llama_client.chat_stream(
        sse_server, [{"role": "user", "content": "x"}], temperature=0.7,
        options={"top_p": 0.8, "num_predict": 4096, "num_ctx": 16384}))
    body = FakeSseServer.seen["body"]
    assert body["temperature"] == 0.7 and body["top_p"] == 0.8
    assert body["max_tokens"] == 4096
    assert "num_ctx" not in body and "num_predict" not in body
```

- [ ] **Step 2: Vérifier l'échec**

Run: `py -3 -m pytest harness/tests/test_llama_client.py -q`
Expected: FAIL, `AttributeError: module 'llama_client' has no attribute 'chat_stream'`.

- [ ] **Step 3: Implémenter**

Dans `harness/llama_client.py` : factoriser le forwarding sampling du `chat()` existant et ajouter :

```python
import time
from repetition_guard import RepetitionGuard

def _sampling(body, temperature, options):
    body["temperature"] = temperature
    options = options or {}
    if options.get("num_predict"):
        body["max_tokens"] = options["num_predict"]
    for key in ("top_p", "top_k", "min_p", "presence_penalty", "repeat_penalty"):
        if key in options:
            body[key] = options[key]


def _to_openai(messages):
    """Wire factory_mcp -> OpenAI: arguments JSON-encodés, ids synthétiques,
    notes 'tool' orphelines (tool_name=system, pas d'appel en attente)
    reversées en user -- un template jinja strict rejette un message tool
    sans tool_call correspondant."""
    out, n, pending_ids = [], 0, []
    for m in messages:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            calls, pending_ids = [], []
            for c in m["tool_calls"]:
                fn = c.get("function") or {}
                n += 1
                cid = "call_{}".format(n)
                pending_ids.append(cid)
                calls.append({"id": cid, "type": "function",
                              "function": {"name": fn.get("name", ""),
                                           "arguments": json.dumps(
                                               fn.get("arguments") or {})}})
            out.append({"role": "assistant", "content": m.get("content") or "",
                        "tool_calls": calls})
        elif role == "tool":
            if pending_ids:
                out.append({"role": "tool",
                            "tool_call_id": pending_ids.pop(0),
                            "name": m.get("tool_name") or "",
                            "content": m.get("content") or ""})
            else:
                out.append({"role": "user",
                            "content": "[system] " + (m.get("content") or "")})
        else:
            out.append({"role": role, "content": m.get("content") or ""})
    return out


def chat_stream(base_url, messages, temperature=0.7, num_ctx=None, timeout=900,
                tools=None, options=None):
    """Générateur SSE sur /v1/chat/completions : même contrat que feu
    ollama_client.chat_stream (deltas thinking/content/tool_call, métriques
    via StopIteration.value). num_ctx est un flag serveur, ignoré ici."""
    body = {"messages": _to_openai(messages), "stream": True}
    _sampling(body, temperature, options)
    if tools:
        body["tools"] = tools
    req = urllib.request.Request(
        base_url.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    parts, guard, stopped, finish = [], RepetitionGuard(), None, ""
    timings, pending_calls = {}, {}
    phase = {"thinking_chars": 0, "content_chars": 0}
    t_think = t_content = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                raw = raw.strip()
                if not raw.startswith(b"data:"):
                    continue
                data = raw[5:].strip()
                if data == b"[DONE]":
                    break
                chunk = json.loads(data.decode("utf-8"))
                if chunk.get("timings"):
                    timings = chunk["timings"]
                choice = (chunk.get("choices") or [{}])[0]
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
                delta = choice.get("delta") or {}
                think = delta.get("reasoning_content") or ""
                if think:
                    now = time.time()
                    t_think = [t_think[0] if t_think else now, now]
                    phase["thinking_chars"] += len(think)
                    yield "thinking", think
                    if guard.feed(think, thinking=True):
                        stopped = "repetition"
                        break
                for frag in delta.get("tool_calls") or []:
                    slot = pending_calls.setdefault(
                        frag.get("index", 0), {"name": "", "arguments": ""})
                    fn = frag.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    slot["arguments"] += fn.get("arguments") or ""
                text = delta.get("content") or ""
                if text:
                    now = time.time()
                    t_content = [t_content[0] if t_content else now, now]
                    phase["content_chars"] += len(text)
                    parts.append(text)
                    yield "content", text
                    if guard.feed(text):
                        stopped = "repetition"
                        break
    except urllib.error.URLError as e:  # HTTPError included
        _raise_readable(e)
    if not stopped:
        for idx in sorted(pending_calls):
            slot = pending_calls[idx]
            try:
                args = json.loads(slot["arguments"]) if slot["arguments"] else {}
            except ValueError:
                args = {}
            yield "tool_call", {"name": slot["name"], "arguments": args}
    metrics = {
        "content": "".join(parts),
        "done_reason": finish,
        "eval_count": timings.get("predicted_n", 0),
        "tokens_per_s": timings.get("predicted_per_second", 0.0),
        "prefill_count": timings.get("prompt_n", 0),
        "prefill_tokens_per_s": timings.get("prompt_per_second", 0.0),
        "ttft_s": timings.get("prompt_ms", 0.0) / 1000.0,
    }
    metrics.update(phase)
    if not stopped and finish == "length":
        stopped = "length"
    if stopped:
        metrics["stopped"] = stopped
    metrics["thinking_s"] = round(t_think[1] - t_think[0], 3) if t_think else 0.0
    metrics["writing_s"] = round(t_content[1] - t_content[0], 3) if t_content else 0.0
    return metrics
```

Ajouter un `_raise_readable(e)` (copie du pattern d'`ollama_client.py:101-112` avec le libellé `"llama-server HTTP {}"` / `"llama-server unreachable"`) et refactorer `chat()` pour utiliser `_sampling` (supprimer le bloc dupliqué lignes 21-30).

- [ ] **Step 4: Vérifier**

Run: `py -3 -m pytest harness/tests/test_llama_client.py -q`
Expected: PASS (nouveaux + anciens tests non-stream).

- [ ] **Step 5: Commit**

```bash
git add harness/llama_client.py harness/tests/test_llama_client.py
git commit -m "feat: llama_client.chat_stream with tools and thinking deltas"
```

---

### Task 3: Flags serveur (jinja/reasoning/cache-reuse) + `chat_idle_s`

**Files:**
- Modify: `harness/llama_server_manager.py:72-75` (argv)
- Modify: `harness/factory_config.py:112-130` (`load_llama_server`)
- Test: fichier de tests existant qui vérifie l'argv du manager (le localiser : `grep -rn "ngl" harness/tests/`) + `harness/tests/test_factory_config.py` s'il existe, sinon le fichier où `load_llama_server` est testé (`grep -rn "load_llama_server" harness/tests/`)

**Interfaces:**
- Produces: argv serveur enrichi de `--jinja --reasoning-format deepseek --cache-reuse 256` ; `load_llama_server()[...]["chat_idle_s"]` (int, défaut 600).

- [ ] **Step 1: Tests d'abord**

Étendre le test d'argv existant : l'argv de `ensure()` contient, dans cet ordre, `"--jinja"`, `"--reasoning-format", "deepseek"`, `"--cache-reuse", "256"`. Étendre le test de `load_llama_server` : sans clé, `chat_idle_s == 600` ; avec `chat_idle_s = 120` dans le TOML, `120`.

- [ ] **Step 2: Vérifier l'échec** (FAIL sur les deux assertions)

- [ ] **Step 3: Implémenter**

`llama_server_manager.py` — dans `ensure()` :

```python
        argv = [self.cfg["exe"], "-m", entry["path"],
                "-ngl", "99", "-fa", "1", "-ctk", "q8_0", "-ctv", "q8_0",
                "-c", str(self.cfg["ctx"]), "--parallel", "1",
                # --jinja: templates natifs = tool calls OpenAI; deepseek:
                # le thinking sort dans reasoning_content, pas dans content;
                # cache-reuse: le KV du prefixe survit aux retouches (TTFT)
                "--jinja", "--reasoning-format", "deepseek",
                "--cache-reuse", "256",
                "--port", str(self.cfg["port"])] + entry["args"]
```

`factory_config.py` — dernière ligne de `load_llama_server` :

```python
    return {"exe": raw["exe"], "port": int(raw["port"]), "ctx": int(raw["ctx"]),
            "chat_idle_s": int(raw.get("chat_idle_s", 600)), "models": models}
```

- [ ] **Step 4: Vérifier** — `py -3 -m pytest harness/tests -q` : PASS.

- [ ] **Step 5: Commit**

```bash
git add harness/llama_server_manager.py harness/factory_config.py harness/tests
git commit -m "feat: jinja/reasoning/cache-reuse server flags and chat_idle_s config"
```

---

### Task 4: Cycle de vie runtime dans `factory_mcp` (lock = VRAM)

Le coeur du chantier. `Factory` possède le manager ; le GPU lock est pris au chargement et gardé tant que le serveur est chargé ; un timer d'inactivité relâche tout.

**Files:**
- Modify: `harness/factory_mcp.py` — imports, `__init__` (l.314-326), nouvelle section runtime (remplace la section ollama l.485-578), `chat_reply` (l.647-731), `_compact` (l.733-754), `_agent_turn` (l.858-861), `agent_reply` (l.929-950), `chat_approve` (l.952-973)
- Test: `harness/tests/test_factory_mcp.py` (adapter les fakes) + nouveaux tests lifecycle

**Interfaces:**
- Consumes: `llama_client.chat_stream` (Task 2), `LlamaServerManager` (Task 3), `load_llama_server(config_path)` avec `chat_idle_s`.
- Produces (pour Task 5) :
  - `Factory.runtime_status() -> {"loaded": str|None, "held_by": "chat"|"job"|None}`
  - `Factory.runtime_models() -> {"models": [{"name", "path", "size", "args", "loaded", "missing"}]}`
  - `Factory.runtime_load(model) -> {"model", "status": "loaded"}`
  - `Factory.runtime_unload() -> {"status": "unloaded"}`
  - Attributs injectables pour tests : `factory._llama` (manager), `factory.timer_fn` (défaut `threading.Timer`).
  - Les méthodes `ollama_*` n'existent plus.

- [ ] **Step 1: Écrire les tests lifecycle**

Dans `test_factory_mcp.py`, un stub manager + timer et les tests clés :

```python
class StubManager:
    def __init__(self):
        self.current = None
        self.url = "http://stub"
        self.shutdowns = 0

    def ensure(self, model):
        self.current = (model, 1234)
        return self.url

    def shutdown(self):
        self.shutdowns += 1
        self.current = None


class StubTimer:
    fired = []  # instances, pour déclencher à la main

    def __init__(self, delay, fn):
        self.delay, self.fn, self.cancelled = delay, fn, False
        StubTimer.fired.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True


def _runtime_factory(tmp_path, monkeypatch):
    factory = ...  # même construction que les tests chat existants du fichier
    factory._llama = StubManager()
    factory._llama_cfg = {"chat_idle_s": 600, "models": {"m": {}}, "port": 8091}
    factory.timer_fn = StubTimer
    return factory


def test_chat_keeps_the_gpu_lock_until_idle(tmp_path, monkeypatch):
    factory = _runtime_factory(tmp_path, monkeypatch)
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream",
                        fake_stream_returning("hi"))  # helper existant adapté
    sid = factory.chat_create("m")["session_id"]
    list(factory.chat_reply(sid, "hello"))
    # generation done: server still loaded, lock still held, idle timer armed
    assert factory._llama.current is not None
    outsider = GpuLock(factory.jobs_root / ".gpu.lock")
    assert not outsider.acquire(timeout=0)
    timer = StubTimer.fired[-1]
    assert timer.delay == 600 and not timer.cancelled
    timer.fn()  # idle fires
    assert factory._llama.shutdowns == 1
    assert outsider.acquire(timeout=0)
    outsider.release()


def test_chat_refuses_while_a_job_holds_the_gpu(tmp_path, monkeypatch):
    factory = _runtime_factory(tmp_path, monkeypatch)
    job = GpuLock(factory.jobs_root / ".gpu.lock")
    assert job.acquire(timeout=0)
    sid = factory.chat_create("m")["session_id"]
    with pytest.raises(ToolError):
        next(factory.chat_reply(sid, "hello"))
    job.release()


def test_runtime_endpoints(tmp_path, monkeypatch):
    factory = _runtime_factory(tmp_path, monkeypatch)
    factory.runtime_load("m")
    assert factory.runtime_status()["loaded"] == "m"
    assert factory.runtime_status()["held_by"] == "chat"
    out = factory.runtime_unload()
    assert out["status"] == "unloaded"
    assert factory.runtime_status()["loaded"] is None
```

Note d'adaptation : les tests chat/agent existants monkeypatchent `ollama_client.chat_stream` (44 occurrences « ollama » dans le fichier) — les rebrancher sur `factory_mcp.llama_client.chat_stream` avec la nouvelle signature `(base_url, messages, ...)` (le premier argument positionnel devient l'url, plus le nom de modèle) et injecter `StubManager` partout où un chat tourne. Supprimer les tests des méthodes `ollama_*` de `test_factory_mcp.py` et `test_ollama_ops.py` (couverts par `runtime_*`).

- [ ] **Step 2: Vérifier l'échec** — `py -3 -m pytest harness/tests/test_factory_mcp.py -q` : FAIL (`_llama`, `runtime_load` inexistants).

- [ ] **Step 3: Implémenter**

Imports de `factory_mcp.py` : remplacer `import ollama_client` par `import llama_client`, ajouter `import threading` et `from factory_config import load_llama_server` (compléter l'import groupé existant), `from llama_server_manager import LlamaServerManager`.

`__init__` : supprimer `self.ollama_spawn_fn` ; ajouter :

```python
        self._llama = None        # LlamaServerManager, lazy; tests inject
        self._llama_cfg = None
        self._runtime_lock = GpuLock(self.jobs_root / ".gpu.lock")
        self._runtime_mutex = threading.Lock()
        self._idle_timer = None
        self.timer_fn = threading.Timer  # tests inject
```

Remplacer toute la section `# ---- ollama ----` (`_spawn_ollama`, `ollama_status`, `ollama_models`, `ollama_load`, `ollama_unload`, `ollama_pull`, `ollama_start`, l.512-578) par :

```python
    # ---- runtime (llama-server) ----

    def _runtime(self):
        if self._llama is None:
            cfg = load_llama_server(self.config_path)
            if cfg is None:
                raise ToolError("factory.toml has no [llama_server] section; "
                                "the chat needs one since the Ollama retirement")
            self._llama_cfg = cfg
            self._llama = LlamaServerManager(cfg)
        return self._llama

    def _acquire_runtime(self, model):
        """Lock = VRAM: take the GPU lock with the first load and keep it
        while the server is loaded. Re-entrant across chat turns; the idle
        timer (armed after each generation) is the only releaser."""
        with self._runtime_mutex:
            self._cancel_idle()
            if not self._runtime_lock.acquired and \
                    not self._runtime_lock.acquire(timeout=0):
                raise ToolError("GPU is busy: a job holds the lock")
            try:
                return self._runtime().ensure(model)
            except Exception:
                self._release_runtime_locked()
                raise

    def _release_runtime(self):
        with self._runtime_mutex:
            self._release_runtime_locked()

    def _release_runtime_locked(self):
        self._cancel_idle()
        if self._llama is not None:
            self._llama.shutdown()
        self._runtime_lock.release()

    def _cancel_idle(self):
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _arm_idle(self):
        with self._runtime_mutex:
            if not self._runtime_lock.acquired:
                return  # nothing loaded, nothing to give back
            self._cancel_idle()
            idle = (self._llama_cfg or {}).get("chat_idle_s", 600)
            timer = self.timer_fn(idle, self._release_runtime)
            timer.daemon = True
            timer.start()
            self._idle_timer = timer

    def runtime_status(self):
        loaded = self._llama.current[0] if (self._llama and
                                            self._llama.current) else None
        if loaded is not None:
            return {"loaded": loaded, "held_by": "chat"}
        probe = GpuLock(self.jobs_root / ".gpu.lock")
        if probe.acquire(timeout=0):
            probe.release()
            return {"loaded": None, "held_by": None}
        return {"loaded": None, "held_by": "job"}

    def runtime_models(self):
        cfg = self._llama_cfg or load_llama_server(self.config_path)
        if cfg is None:
            return {"models": []}
        loaded = self._llama.current[0] if (self._llama and
                                            self._llama.current) else None
        models = []
        for name, entry in sorted(cfg["models"].items()):
            path = Path(entry["path"])
            exists = path.is_file()
            models.append({"name": name, "path": entry["path"],
                           "size": path.stat().st_size if exists else None,
                           "args": entry["args"], "loaded": name == loaded,
                           "missing": not exists})
        return {"models": models}

    def runtime_load(self, model):
        self._check_runtime_model(model)
        self._acquire_runtime(model)
        self._arm_idle()
        return {"model": model, "status": "loaded"}

    def runtime_unload(self):
        self._release_runtime()
        return {"status": "unloaded"}

    def _check_runtime_model(self, model):
        self._check_model(model)
        cfg = self._llama_cfg or load_llama_server(self.config_path)
        if cfg is None or model not in cfg["models"]:
            raise ToolError("{!r} has no [llama_server.models] entry in "
                            "factory.toml".format(model))
```

`chat_reply` (l.660) : remplacer `lock = self._gpu_guard()` par `url = self._acquire_runtime(session["model"])` ; l'appel stream (l.697) devient :

```python
            stream = llama_client.chat_stream(
                url, history, num_ctx=num_ctx,
                options=self._model_options(session["model"]))
```

et le `finally` (l.727-731) devient :

```python
        finally:
            # Client gone mid-stream (GeneratorExit lands here): keep the partial.
            if not finished and (parts or thoughts):
                _partial("interrupted")
            self._arm_idle()
```

`_compact` (l.744) :

```python
        res = llama_client.chat(self._runtime().url, chat_context.SUMMARY_PROMPT,
                                "\n\n".join(parts), temperature=0.1,
                                num_ctx=CHAT_NUM_CTX,
                                options=self._model_options(model))
```

(le paramètre `model` de `_compact` sert encore aux options et au champ `model` du résumé ; le serveur est déjà chargé par l'appelant).

`_agent_turn` (l.858) : en tête de chaque itération de la boucle (juste après `session = self.chats.get(session_id)` l.828) ajouter `url = self._acquire_runtime(session["model"])` puis :

```python
            stream = llama_client.chat_stream(
                url, history, num_ctx=num_ctx,
                tools=tools,
                options=self._model_options(session["model"]))
```

`agent_reply` (l.943-950) et `chat_approve` (l.958-973) : remplacer `lock = self._gpu_guard()` par `self._acquire_runtime(session["model"])` et `lock.release()` par `self._arm_idle()`.

`RUNNERS` (l.140) : retirer l'entrée `"pull": "ollama_pull.py"`.

`_gpu_guard` (l.491-496) reste : les vérifications `runtime_load`-hors-chat n'en ont plus besoin mais d'autres appelants jobs peuvent l'utiliser — le supprimer seulement s'il n'a plus aucun appelant (`grep -n "_gpu_guard" harness/factory_mcp.py`).

- [ ] **Step 4: Vérifier** — `py -3 -m pytest harness/tests/test_factory_mcp.py -q` : PASS.

- [ ] **Step 5: Commit**

```bash
git add harness/factory_mcp.py harness/tests/test_factory_mcp.py harness/tests/test_ollama_ops.py
git commit -m "feat: chat/agent ride llama-server with lock-owned lifecycle"
```

---

### Task 5: Routes web `/api/runtime`

**Files:**
- Modify: `harness/factory_web.py` — handlers l.153-169, ROUTES l.276-281, boot l.315-319
- Test: `harness/tests/test_factory_web.py` (remplacer les tests d'endpoints ollama)

**Interfaces:**
- Consumes: `Factory.runtime_status/models/load/unload` (Task 4).
- Produces: `GET /api/runtime`, `GET /api/runtime/models`, `POST /api/runtime/load {model}`, `POST /api/runtime/unload {}` (token requis sur POST, comme partout).

- [ ] **Step 1: Tests** — dans `test_factory_web.py`, remplacer les tests `/api/ollama*` par : GET `/api/runtime` renvoie le dict de `runtime_status` ; GET `/api/runtime/models` celui de `runtime_models` ; POST load/unload appellent les méthodes Factory (suivre le pattern de stub Factory déjà utilisé dans ce fichier) ; POST sans token -> 403.

- [ ] **Step 2: Vérifier l'échec** (404 sur `/api/runtime`).

- [ ] **Step 3: Implémenter** — remplacer les 6 handlers `_ollama_*` par :

```python
    def _runtime_status(self, query=None):
        self._json(200, self.server.factory.runtime_status())

    def _runtime_models(self, query=None):
        self._json(200, self.server.factory.runtime_models())

    def _runtime_load(self, query=None):
        self._json(200, self.server.factory.runtime_load(self._body()["model"]))

    def _runtime_unload(self, query=None):
        self._json(200, self.server.factory.runtime_unload())
```

ROUTES :

```python
    (r"/api/runtime", "GET", Handler._runtime_status),
    (r"/api/runtime/models", "GET", Handler._runtime_models),
    (r"/api/runtime/load", "POST", Handler._runtime_load),
    (r"/api/runtime/unload", "POST", Handler._runtime_unload),
```

Boot (`main`, l.315-319) : supprimer le bloc `ollama_start` et son commentaire ; le serveur modèle démarre paresseusement au premier message.

- [ ] **Step 4: Vérifier** — `py -3 -m pytest harness/tests/test_factory_web.py -q` : PASS.

- [ ] **Step 5: Commit**

```bash
git add harness/factory_web.py harness/tests/test_factory_web.py
git commit -m "feat: /api/runtime endpoints replace the ollama panel API"
```

---

### Task 6: UI — onglet Modèles + sélecteurs chat

Pas de tests JS dans ce repo : validation visuelle au smoke final (Task 10).

**Files:**
- Modify: `harness/web/index.html:17` (nav)
- Modify: `harness/web/app.js` — cache l.23-33, `renderOllama` l.270-333, vue chat l.679-681 + l.739 + l.854, routeur l.949

**Interfaces:**
- Consumes: `/api/runtime`, `/api/runtime/models` (Task 5).

- [ ] **Step 1: nav** — `index.html:17` : `<a href="#runtime" data-view="runtime">Modèles</a>`.

- [ ] **Step 2: runtime status helper** — remplacer `OLLAMA_CACHE`/`ollamaStatus` (l.23-33) par :

```js
// /api/runtime est local et instantane (pas d'appel au serveur modele):
// le cache ne sert qu'a eviter un fetch par render.
let RUNTIME_CACHE = { at: 0, data: null };
async function runtimeModels(maxAge = 30000) {
  if (RUNTIME_CACHE.data && Date.now() - RUNTIME_CACHE.at < maxAge)
    return RUNTIME_CACHE.data;
  const data = await api("/api/runtime/models");
  RUNTIME_CACHE = { at: Date.now(), data };
  return data;
}
```

- [ ] **Step 3: renderRuntime** — remplacer `renderOllama` (l.270-333) par :

```js
// ---- Runtime (llama-server) ----

const fmtGB = (b) => b == null ? "—" : (b / 1e9).toFixed(1) + " GB";

async function renderRuntime() {
  const [s, { models }] = await Promise.all(
    [api("/api/runtime"), runtimeModels(0)]);
  const act = (path, body, label) => h("button", { text: label, onclick: async (ev) => {
    ev.target.disabled = true;
    try { await api(path, { method: "POST", body: JSON.stringify(body || {}) }); tick(); }
    catch (e) { showError(e); ev.target.disabled = false; }
  } });
  const state = s.loaded ? s.loaded + " chargé"
    : (s.held_by === "job" ? "GPU occupé par un job" : "aucun modèle chargé");
  const parts = [h("h2", { text: "Modèles" }),
    h("div", { class: "banner" },
      badge(s.loaded ? "succeeded" : "dead"),
      h("span", { text: "llama-server · " + state }),
      s.loaded ? act("/api/runtime/unload", {}, "Décharger") : "")];
  parts.push(h("h3", { text: "Catalogue (factory.toml)" }),
    h("table", {}, h("thead", {}, h("tr", {},
      ...["modèle", "GGUF", "flags", ""].map((t) => h("th", { text: t })))),
      h("tbody", {}, ...models.map((m) => h("tr", {},
        h("td", { text: m.name }),
        h("td", { text: m.missing ? "GGUF manquant" : fmtGB(m.size) }),
        h("td", { text: m.args.join(" ") || "—" }),
        h("td", {}, m.loaded ? badge("succeeded")
          : (m.missing ? "" : act("/api/runtime/load", { model: m.name }, "Charger"))))))),
    h("p", { class: "empty", text: models.length ? ""
      : "Aucune entrée [llama_server.models] dans factory.toml." }));
  MAIN.replaceChildren(...parts);
  return false;
}
```

- [ ] **Step 4: vue chat** — l.679-681 :

```js
  const [{ sessions }, { models }, { toolsets }] = await Promise.all(
    [api("/api/chats"), runtimeModels(), api("/api/toolsets")]);
  const installed = models.map((m) => m.name);
```

l.739 (message vide de la liste des modèles) : remplacer la condition `ollama.up ? ... : ...` par un texte basé sur `installed.length`, ex. `"Aucun modèle : ajoute une entrée [llama_server.models] dans factory.toml."`. l.854 : remplacer `const isLoaded = (ollama.loaded || []).some(...)` par un fetch du statut dans le même `Promise.all` du render concerné et `const isLoaded = runtime.loaded === session.model;` (adapter la variable au scope local réel du fichier). Routeur l.949 : `else if (view === "runtime") keepPolling = await renderRuntime();`. Balayer le fichier : `grep -n "ollama" harness/web/app.js` doit rendre zéro ligne active.

- [ ] **Step 5: Commit**

```bash
git add harness/web/index.html harness/web/app.js
git commit -m "feat: models tab reads the llama-server catalogue"
```

---

### Task 7: `draft_job` et `loop_job` sans fallback Ollama

**Files:**
- Modify: `harness/draft_job.py:25,161-167`
- Modify: `harness/loop_job.py:27,496-505`
- Test: fichiers de tests correspondants (`grep -rln "run_draft\|run_job" harness/tests/`)

**Interfaces:**
- Consumes: `LlamaServerManager`, `load_llama_server`.
- Produces: `run_draft`/`run_job` exigent `[llama_server]` quand aucun `chat_fn` n'est injecté (ConfigError sinon) ; `chat_fn` injectable inchangé pour les tests.

- [ ] **Step 1: Tests** — un test par runner : sans section `[llama_server]` dans le TOML et sans `chat_fn` injecté, le run se termine en `status == "error"` avec `"llama_server"` dans le message (les deux runners écrivent toujours un verdict, jamais d'exception nue).

- [ ] **Step 2: Vérifier l'échec** (aujourd'hui : fallback silencieux sur `ollama_chat`).

- [ ] **Step 3: Implémenter** — dans `loop_job.py`, remplacer l.496-505 par :

```python
    manager = None
    if chat_fn is None:
        # Zero-Ollama (2026-07-18): the ladder requires the llama-server
        # runtime; injected chat_fns (tests, rigs) bypass it entirely.
        ls_cfg = load_llama_server(config_path)
        if ls_cfg is None:
            raise ConfigError("factory.toml has no [llama_server] section; "
                              "the Ollama runtime was retired")
        manager = LlamaServerManager(ls_cfg)
        chat_fn = manager.chat
        unload_fn = unload_fn or manager.unload
```

et supprimer `from ollama_client import chat as ollama_chat` (l.27) ainsi que la ligne `chat_fn = chat_fn or ollama_chat`. Vérifier que `ConfigError` est bien attrapé par le try/except qui écrit le verdict (sinon l'ajouter au tuple attrapé). Miroir exact dans `draft_job.py` (imports l.25, corps l.161-167, `manager.shutdown()` dans le `finally` de `run_draft` comme `run_job` le fait déjà l.527).

- [ ] **Step 4: Vérifier** — `py -3 -m pytest harness/tests -q` : PASS.

- [ ] **Step 5: Commit**

```bash
git add harness/loop_job.py harness/draft_job.py harness/tests
git commit -m "feat: draft and ladder require the llama-server runtime"
```

---

### Task 8: Suppression d'Ollama

**Files:**
- Delete: `harness/ollama_client.py`, `harness/ollama_pull.py`, `harness/tests/test_ollama_client.py`, `harness/tests/test_ollama_ops.py`, `harness/tests/test_pull_job.py`
- Modify: tout fichier encore importeur (`grep -rln "ollama" harness/ --include="*.py"`) — attendu : `loop.py` (1 occurrence), `mcp_registry.py` (1), `factory_config.py` (2), `chat_context.py` (1), `conftest.py` (1 commentaire) — traiter chaque cas : commentaire à reformuler ou code mort à retirer.

- [ ] **Step 1: Balayer** — `grep -rn "ollama" harness/ --include="*.py"` ; pour chaque hit restant hors fichiers à supprimer, retirer l'import/le code mort ou reformuler le commentaire (ex. conftest.py:4 cite `ollama_client.py` en exemple — remplacer par `llama_client.py`).

- [ ] **Step 2: Supprimer les fichiers** — `git rm harness/ollama_client.py harness/ollama_pull.py harness/tests/test_ollama_client.py harness/tests/test_ollama_ops.py harness/tests/test_pull_job.py`. Si `test_ollama_client.py` contient encore des tests `chat_stream` de contrat général (deltas, phases, length-stop) non couverts par Task 2, les porter d'abord dans `test_llama_client.py`.

- [ ] **Step 3: Vérifier** — `py -3 -m pytest harness/tests -q` : PASS, et `grep -rn "ollama" harness/ --include="*.py" --include="*.js" --include="*.html"` ne rend plus rien.

- [ ] **Step 4: Commit**

```bash
git commit -am "chore: retire the ollama runtime from the studio"
```

---

### Task 9: Byte-stable — verrouiller le déterminisme du prompt

**Files:**
- Test: `harness/tests/test_factory_mcp.py` (ajouts)

**Interfaces:**
- Consumes: `factory_mcp._agent_system`, wire construit par `chat_reply`/`_agent_turn`.

- [ ] **Step 1: Tests**

```python
def test_agent_system_is_byte_stable():
    a = factory_mcp._agent_system("C:/ws", ["read_file", "run_command"])
    assert a == factory_mcp._agent_system("C:/ws", ["read_file", "run_command"])


def test_wire_messages_carry_no_timestamps(tmp_path, monkeypatch):
    # le prefixe KV n'est reutilisable que si l'historique renvoye est
    # byte-identique d'un tour a l'autre : ts/metrics ne doivent jamais
    # entrer dans les messages envoyes au serveur
    factory = _runtime_factory(tmp_path, monkeypatch)
    seen = {}

    def spy(url, messages, **kw):
        seen["messages"] = messages
        return fake_stream_returning("ok")(url, messages, **kw)

    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", spy)
    sid = factory.chat_create("m")["session_id"]
    list(factory.chat_reply(sid, "hello"))
    for m in seen["messages"]:
        assert set(m) <= {"role", "content", "tool_calls", "tool_name"}
```

- [ ] **Step 2: Vérifier** — attendu : PASS direct (le wire strip déjà à role/content, `_agent_system` est un template statique). Si un test échoue, corriger la source de non-déterminisme (c'est le but du verrou), pas le test.

- [ ] **Step 3: Commit**

```bash
git add harness/tests/test_factory_mcp.py
git commit -m "test: lock byte-stable prompt invariants for KV prefix reuse"
```

---

### Task 10: Validation réelle + mesures + docs

Étapes opérateur (GPU) : à lancer détachées, règle projet.

- [ ] **Step 1: Suite complète** — `py -3 -m pytest harness/tests -q` : 0 failure.

- [ ] **Step 2: Régression jobs** — le harnais de régression valide que `--jinja`/`--reasoning-format`/`--cache-reuse` ne changent pas les verdicts jobs : lancer la suite (`regress` CLI) **détachée** (`Start-Process` + log fichier). Attendu : verdicts identiques à la baseline `211c5eb`. Tout écart -> autopsie avant de continuer.

- [ ] **Step 3: Smoke studio** — lancer `py -3 harness/factory_web.py`, puis : (a) session chat 30b, 2 tours, vérifier stream + thinking + métriques tok/s affichées ; (b) session agent avec un tool run (read_file), vérifier tool_call/résultat ; (c) onglet Modèles : catalogue, charger/décharger ; (d) laisser passer l'idle-timeout (mettre `chat_idle_s = 60` temporairement) et vérifier que la VRAM se libère (nvidia-smi) et qu'un job passe.

- [ ] **Step 4: Mesures TTFT/tok-s** — sur une session longue (>10 messages) : noter `ttft_s` au tour N>1 avant migration (chiffres Ollama de référence : ~60 tok/s visibles) et après ; puis A/B `--cache-reuse` retiré vs présent (une variable à la fois). Consigner au registre.

- [ ] **Step 5: Docs** — mettre à jour : `docs/backlog-optimisations.md` (migration chat/studio -> SHIPPED avec preuves ; byte-stable -> statut mesuré), `ROADMAP.md` (cocher « Chat/studio -> llama-server » et « Prompt-cache stability » avec les chiffres), `CHANGELOG.md`, `docs/architecture.md` si elle décrit le runtime Ollama.

- [ ] **Step 6: Commit final**

```bash
git add docs/backlog-optimisations.md ROADMAP.md CHANGELOG.md docs/architecture.md
git commit -m "docs: record chat llama-server migration results"
git push
```

---

## Points de vigilance (issus de la spec, §7)

À falsifier pendant Task 2/10, jamais à supposer : streaming `tool_calls` réel de notre build llama.cpp (sinon : mettre à jour le parsing sur le format observé), `reasoning_content` en delta (dépend de `--reasoning-format`), `timings` présent dans le dernier chunk SSE (sinon : fallback métriques à zéro + autopsie), messages `tool` orphelins acceptés par le template jinja (le mapping `[system] note -> user` de Task 2 est la parade), effet mesurable de `--cache-reuse`.
