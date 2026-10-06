# OpenRouter Cloud Column Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `experiments/coder_bench/cloud_client.py` an OpenRouter backend and per-cell cost logging, so banc A can run a real cloud column against the frozen corpus.

**Architecture:** The `chat_fn` seam in `run_bench.py:152-155` does not move. `cloud_client` gains a second backend selected by the model id (`/` present → OpenRouter, bare `claude-…` → the existing Anthropic SDK path), plus a process-global ledger that `run_bench` diffs around each cell. `loop_job.py` is not touched, so the production jobs lane is unaffected and no `regress run` is mandated by the change itself.

**Tech Stack:** Python 3.9, stdlib `urllib` only (matching `harness/llama_client.py` — no new dependency), pytest.

**Spec:** `docs/design/specs/2026-07-27-openrouter-cloud-column-design.md`

## Global Constraints

- **Python 3.9.** The bench runs under `py -3.9` (`run_bench.py:12`). No `match`, no `dict |` merge, no `list[str]` builtin generics.
- **Stdlib only.** Use `urllib.request`, as `llama_client.py` does. Do not add the `openai` or `requests` package. The `anthropic` import stays lazy (`cloud_client.py:31`) so the local rungs never need the SDK.
- **String formatting:** `.format()`, not f-strings — matches the surrounding files.
- **Never touch `harness/loop_job.py`.** Widening its attempt whitelist is explicitly rejected by the spec.
- **Commits:** conventional, imperative, English, ASCII only, no AI attribution (project `CLAUDE.md`). Never commit on `main` — the `.githooks` guardrail branches agent work onto `agent/<date>`.
- **Secret:** the key is read from the `OPENROUTER_API_KEY` environment variable. It must never appear in a source file, a test, a fixture, a log line, or a commit.
- **Provider pin:** `{"order": ["anthropic"], "allow_fallbacks": false}`. The slug `"anthropic"` is unverified — Task 5 settles it.

## File Structure

| File | Responsibility |
|---|---|
| `experiments/coder_bench/cloud_client.py` (modify) | Both cloud backends, the normalized return contract, the cost ledger. |
| `experiments/coder_bench/run_bench.py` (modify) | Preflight, per-cell ledger diff onto the row, cost in the summary. |
| `harness/tests/test_cloud_client.py` (create) | Ledger arithmetic, request shape, response normalization, dispatch, missing-key. |

Tests live in `harness/tests/` rather than beside the bench so the regression gate (`pytest harness/tests -q`) covers them; the file adds `experiments/coder_bench` to `sys.path` itself, since `conftest.py:9` only inserts `harness`.

---

### Task 1: The cost ledger

Pure arithmetic, no network. Built first so later tasks have something to record into.

**Files:**
- Modify: `experiments/coder_bench/cloud_client.py`
- Test: `harness/tests/test_cloud_client.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `ledger_snapshot() -> dict` with keys `calls` (int), `cost_usd` (float), `prompt_tokens` (int), `completion_tokens` (int), `providers` (tuple of str).
  - `ledger_delta(before: dict, after: dict = None) -> dict` with keys `calls`, `cost_usd`, `prompt_tokens`, `completion_tokens` (numbers) and `providers` (sorted list of str). `after` defaults to a fresh snapshot.
  - `reset_ledger() -> None`.
  - `_record(payload: dict) -> None` — internal; accumulates one OpenRouter response body.

- [ ] **Step 1: Write the failing test**

Create `harness/tests/test_cloud_client.py`:

```python
"""The bench's cloud rung. Transport is tested without network: what matters
is the request shape (auth, provider pin) and that the ledger prices a cell."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "experiments" / "coder_bench"))

import cloud_client  # noqa: E402


@pytest.fixture(autouse=True)
def clean_ledger():
    cloud_client.reset_ledger()
    yield
    cloud_client.reset_ledger()


def _usage(cost, prompt, completion, provider="Anthropic"):
    return {"usage": {"cost": cost, "prompt_tokens": prompt,
                      "completion_tokens": completion},
            "provider": provider}


def test_empty_ledger_is_zeroed():
    snap = cloud_client.ledger_snapshot()
    assert snap["calls"] == 0
    assert snap["cost_usd"] == 0.0
    assert snap["providers"] == ()


def test_record_accumulates():
    cloud_client._record(_usage(0.01, 100, 20))
    cloud_client._record(_usage(0.02, 200, 30))
    snap = cloud_client.ledger_snapshot()
    assert snap["calls"] == 2
    assert snap["cost_usd"] == pytest.approx(0.03)
    assert snap["prompt_tokens"] == 300
    assert snap["completion_tokens"] == 50


def test_delta_prices_one_cell_only():
    cloud_client._record(_usage(0.01, 100, 20))
    before = cloud_client.ledger_snapshot()
    cloud_client._record(_usage(0.05, 400, 60))
    delta = cloud_client.ledger_delta(before)
    assert delta["calls"] == 1
    assert delta["cost_usd"] == pytest.approx(0.05)
    assert delta["prompt_tokens"] == 400
    assert delta["completion_tokens"] == 60


def test_delta_reports_only_providers_seen_since_before():
    cloud_client._record(_usage(0.01, 10, 1, provider="Anthropic"))
    before = cloud_client.ledger_snapshot()
    cloud_client._record(_usage(0.01, 10, 1, provider="Bedrock"))
    assert cloud_client.ledger_delta(before)["providers"] == ["Bedrock"]


def test_missing_cost_and_provider_do_not_crash():
    cloud_client._record({"usage": {"prompt_tokens": 5}})
    snap = cloud_client.ledger_snapshot()
    assert snap["cost_usd"] == 0.0
    assert snap["completion_tokens"] == 0
    assert snap["providers"] == ("unknown",)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `py -3.9 -m pytest harness/tests/test_cloud_client.py -v`
Expected: FAIL — `AttributeError: module 'cloud_client' has no attribute 'reset_ledger'`

- [ ] **Step 3: Write the implementation**

In `cloud_client.py`, after the `_client = None` line (`:21`), add:

```python
# Process-global, because `loop_job` builds each attempt record from an
# explicit whitelist (loop_job.py:298-303) and drops anything else the chat
# returns. Widening that whitelist would edit the path every production job
# takes for a bench-only need; a ledger the runner diffs around a cell does
# not. `providers` is a log, not a set: with fallbacks off it should hold one
# name, and a second one is the evidence the pin failed.
_ledger = {"calls": 0, "cost_usd": 0.0, "prompt_tokens": 0,
           "completion_tokens": 0, "providers": []}


def reset_ledger():
    _ledger.update(calls=0, cost_usd=0.0, prompt_tokens=0,
                   completion_tokens=0)
    del _ledger["providers"][:]


def ledger_snapshot():
    return {"calls": _ledger["calls"],
            "cost_usd": _ledger["cost_usd"],
            "prompt_tokens": _ledger["prompt_tokens"],
            "completion_tokens": _ledger["completion_tokens"],
            "providers": tuple(_ledger["providers"])}


def ledger_delta(before, after=None):
    """What one cell cost: the difference between two snapshots."""
    if after is None:
        after = ledger_snapshot()
    seen = after["providers"][len(before["providers"]):]
    return {"calls": after["calls"] - before["calls"],
            "cost_usd": round(after["cost_usd"] - before["cost_usd"], 6),
            "prompt_tokens": after["prompt_tokens"] - before["prompt_tokens"],
            "completion_tokens": (after["completion_tokens"]
                                  - before["completion_tokens"]),
            "providers": sorted(set(seen))}


def _record(payload):
    usage = payload.get("usage") or {}
    _ledger["calls"] += 1
    _ledger["cost_usd"] += float(usage.get("cost") or 0.0)
    _ledger["prompt_tokens"] += usage.get("prompt_tokens") or 0
    _ledger["completion_tokens"] += usage.get("completion_tokens") or 0
    _ledger["providers"].append(payload.get("provider") or "unknown")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -3.9 -m pytest harness/tests/test_cloud_client.py -v`
Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add harness/tests/test_cloud_client.py experiments/coder_bench/cloud_client.py
git commit -m "feat: cost ledger for the bench cloud rung"
```

---

### Task 2: OpenRouter transport

**Files:**
- Modify: `experiments/coder_bench/cloud_client.py`
- Test: `harness/tests/test_cloud_client.py`

**Interfaces:**
- Consumes: `_record(payload)` from Task 1.
- Produces:
  - `OPENROUTER_URL: str`, `OPENROUTER_PROVIDER: str`, `TIMEOUT: float`
  - `_bare(model: str) -> str` — strips the provider prefix (`anthropic/claude-haiku-4-5` → `claude-haiku-4-5`).
  - `_openrouter_chat(model, system, user, temperature, options) -> dict` — the normalized return contract: `content`, `done_reason`, `eval_count`, `tokens_per_s`, `prefill_tokens_per_s`, `ttft_s`, `input_tokens`, `output_tokens`.

- [ ] **Step 1: Write the failing test**

Append to `harness/tests/test_cloud_client.py`:

```python
import json
import urllib.error


class _FakeResponse:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _reply(content="ok", cost=0.01, prompt=100, completion=20):
    return {"choices": [{"message": {"content": content},
                         "finish_reason": "stop"}],
            "usage": {"cost": cost, "prompt_tokens": prompt,
                      "completion_tokens": completion},
            "provider": "Anthropic"}


@pytest.fixture
def sent(monkeypatch):
    """Captures the request cloud_client would have put on the wire."""
    box = {"payload": _reply()}

    def fake_urlopen(req, timeout=None):
        box["url"] = req.full_url
        box["auth"] = req.get_header("Authorization")
        box["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse(box["payload"])

    monkeypatch.setattr(cloud_client.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    return box


def test_request_carries_auth_and_pins_the_provider(sent):
    cloud_client._openrouter_chat("anthropic/claude-haiku-4-5", "sys", "usr",
                                  0.1, {})
    assert sent["url"] == cloud_client.OPENROUTER_URL
    assert sent["auth"] == "Bearer test-key"
    assert sent["body"]["model"] == "anthropic/claude-haiku-4-5"
    assert sent["body"]["provider"] == {
        "order": [cloud_client.OPENROUTER_PROVIDER], "allow_fallbacks": False}
    assert sent["body"]["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "usr"}]


def test_num_predict_becomes_max_tokens_and_is_capped(sent):
    cloud_client._openrouter_chat("anthropic/claude-haiku-4-5", "s", "u", 0.1,
                                  {"num_predict": 10 ** 9})
    assert sent["body"]["max_tokens"] == cloud_client.MAX_OUTPUT["claude-haiku-4-5"]


def test_temperature_sent_only_where_the_model_accepts_it(sent):
    cloud_client._openrouter_chat("anthropic/claude-haiku-4-5", "s", "u", 0.7, {})
    assert sent["body"]["temperature"] == 0.7
    cloud_client._openrouter_chat("anthropic/claude-sonnet-5", "s", "u", 0.7, {})
    assert "temperature" not in sent["body"]


def test_response_is_normalized_to_the_chat_contract(sent):
    out = cloud_client._openrouter_chat("anthropic/claude-haiku-4-5", "s", "u",
                                        0.1, {})
    assert out["content"] == "ok"
    assert out["done_reason"] == "stop"
    assert out["eval_count"] == 20
    assert out["output_tokens"] == 20
    assert out["input_tokens"] == 100
    # Local-GPU metrics have no meaning across a network (spec: accepted
    # degradation), and must stay zero rather than carry a plausible lie.
    assert out["ttft_s"] == 0.0
    assert out["prefill_tokens_per_s"] == 0.0
    assert out["tokens_per_s"] >= 0.0


def test_call_lands_in_the_ledger(sent):
    cloud_client._openrouter_chat("anthropic/claude-haiku-4-5", "s", "u", 0.1, {})
    snap = cloud_client.ledger_snapshot()
    assert snap["calls"] == 1
    assert snap["cost_usd"] == pytest.approx(0.01)
    assert snap["providers"] == ("Anthropic",)


def test_http_error_surfaces_the_server_body(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(
            "u", 402, "Payment Required", {},
            __import__("io").BytesIO(b'{"error":"insufficient credits"}'))

    monkeypatch.setattr(cloud_client.urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError) as excinfo:
        cloud_client._openrouter_chat("anthropic/claude-haiku-4-5", "s", "u",
                                      0.1, {})
    assert "402" in str(excinfo.value)
    assert "insufficient credits" in str(excinfo.value)


def test_empty_content_does_not_crash(sent):
    sent["payload"] = {"choices": [{"message": {"content": None},
                                    "finish_reason": "length"}],
                       "usage": {}}
    out = cloud_client._openrouter_chat("anthropic/claude-haiku-4-5", "s", "u",
                                        0.1, {})
    assert out["content"] == ""
    assert out["done_reason"] == "length"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -3.9 -m pytest harness/tests/test_cloud_client.py -v`
Expected: FAIL — `AttributeError: module 'cloud_client' has no attribute 'urllib'` (the module does not import it yet).

- [ ] **Step 3: Write the implementation**

Add to the imports at the top of `cloud_client.py` (it currently imports only `os` and `time`):

```python
import json
import urllib.error
import urllib.request
```

Add below the existing `MAX_OUTPUT` / `DEFAULT_MAX_OUTPUT` constants:

```python
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Pinned, fallbacks off. cloud_client's whole premise is that the harness is
# the constant (see module docstring); OpenRouter arbitrating between providers
# for one model id would close the registry's reservation by opening another.
OPENROUTER_PROVIDER = "anthropic"
TIMEOUT = 1800.0  # same order as llama_client: an attempt can think for minutes


def _bare(model):
    """`anthropic/claude-haiku-4-5` -> `claude-haiku-4-5`.

    The capability tables above are keyed on the model, not on who serves it.
    """
    return model.split("/")[-1]
```

Then the transport:

```python
def _openrouter_chat(model, system, user, temperature, options):
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is unset; the cloud rung "
                           "cannot run")
    bare = _bare(model)
    body = {"model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "provider": {"order": [OPENROUTER_PROVIDER],
                         "allow_fallbacks": False},
            # Same default as _anthropic_chat: the two backends must cap
            # generation identically, or the bride stops being the constant.
            "max_tokens": min(options.get("num_predict", 16384),
                              MAX_OUTPUT.get(bare, DEFAULT_MAX_OUTPUT))}
    if bare in TEMPERATURE_OK:
        body["temperature"] = temperature

    req = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as f:
            payload = json.loads(f.read().decode("utf-8"))
    except urllib.error.URLError as e:      # HTTPError included
        _raise_readable(e)
    elapsed = time.time() - t0

    _record(payload)
    choice = payload["choices"][0]
    usage = payload.get("usage") or {}
    out = usage.get("completion_tokens") or 0
    return {"content": choice["message"].get("content") or "",
            "done_reason": choice.get("finish_reason", ""),
            "eval_count": out,
            "tokens_per_s": (out / elapsed) if elapsed else 0.0,
            # Zero on purpose: these measure a local GPU. A wall-clock guess
            # would be a plausible lie in a file whose job is measurement.
            "prefill_tokens_per_s": 0.0,
            "ttft_s": 0.0,
            "input_tokens": usage.get("prompt_tokens") or 0,
            "output_tokens": out}


def _raise_readable(e):
    """A bare 402 tells the operator nothing; the body says "insufficient
    credits". Surface it. Mirrors llama_client._raise_readable."""
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read().decode("utf-8", errors="replace").strip()
        except Exception:
            body = ""
        raise RuntimeError("OpenRouter HTTP {}: {}".format(
            e.code, body[:500] or e.reason)) from e
    raise RuntimeError("OpenRouter unreachable: {}".format(
        getattr(e, "reason", e))) from e
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -3.9 -m pytest harness/tests/test_cloud_client.py -v`
Expected: PASS, 12 tests.

- [ ] **Step 5: Commit**

```bash
git add harness/tests/test_cloud_client.py experiments/coder_bench/cloud_client.py
git commit -m "feat: OpenRouter transport for the bench cloud rung"
```

---

### Task 3: Dispatch and preflight

**Files:**
- Modify: `experiments/coder_bench/cloud_client.py`
- Test: `harness/tests/test_cloud_client.py`

**Interfaces:**
- Consumes: `_openrouter_chat`, `_bare` from Task 2.
- Produces:
  - `is_cloud(model: str) -> bool` — now true for any id containing `/`.
  - `chat(model, system, user, temperature=0.1, num_ctx=None, options=None) -> dict` — unchanged signature, routes on the id.
  - `preflight(models: list) -> None` — raises `RuntimeError` if a key a listed model needs is absent.

- [ ] **Step 1: Write the failing test**

Append to `harness/tests/test_cloud_client.py`:

```python
def test_is_cloud_covers_both_backends():
    assert cloud_client.is_cloud("anthropic/claude-haiku-4-5")
    assert cloud_client.is_cloud("claude-haiku-4-5")
    assert not cloud_client.is_cloud("qwen3-coder:30b")
    assert not cloud_client.is_cloud("qwen2.5-coder:7b")


def test_chat_routes_prefixed_ids_to_openrouter(sent):
    out = cloud_client.chat("anthropic/claude-haiku-4-5", "s", "u")
    assert out["content"] == "ok"
    assert sent["body"]["provider"]["allow_fallbacks"] is False


def test_preflight_rejects_a_missing_openrouter_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        cloud_client.preflight(["anthropic/claude-haiku-4-5"])
    assert "OPENROUTER_API_KEY" in str(excinfo.value)


def test_preflight_rejects_a_missing_anthropic_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        cloud_client.preflight(["claude-haiku-4-5"])
    assert "ANTHROPIC_API_KEY" in str(excinfo.value)


def test_preflight_ignores_local_models(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cloud_client.preflight(["qwen3-coder:30b", "qwen3.6:35b"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -3.9 -m pytest harness/tests/test_cloud_client.py -v`
Expected: FAIL — `test_is_cloud_covers_both_backends` fails on the prefixed id, and `preflight` does not exist.

- [ ] **Step 3: Write the implementation**

Replace `is_cloud` (`cloud_client.py:24-25`) with:

```python
def is_cloud(model):
    """A provider prefix means OpenRouter; a bare `claude-` means the SDK.

    The prefix is also what makes the routing legible in rows.jsonl, and
    run_bench.slug() already maps `/` to `_`.
    """
    return "/" in model or model.startswith("claude-")


def preflight(models):
    """Fail before cell 1, not at cell 7 of 12."""
    for model in models:
        if "/" in model and not os.environ.get("OPENROUTER_API_KEY"):
            raise RuntimeError("OPENROUTER_API_KEY is unset; the cloud rung "
                               "cannot run ({})".format(model))
        if (model.startswith("claude-")
                and not os.environ.get("ANTHROPIC_API_KEY")):
            raise RuntimeError("ANTHROPIC_API_KEY is unset; the cloud rung "
                               "cannot run ({})".format(model))
```

Rename the existing `chat` (`cloud_client.py:39`) to `_anthropic_chat`, keeping its body and docstring unchanged, and add the dispatcher in its place:

```python
def chat(model, system, user, temperature=0.1, num_ctx=None, options=None):
    """One completion, shaped like the local client's return value.

    `num_ctx` is ignored: the context window is the model's, not ours.
    """
    options = options or {}
    if "/" in model:
        return _openrouter_chat(model, system, user, temperature, options)
    return _anthropic_chat(model, system, user, temperature, num_ctx, options)
```

`_anthropic_chat` keeps the signature `(model, system, user, temperature=0.1, num_ctx=None, options=None)` and its `options = options or {}` first line stays harmless.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -3.9 -m pytest harness/tests/test_cloud_client.py -v`
Expected: PASS, 17 tests.

- [ ] **Step 5: Commit**

```bash
git add harness/tests/test_cloud_client.py experiments/coder_bench/cloud_client.py
git commit -m "feat: route bench cloud models by provider prefix"
```

---

### Task 4: Wire the ledger into run_bench

**Files:**
- Modify: `experiments/coder_bench/run_bench.py` (three sites: `:89-98`, `:136-150`, `:171-185`)

**Interfaces:**
- Consumes: `cloud_client.preflight`, `ledger_snapshot`, `ledger_delta`.
- Produces: rows carrying `cost_usd`, `provider`, `prompt_tokens`, `completion_tokens`; a `total_cost_usd` per model in the summary.

There is no unit test for this task: `run_bench.main()` is a script entry point that drives a real ladder. It is validated by the smoke run in Step 3 and the live cell in Task 5.

- [ ] **Step 1: Add the preflight call**

In `main()`, immediately after `a = ap.parse_args()` (`run_bench.py:119`), insert:

```python
    # A missing key must cost nothing, not eleven cells of a twelve-cell run.
    cloud_client.preflight(a.models)
```

- [ ] **Step 2: Diff the ledger around each cell**

In the cell loop, replace lines `164-183` (from `t0 = time.time()` through `rows.append(row)`) with:

```python
                t0 = time.time()
                spend_before = cloud_client.ledger_snapshot()
                result = loop_job.run_job(
                    job_id, run_dir, CONFIG, chat_fn=chat_fn,
                    unload_fn=lambda m: None,   # one rung: nothing to unload
                    ladder=[stage_for(model, a.attempts)],
                    plateau_k=a.plateau_k or 10 ** 6,
                    gpu_lock_path=JOBS / ".gpu.lock")
                # loop_job builds its attempt records from a fixed whitelist,
                # so cost cannot ride back on the result: diff the ledger
                # instead. On a local model every field here is zero, which
                # keeps the rows homogeneous.
                spend = cloud_client.ledger_delta(spend_before)
                row = {
                    "model": model, "case": case["id"], "expected": case["expected"],
                    "status": result.get("status"),
                    "success": bool(result.get("success")),
                    "attempts": len(result.get("attempts") or []),
                    "failure_reason": result.get("failure_reason"),
                    "regression_passed": result.get("regression_passed"),
                    "diff_bytes": len(result.get("diff") or ""),
                    "seconds": round(time.time() - t0, 1),
                    "error": result.get("error", ""),
                    "archived": case["archived"],
                    "cost_usd": spend["cost_usd"],
                    "prompt_tokens": spend["prompt_tokens"],
                    "completion_tokens": spend["completion_tokens"],
                    # More than one name with allow_fallbacks off is the
                    # evidence the pin did not hold -- keep the whole list.
                    "provider": ",".join(spend["providers"]),
                }
                rows.append(row)
```

- [ ] **Step 3: Put the cost in the summary and on the console**

In `summarize()`, add to the `out[model]` dict (`run_bench.py:89-98`), after `"total_seconds"`:

```python
            "total_cost_usd": round(sum(r.get("cost_usd", 0.0) for r in cell), 4),
```

And extend the per-cell print (`run_bench.py:186-188`) so a long run shows spend as it accrues:

```python
                print("{:20} {:14} {:6} -> {:9} {} att {}s ${}".format(
                    model, case["id"], case["expected"], row["status"],
                    row["attempts"], row["seconds"], row["cost_usd"]), flush=True)
```

- [ ] **Step 4: Verify nothing regressed on the local path**

Run: `py -3.9 -m pytest harness/tests -q`
Expected: PASS — the whole suite, including the new file. This confirms the edits did not break an import.

Then confirm the edited script still imports — a deterministic check that does
not depend on corpus or git state:

Run: `py -3.9 -c "import sys; sys.path.insert(0, 'experiments/coder_bench'); import run_bench; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 5: Commit**

```bash
git add experiments/coder_bench/run_bench.py
git commit -m "feat: record per-cell cost and provider in bench rows"
```

---

### Task 5: Settle the open points on one live cell

This is the task that spends money. Everything before it is offline.

**Files:**
- Modify: `experiments/coder_bench/cloud_client.py` (constants only, if the checks below contradict them)
- Modify: `docs/design/specs/2026-07-27-openrouter-cloud-column-design.md` (close the "Open points" section)

**Interfaces:** none — this task produces facts, not code.

**Prerequisite, done by the operator, not by the implementer:** a credit ceiling set on the OpenRouter key, and `OPENROUTER_API_KEY` exported in the environment. Do not proceed without confirming both.

- [ ] **Step 1: Resolve the exact model id and provider slug**

Run:

```bash
curl -s https://openrouter.ai/api/v1/models | py -3.9 -c "import json,sys; [print(m['id']) for m in json.load(sys.stdin)['data'] if 'claude' in m['id']]"
```

Pick the intended Claude model id from that list. Then confirm the provider slug:

```bash
curl -s "https://openrouter.ai/api/v1/models/<id>/endpoints" | py -3.9 -m json.tool
```

Expected: a `provider_name` / slug field naming the Anthropic first-party endpoint.

If the slug is not `"anthropic"`, update `OPENROUTER_PROVIDER` in `cloud_client.py`. If the model id's bare form is not already a key in `TEMPERATURE_OK` / `MAX_OUTPUT`, add it — a missing entry silently falls back to `DEFAULT_MAX_OUTPUT` and drops temperature, which would quietly change the bench conditions.

- [ ] **Step 2: Run one cell**

Pick a job id present in the corpus (any `j_…` directory under `experiments/coder_bench/runs/bancA-bride/`), then:

```bash
py -3.9 experiments/coder_bench/run_bench.py --models anthropic/<id> --only j_8e0944d1 --out experiments/coder_bench/runs/or-probe
```

Expected: one row in `runs/or-probe/rows.jsonl` with a non-zero `cost_usd`, a non-empty `provider`, and a `status` that is not `error`.

- [ ] **Step 3: Check the pin held and price the full run**

Read the row. Confirm `provider` holds exactly one name and that it is the pinned one. If it reads `unknown`, the response carries no provider field — record that in the spec and rely on the no-fallback pin as the guarantee, as the spec already allows.

Multiply `cost_usd` by 12 to sanity-check the 5–10 USD estimate before committing to the full corpus. If it lands far above, stop and report rather than launching.

- [ ] **Step 4: Close the open points in the spec**

Rewrite the "Open points, to settle at implementation" section of the spec with the answers found: the model id used, the provider slug, and whether the response exposes the serving provider.

- [ ] **Step 5: Commit**

```bash
git add docs/design/specs/2026-07-27-openrouter-cloud-column-design.md experiments/coder_bench/cloud_client.py
git commit -m "docs: settle OpenRouter model id and provider slug on a live cell"
```

---

## After the plan

The full run is a separate operation, not a plan task, and it belongs to the operator:

```powershell
Start-Process -FilePath "py" -ArgumentList "-3.9","experiments/coder_bench/run_bench.py","--models","anthropic/<id>","--out","experiments/coder_bench/runs/bancA-cloud" -RedirectStandardOutput runs\bancA-cloud.log -RedirectStandardError runs\bancA-cloud.err -NoNewWindow
```

Detached with log files, never through a task runner — `CLAUDE.md` records a smoke run killed by the task infrastructure on 18/07.

The deliverable that closes the loop is not the code: it is the reservation at `docs/backlog-optimisations.md:20` rewritten as a verdict, and the two leviers it gates (`:21`, `:22`) unblocked or confirmed blocked on the numbers.
