"""The OpenRouter catalogue: what the network actually serves, today.

Why this exists: the cloud models used to be three ids typed into
factory.toml by hand. That list cannot say what a model's context window is,
whether it takes tools, whether it is free, or whether it still exists -- so
every one of those facts was either guessed or wrong. The registry that knows
them is `GET /api/v1/models`, and it is public: no key, no cost, no quota.

Two rules shape the module:

  * The network is a cache, not a dependency. A studio that starts without
    internet must still list its models, so the last good answer is written to
    disk and reused. A stale catalogue is a far smaller problem than a studio
    that cannot open a session.
  * Nothing here decides what the operator may use. It reports; the config
    filters (`factory_config.load_cloud_models`). Mixing the two would mean a
    provider's outage silently rewrites the operator's allowlist.
"""
import json
import os
import time
import urllib.error
import urllib.request

import credentials

MODELS_URL = "https://openrouter.ai/api/v1/models"
KEY_URL = "https://openrouter.ai/api/v1/key"
TIMEOUT = 30.0
MAX_AGE_S = 24 * 3600      # a day: models appear and vanish in weeks, not hours

# The free tier is rationed in REQUESTS, not tokens, and one agent turn spends
# one request per tool call -- which is why these numbers belong on screen and
# not in a doc. Source: OpenRouter's published limits, 2026-07-29.
FREE_RPM = 20
FREE_RPD = 50
FREE_RPD_WITH_CREDITS = 1000
CREDITS_THRESHOLD_USD = 10


def _cache_path(config_path):
    return os.path.join(os.path.dirname(os.path.abspath(config_path)),
                        "cache", "openrouter_models.json")


def normalize(record):
    """One API record -> the fields the factory reasons about.

    `context_length` is taken from `top_provider` when it is there: the
    model-level number is the best any provider offers, while the served
    endpoint is what our request will actually meet.
    """
    top = record.get("top_provider") or {}
    pricing = record.get("pricing") or {}

    def _usd(key):
        try:
            return float(pricing.get(key) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    prompt_usd, completion_usd = _usd("prompt"), _usd("completion")
    params = record.get("supported_parameters") or []
    arch = record.get("architecture") or {}
    return {
        "id": record["id"],
        "label": record.get("name") or record["id"],
        "context_length": (top.get("context_length")
                           or record.get("context_length") or 0),
        "max_output": top.get("max_completion_tokens") or 0,
        "tools": "tools" in params,
        # Free means both sides priced at zero. `:free` in the id is a naming
        # convention, not a guarantee, and reading the price cannot drift.
        "free": prompt_usd == 0.0 and completion_usd == 0.0,
        "usd_per_mtok_in": round(prompt_usd * 1e6, 4),
        "usd_per_mtok_out": round(completion_usd * 1e6, 4),
        "modalities": arch.get("input_modalities") or ["text"],
    }


def fetch(timeout=TIMEOUT, opener=None):
    """The live catalogue, normalized and sorted. Raises on network failure --
    the caller decides whether a stale cache beats no answer."""
    opener = opener or urllib.request.urlopen
    try:
        with opener(MODELS_URL, timeout=timeout) as f:
            payload = json.loads(f.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError) as e:
        raise RuntimeError("OpenRouter catalogue unreachable: {}".format(e))
    models = [normalize(r) for r in payload.get("data") or [] if r.get("id")]
    models.sort(key=lambda m: m["id"])
    return models


def _read_cache(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            blob = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(blob, dict) or not isinstance(blob.get("models"), list):
        return None
    return blob


def _write_cache(path, models, now):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": now, "models": models}, f)
        os.replace(tmp, path)          # never leave a half-written catalogue
    except OSError:
        pass                           # a cache that cannot be written is not
                                       # a reason to fail the call


def load(config_path, max_age_s=MAX_AGE_S, force=False, now=None,
         fetch_fn=None):
    """The catalogue, from cache when it is fresh enough, else from the network.

    Returns `(models, meta)` where meta carries `fetched_at`, `stale` and, when
    the network was tried and failed, `error`. The operator has to be able to
    tell "these are today's models" from "these are the models I had on
    Sunday" -- a silent fallback would make a vanished model look present.
    """
    now = time.time() if now is None else now
    fetch_fn = fetch_fn or fetch
    path = _cache_path(config_path)
    cached = _read_cache(path)
    age = (now - cached["fetched_at"]) if cached else None
    if cached and not force and age is not None and age < max_age_s:
        return cached["models"], {"fetched_at": cached["fetched_at"],
                                  "stale": False, "source": "cache"}
    try:
        models = fetch_fn()
    except RuntimeError as e:
        if cached:
            return cached["models"], {"fetched_at": cached["fetched_at"],
                                      "stale": True, "source": "cache",
                                      "error": str(e)}
        raise
    _write_cache(path, models, now)
    return models, {"fetched_at": now, "stale": False, "source": "network"}


def quota(config_path=None, timeout=TIMEOUT, opener=None):
    """What the key is allowed to spend, from `GET /api/v1/key`.

    The daily cap on free models is not in that response -- it is a function of
    how much has ever been purchased -- so it is derived here rather than left
    for the UI to hardcode.
    """
    key = credentials.require("OPENROUTER_API_KEY", config_path,
                              purpose="the OpenRouter quota")
    opener = opener or urllib.request.urlopen
    req = urllib.request.Request(KEY_URL,
                                 headers={"Authorization": "Bearer " + key})
    try:
        with opener(req, timeout=timeout) as f:
            data = (json.loads(f.read().decode("utf-8")) or {}).get("data") or {}
    except (urllib.error.URLError, ValueError) as e:
        raise RuntimeError("OpenRouter quota unreachable: {}".format(e))
    free_tier = bool(data.get("is_free_tier", True))
    return {
        "free_tier": free_tier,
        "usage_usd": round(float(data.get("usage") or 0.0), 4),
        "usage_daily_usd": round(float(data.get("usage_daily") or 0.0), 4),
        "credit_limit_usd": data.get("limit"),
        "free_rpm": FREE_RPM,
        "free_rpd": FREE_RPD if free_tier else FREE_RPD_WITH_CREDITS,
        # The lever, stated where it is read: 10 USD once multiplies the daily
        # allowance by 20. Without this the operator sees a wall and no door.
        "upgrade_hint": (
            "{} requests/day on free models; buying {} USD of credits raises "
            "it to {}. One agent turn spends one request per tool call."
            .format(FREE_RPD, CREDITS_THRESHOLD_USD, FREE_RPD_WITH_CREDITS)
            if free_tier else
            "{} requests/day on free models.".format(FREE_RPD_WITH_CREDITS)),
    }
