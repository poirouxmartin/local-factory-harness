"""Who serves a model, and how to address them.

The factory used to have exactly one cloud route, so "cloud" and "OpenRouter"
were the same word. They are not: the operator wants to spend their own
accounts directly, which means several endpoints, several keys, and one
routing rule that has to be unambiguous.

The rule is a sigil. An id that starts with `@` names a direct route:

    @anthropic/claude-haiku-4.5   -> Anthropic, model `claude-haiku-4.5`
    @mistral/mistral-large-latest -> Mistral,   model `mistral-large-latest`
    nvidia/nemotron-3-ultra:free  -> OpenRouter, model unchanged

Why a sigil and not the first path segment: OpenRouter's own ids are
`vendor/model`, and `openai/gpt-oss-20b:free` is an OpenRouter id whose vendor
happens to be OpenAI. Routing on the vendor would send it to OpenAI's API,
where it does not exist. `@` cannot collide with anything a registry serves.

Every route here speaks the OpenAI chat-completions protocol. That is not a
coincidence we are lucky to have -- it is the reason this list is short and
the transport is one file: Anthropic and Google both publish OpenAI-compatible
endpoints alongside their native ones, so the factory's single SSE parser
keeps working. A route whose vendor drops that compatibility needs a real
adapter, and does not belong in this table.
"""

SIGIL = "@"
DEFAULT_ROUTE = "openrouter"

# `pin`: whether the route arbitrates between upstream providers for one model
# id. Only an aggregator does, and only there does pinning mean anything.
#
# `usage_ext`: whether the route understands OpenRouter's `usage: {include}`
# extension -- the only thing that returns a PRICE for the turn. Sending it
# anywhere else is a hard 400: Google's shim validates the payload strictly and
# answers `Unknown name "usage": Cannot find field`, which killed every Gemini
# session on the first turn. Everyone else gets the standard
# `stream_options: {include_usage}`, which counts tokens but knows no cost.
# A separate key from `pin` although today they agree: a route can start
# reporting a price without becoming an aggregator, and vice versa.
ROUTES = {
    "openrouter": {
        "label": "OpenRouter",
        "key": "OPENROUTER_API_KEY",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "console": "https://openrouter.ai/keys",
        "pin": True,
        "usage_ext": True,
    },
    "anthropic": {
        "label": "Anthropic",
        "key": "ANTHROPIC_API_KEY",
        # Anthropic's OpenAI-compatibility endpoint. The native Messages API
        # would need its own adapter for zero gain here: the factory speaks
        # OpenAI tool-calling everywhere else.
        "url": "https://api.anthropic.com/v1/chat/completions",
        "console": "https://console.anthropic.com/settings/keys",
        "pin": False,
    },
    "openai": {
        "label": "OpenAI",
        "key": "OPENAI_API_KEY",
        "url": "https://api.openai.com/v1/chat/completions",
        "console": "https://platform.openai.com/api-keys",
        "pin": False,
    },
    "mistral": {
        "label": "Mistral",
        "key": "MISTRAL_API_KEY",
        "url": "https://api.mistral.ai/v1/chat/completions",
        "console": "https://console.mistral.ai/api-keys",
        "pin": False,
    },
    "deepseek": {
        "label": "DeepSeek",
        "key": "DEEPSEEK_API_KEY",
        "url": "https://api.deepseek.com/chat/completions",
        "console": "https://platform.deepseek.com/api_keys",
        "pin": False,
    },
    "gemini": {
        "label": "Google Gemini",
        "key": "GEMINI_API_KEY",
        "url": ("https://generativelanguage.googleapis.com/v1beta/openai/"
                "chat/completions"),
        "console": "https://aistudio.google.com/apikey",
        "pin": False,
    },
}


def split(model):
    """`model id -> (route name, id as the route spells it)`.

    An unknown route after the sigil is left as a route name so the caller can
    fail with "unknown provider `@foo`" rather than silently posting a
    malformed id to OpenRouter.
    """
    model = model or ""
    if not model.startswith(SIGIL):
        return DEFAULT_ROUTE, model
    body = model[len(SIGIL):]
    route, _, remote = body.partition("/")
    return route, remote


def known(route):
    return route in ROUTES


def config(model):
    """The route table entry for a model id. Raises on an unknown route."""
    route, _ = split(model)
    try:
        return ROUTES[route]
    except KeyError:
        raise RuntimeError(
            "unknown provider {!r} in model id {!r}; known routes: {}"
            .format(route, model, ", ".join(sorted(ROUTES))))


def remote_id(model):
    """What to put in the request body -- the id the route itself uses."""
    return split(model)[1]


def label(model):
    return config(model)["label"]


def is_browser(model):
    """Does this route need a browser rather than an API key?

    The one question that has to be answerable BEFORE the credential check:
    a browser route has no key to require, and demanding one would fail every
    session on it before the first turn.
    """
    try:
        return bool(config(model).get("browser"))
    except RuntimeError:
        return False


def usage_ext(model):
    """Does this route take OpenRouter's `usage: {include}`? Absent means no --
    the safe default, since sending it to a strict shim is a 400, not a
    missing price."""
    return bool(config(model).get("usage_ext"))


def by_url(url):
    """The route that owns an endpoint, or None.

    Used by the transport's error path: it holds a request, not a model id, and
    threading the route through a module-level would break the moment two
    sessions run at once -- which is the whole point of the cloud lane.
    """
    for name, cfg in ROUTES.items():
        # A browser route has no url. Skipping it explicitly stops `None == None`
        # from handing it every request whose url the error path lost.
        if cfg["url"] and cfg["url"] == url:
            return name
    return None


def catalogue():
    """The routes, for the UI's Providers tab. No secrets: this is a table of
    endpoints, and what is or is not configured comes from `credentials`."""
    return [{"route": name, "label": cfg["label"], "key": cfg["key"],
             "console": cfg["console"],
             # A browser route has no key field to offer: the studio shows it
             # as connected or not, and the operator signs in once.
             "browser": bool(cfg.get("browser")),
             "prefix": SIGIL + name + "/" if name != DEFAULT_ROUTE else ""}
            for name, cfg in sorted(ROUTES.items(),
                                    key=lambda kv: kv[1]["label"])]
