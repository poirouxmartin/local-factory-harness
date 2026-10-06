"""Routing a model id to whoever serves it.

The one property worth a file of its own: the rule must never send an
OpenRouter id to a vendor's own API. `openai/gpt-oss-20b:free` is served by
OpenRouter and does not exist at OpenAI.
"""
import pytest

import providers


def test_a_bare_id_stays_on_the_aggregator():
    assert providers.split("nvidia/nemotron-3-ultra-550b-a55b:free") == (
        "openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free")


def test_a_vendor_prefixed_openrouter_id_is_not_mistaken_for_that_vendor():
    """The trap the sigil exists to avoid."""
    route, remote = providers.split("openai/gpt-oss-20b:free")
    assert route == "openrouter"
    assert remote == "openai/gpt-oss-20b:free"


def test_the_sigil_names_a_direct_route():
    assert providers.split("@anthropic/claude-haiku-4.5") == (
        "anthropic", "claude-haiku-4.5")
    assert providers.split("@mistral/mistral-large-latest") == (
        "mistral", "mistral-large-latest")


def test_the_remote_id_drops_the_route_but_keeps_the_rest():
    # A direct id can carry slashes of its own; only the first segment routes.
    assert providers.remote_id("@gemini/models/gemini-3-flash") == \
        "models/gemini-3-flash"


def test_an_unknown_route_fails_by_name_instead_of_silently_defaulting():
    with pytest.raises(RuntimeError) as excinfo:
        providers.config("@nope/some-model")
    msg = str(excinfo.value)
    assert "nope" in msg
    assert "openrouter" in msg      # tells the operator what does exist


def test_only_the_aggregator_pins_a_provider():
    assert providers.config("nvidia/x:free")["pin"] is True
    assert providers.config("@anthropic/claude-haiku-4.5")["pin"] is False


def test_every_route_declares_what_the_transport_needs():
    # Two shapes, and a route must be exactly one of them: an HTTP route is an
    # endpoint plus a key, a browser route is neither and says so. A route with
    # no url and no `browser` flag would reach the transport and post to None.
    for name, cfg in providers.ROUTES.items():
        assert cfg["label"] and cfg["console"], name
        if cfg.get("browser"):
            assert cfg["url"] is None and cfg["key"] is None, name
        else:
            assert cfg["url"].startswith("https://"), name
            assert cfg["key"].endswith("_API_KEY"), name


def test_a_url_maps_back_to_its_route():
    # The error path holds a request, not a model id.
    for name, cfg in providers.ROUTES.items():
        if cfg.get("browser"):
            continue
        assert providers.by_url(cfg["url"]) == name
    assert providers.by_url("https://example.invalid/") is None


def test_a_route_without_an_endpoint_owns_no_request():
    """`by_url` compares against every row, and a browser route's url is None:
    without a guard it would claim any request whose url went missing."""
    assert providers.by_url(None) is None
    assert providers.by_url("") is None


def test_the_catalogue_shows_the_prefix_to_type():
    rows = {r["route"]: r for r in providers.catalogue()}
    assert rows["anthropic"]["prefix"] == "@anthropic/"
    # The default route needs no prefix, and saying otherwise would make every
    # existing id look wrong.
    assert rows["openrouter"]["prefix"] == ""


def test_the_catalogue_carries_no_secret():
    blob = repr(providers.catalogue())
    assert "API_KEY" in blob          # the NAME is fine
    assert "sk-" not in blob
