"""Where the factory's API keys live.

One file, `secrets.toml` at the repo root, never committed. It exists because
the operator asked to authenticate providers from the studio rather than from a
shell profile, and a key typed into a web form has to land somewhere.

Three rules, and they are the whole design:

  * **The environment still wins.** A key exported before launch overrides the
    file. Scripts, CI and one-off runs must not change behaviour because a
    studio wrote a file once.
  * **A value never leaves this module.** Everything the UI and the logs get is
    a `hint` -- first four characters and last three. There is no endpoint, no
    log line and no prompt that can carry the secret itself, because none of
    them is ever handed one.
  * **Writing is atomic and narrow.** The file is replaced, never appended to,
    so a crash mid-write cannot leave a half-key; and on POSIX it is created
    0600. Windows has no equivalent one-liner -- the file lives inside the
    operator's own profile and is gitignored, which is what protects it there.
"""
import os
import stat

try:  # 3.11+
    import tomllib
except ImportError:
    import tomli as tomllib

FILENAME = "secrets.toml"

# The keys the factory knows how to use. Declared, not free-form: a typo in a
# provider name must fail at the door rather than silently store a secret under
# a name nothing reads.
KNOWN = {
    "OPENROUTER_API_KEY": "OpenRouter",
    "ANTHROPIC_API_KEY": "Anthropic",
    "OPENAI_API_KEY": "OpenAI",
    "MISTRAL_API_KEY": "Mistral",
    "DEEPSEEK_API_KEY": "DeepSeek",
    "GEMINI_API_KEY": "Google Gemini",
}


def path_for(config_path):
    return os.path.join(os.path.dirname(os.path.abspath(config_path)), FILENAME)


def _read(config_path):
    try:
        with open(path_for(config_path), "rb") as f:
            raw = tomllib.load(f)
    except (OSError, ValueError):
        return {}
    keys = raw.get("keys")
    if not isinstance(keys, dict):
        return {}
    return {k: v for k, v in keys.items() if isinstance(v, str) and v}


def get(name, config_path=None):
    """The key, or None. Environment first: an explicit export outranks a
    stored value, so nothing the studio writes can shadow a deliberate one."""
    from_env = os.environ.get(name)
    if from_env:
        return from_env
    if config_path is None:
        return None
    return _read(config_path).get(name)


def require(name, config_path=None, purpose=""):
    """The key, or a RuntimeError that says how to supply it."""
    value = get(name, config_path)
    if value:
        return value
    raise RuntimeError(
        "{} is unset{}; add it in the studio's Providers tab or export it"
        .format(name, " -- " + purpose if purpose else ""))


def hint(value):
    """What may be shown: enough to recognise a key, never enough to use it."""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "..." + value[-3:]


def status(config_path):
    """Per provider: is a key present, where does it come from, what does it
    look like. This is the only shape that ever reaches the UI."""
    stored = _read(config_path)
    out = []
    for name, label in sorted(KNOWN.items(), key=lambda kv: kv[1]):
        env_value = os.environ.get(name)
        value = env_value or stored.get(name)
        out.append({
            "name": name,
            "provider": label,
            "set": bool(value),
            # Which one is live matters: an operator who edits the file and
            # sees no change is looking at an environment variable winning.
            "source": "env" if env_value else ("file" if value else None),
            "hint": hint(value),
        })
    return out


def put(config_path, name, value):
    """Store or clear one key. An empty value removes it."""
    if name not in KNOWN:
        raise ValueError("unknown credential {!r}".format(name))
    keys = _read(config_path)
    if value:
        keys[name] = value.strip()
    else:
        keys.pop(name, None)
    _write(config_path, keys)
    return status(config_path)


def _write(config_path, keys):
    target = path_for(config_path)
    tmp = target + ".tmp"
    body = ["# Written by the studio. Never commit this file.", "", "[keys]"]
    for name in sorted(keys):
        # TOML basic strings: the only characters that can appear in an API key
        # and break the file are the quote and the backslash.
        escaped = keys[name].replace("\\", "\\\\").replace('"', '\\"')
        body.append('{} = "{}"'.format(name, escaped))
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(body) + "\n")
    try:
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)   # no-op on Windows
    except OSError:
        pass
    os.replace(tmp, target)
