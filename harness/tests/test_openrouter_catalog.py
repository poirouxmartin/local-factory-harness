"""The OpenRouter catalogue. No network: what matters is that a record is read
into the fields the factory reasons about, and that an outage degrades to the
last good answer instead of to no answer at all.
"""
import json
import urllib.error

import pytest

import openrouter_catalog as cat


def _record(mid="nvidia/nemotron-3-super-120b-a12b:free", ctx=262144,
            top_ctx=None, prompt="0", completion="0", params=("tools",),
            name=None, max_out=8192):
    return {"id": mid, "name": name or ("NVIDIA: " + mid),
            "context_length": ctx,
            "top_provider": {"context_length": top_ctx if top_ctx is not None
                             else ctx, "max_completion_tokens": max_out},
            "pricing": {"prompt": prompt, "completion": completion},
            "supported_parameters": list(params),
            "architecture": {"input_modalities": ["text"]}}


def _opener(payload):
    class _Resp:
        def read(self):
            return json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(url, timeout=None):
        return _Resp()

    return opener


# ---- normalize ----

def test_a_record_becomes_the_fields_the_factory_reasons_about():
    m = cat.normalize(_record())
    assert m["id"] == "nvidia/nemotron-3-super-120b-a12b:free"
    assert m["context_length"] == 262144
    assert m["max_output"] == 8192
    assert m["tools"] is True
    assert m["free"] is True
    assert m["usd_per_mtok_out"] == 0.0


def test_the_served_window_wins_over_the_advertised_one():
    # The model-level number is the best any provider offers; our request meets
    # the endpoint the top provider serves.
    m = cat.normalize(_record(ctx=1000000, top_ctx=131072))
    assert m["context_length"] == 131072


def test_free_is_read_from_the_price_not_from_the_name():
    paid = cat.normalize(_record(mid="anthropic/claude-haiku-4-5",
                                 prompt="0.000001", completion="0.000005"))
    assert paid["free"] is False
    assert paid["usd_per_mtok_in"] == 1.0
    assert paid["usd_per_mtok_out"] == 5.0
    # A `:free` suffix with a price on it would be a lie the catalogue must not
    # repeat.
    liar = cat.normalize(_record(mid="vendor/model:free", prompt="0.00001",
                                 completion="0.00002"))
    assert liar["free"] is False


def test_a_model_without_tools_is_marked_as_such():
    assert cat.normalize(_record(params=()))["tools"] is False


def test_missing_pricing_does_not_crash():
    rec = _record()
    del rec["pricing"]
    assert cat.normalize(rec)["free"] is True


# ---- fetch ----

def test_fetch_normalizes_and_sorts():
    payload = {"data": [_record(mid="z/last"), _record(mid="a/first"),
                        {"name": "no id"}]}
    models = cat.fetch(opener=_opener(payload))
    assert [m["id"] for m in models] == ["a/first", "z/last"]


def test_fetch_raises_a_readable_error(monkeypatch):
    def boom(url, timeout=None):
        raise urllib.error.URLError("no route to host")

    with pytest.raises(RuntimeError) as excinfo:
        cat.fetch(opener=boom)
    assert "unreachable" in str(excinfo.value)


# ---- load: the cache is the point ----

@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "factory.toml"
    path.write_text("", encoding="utf-8")
    return str(path)


def test_first_load_hits_the_network_and_writes_the_cache(cfg):
    models, meta = cat.load(cfg, fetch_fn=lambda: [{"id": "a/b"}], now=1000.0)
    assert meta["source"] == "network"
    assert meta["stale"] is False
    models2, meta2 = cat.load(cfg, fetch_fn=_never, now=1100.0)
    assert models2 == models
    assert meta2["source"] == "cache"


def _never():
    raise AssertionError("must not hit the network")


def test_a_stale_cache_is_refreshed(cfg):
    cat.load(cfg, fetch_fn=lambda: [{"id": "old"}], now=0.0)
    models, meta = cat.load(cfg, fetch_fn=lambda: [{"id": "new"}],
                            max_age_s=60, now=1000.0)
    assert [m["id"] for m in models] == ["new"]
    assert meta["source"] == "network"


def test_force_refetches_a_fresh_cache(cfg):
    cat.load(cfg, fetch_fn=lambda: [{"id": "old"}], now=0.0)
    models, _ = cat.load(cfg, fetch_fn=lambda: [{"id": "new"}], force=True,
                         now=1.0)
    assert [m["id"] for m in models] == ["new"]


def test_an_outage_degrades_to_the_cache_and_says_so(cfg):
    cat.load(cfg, fetch_fn=lambda: [{"id": "a/b"}], now=0.0)

    def down():
        raise RuntimeError("OpenRouter catalogue unreachable: boom")

    models, meta = cat.load(cfg, fetch_fn=down, max_age_s=0, now=1000.0)
    assert [m["id"] for m in models] == ["a/b"]
    # Stale must be visible: a vanished model looking present is the failure
    # mode this flag exists to prevent.
    assert meta["stale"] is True
    assert "boom" in meta["error"]


def test_an_outage_with_no_cache_raises(cfg):
    def down():
        raise RuntimeError("OpenRouter catalogue unreachable: boom")

    with pytest.raises(RuntimeError):
        cat.load(cfg, fetch_fn=down)


def test_a_corrupt_cache_is_ignored_not_fatal(cfg, tmp_path):
    cat.load(cfg, fetch_fn=lambda: [{"id": "a/b"}], now=0.0)
    (tmp_path / "cache" / "openrouter_models.json").write_text(
        "{not json", encoding="utf-8")
    models, meta = cat.load(cfg, fetch_fn=lambda: [{"id": "fresh"}], now=1.0)
    assert [m["id"] for m in models] == ["fresh"]
    assert meta["source"] == "network"


# ---- quota ----

def test_quota_derives_the_daily_cap_from_the_tier(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    q = cat.quota(opener=_opener({"data": {"is_free_tier": True, "usage": 0}}))
    assert q["free_tier"] is True
    assert q["free_rpm"] == cat.FREE_RPM
    assert q["free_rpd"] == cat.FREE_RPD
    assert "10" in q["upgrade_hint"] and str(cat.FREE_RPD_WITH_CREDITS) in \
        q["upgrade_hint"]


def test_a_paying_key_gets_the_higher_cap(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    q = cat.quota(opener=_opener({"data": {"is_free_tier": False,
                                           "usage": 1.25}}))
    assert q["free_rpd"] == cat.FREE_RPD_WITH_CREDITS
    assert q["usage_usd"] == 1.25


def test_quota_without_a_key_fails_before_the_network(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    def never(*a, **kw):
        raise AssertionError("must not reach the network")

    with pytest.raises(RuntimeError) as excinfo:
        cat.quota(opener=never)
    assert "OPENROUTER_API_KEY" in str(excinfo.value)
