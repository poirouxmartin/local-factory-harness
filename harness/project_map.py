"""A map of one workspace: where things are, and what the agent learned.

Generated content states WHERE (from `git ls-files`, the only authority on what
belongs to a project); annotations state WHY. An annotation can never
contradict the generated line it hangs under, and one whose anchor file changed
renders as stale rather than lying.

Why this exists: audited over the 256 stored sessions (2026-07-27), 17% of all
tool calls were byte-identical replays -- one session read `harness/web/app.js`
29 times, a 56 KB file that is ~30% of the chat window, so every read crossed
the compaction threshold that then evicted it. Meanwhile MEMORY.md asserted the
backend was FastAPI, which appears nowhere in the repository. See
docs/design/specs/2026-07-27-project-map-design.md.

Pure functions over plain dicts, no I/O: `factory_mcp` owns git, the files and
MEMORY.md. The rendered block goes into the agent system prompt, which is the
KV prefix (`--cache-reuse`, locked 2026-07-19), so rendering is deterministic
and bounded: same entries in, same bytes out.
"""

import io
import re
import token as _token
import tokenize

# Measured on local-factory 2026-07-27: 253 git-tracked files (99 .py, 92 .md,
# 55 .json) render to 20546 chars, ~6850 tokens. The first ceiling was 12000,
# tuned on code files alone, and it made the real repo fall back to the
# directory view -- which says `harness/ (32 files)`, exactly what the list_dir
# call it replaces already said. A ceiling that degrades the case it was built
# for is the wrong ceiling.
#
# 32000 is that measurement plus room to roughly 390 files. It is deliberately
# not set just above today's size: the previous value left 454 chars of margin,
# so the next handful of commits would have tipped the map into the degraded
# view. `degraded()` exists so that when it does happen it is never silent.
MAX_MAP_CHARS = 32000
MAX_NOTE_CHARS = 160
MAX_NOTES_PER_STEP = 8
DOC_CHARS = 88

HEADER = "--- Project map (git ls-files) ---"
FOOTER = "--- End project map ---"
STALE_MARK = " (périmé)"
# Rendered into the block itself when the per-file view does not fit, so the
# model is told it is seeing directories instead of silently believing the
# project holds nothing but folders. `{n}` is filled with the file count.
_DEGRADED_PREFIX = "(too many files for the per-file view"
_DEGRADED_MARK = (_DEGRADED_PREFIX + ": {n} files, directories only -- "
                  "list_dir to go deeper)")

# Markers, not a whole-file rewrite: MEMORY.md is a project file a human owns.
# Same discipline as workspace_memory's `factory:auto` and `factory:carnet`.
BEGIN = "<!-- factory:map -->"
END = "<!-- /factory:map -->"
PREAMBLE = ("Written by the agent, checked against the repository. "
            "Edit above this marker; this block is regenerated.")


def skeleton(entries):
    """One `path (NL) doc` line per entry, sorted by path."""
    out = []
    for e in sorted(entries, key=lambda x: x["path"]):
        line = "{} ({}L)".format(e["path"], e["lines"])
        doc = (e.get("doc") or "").strip()[:DOC_CHARS]
        if doc:
            line += " " + doc
        out.append(line)
    return out


def stale(notes, blobs):
    """Copy of `notes` with `stale` set. A note is never dropped: one the agent
    can no longer trust is information; one that vanishes is not."""
    return [dict(n, stale=n.get("blob") != blobs.get(n["path"]))
            for n in notes]


def _directory_lines(entries):
    """The degraded view: one line per directory, file count and total size."""
    dirs = {}
    for e in entries:
        d = e["path"].rsplit("/", 1)[0] + "/" if "/" in e["path"] else "./"
        n, total = dirs.get(d, (0, 0))
        dirs[d] = (n + 1, total + e["lines"])
    return ["{} ({} files, {}L)".format(d, n, total)
            for d, (n, total) in sorted(dirs.items())]


def degraded(block):
    """True when `block` is the directory-level fallback rather than the real
    map. The caller warns on it: a map that quietly stops naming files looks
    exactly like a map, and the agent goes back to guessing paths."""
    return bool(block) and _DEGRADED_PREFIX in block


def render(entries, notes, budget=MAX_MAP_CHARS):
    """The prompt block. Deterministic and bounded: this is the KV prefix."""
    if not entries:
        return ""
    by_path = {n["path"]: n for n in sorted(notes, key=lambda x: x["path"])}
    ordered = sorted(entries, key=lambda x: x["path"])
    lines = []
    for e, line in zip(ordered, skeleton(ordered)):
        lines.append(line)
        note = by_path.get(e["path"])
        if not note:
            continue
        text = (note.get("text") or "").strip()[:MAX_NOTE_CHARS]
        if note.get("stale"):
            text += STALE_MARK
        lines.append("    ^ " + text)
    block = "\n".join([HEADER] + lines + [FOOTER])
    if len(block) <= budget:
        return block
    # Degrade whole, never truncate: a cut list silently hides whatever sorts
    # last, and the agent cannot tell the difference between "absent" and
    # "did not fit". The marker line says so in the prompt itself, so the model
    # knows it is looking at directories and must list them to go further.
    return "\n".join([HEADER, _DEGRADED_MARK.format(n=len(ordered))]
                     + _directory_lines(ordered) + [FOOTER])


_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BACKTICKED_RE = re.compile(r"`([^`]+)`")
_WORD_RE = re.compile(r"[^\s,;:()\[\]]+")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_./-]*$")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


# Prose is not evidence. Two failures forced this, both found 2026-07-27:
# MEMORY.md is itself a source, so the false "FastAPI" claim became its own
# proof and could never be rejected again; and a docstring written to DOCUMENT
# that bug re-armed it. A name counts only where the code uses it.
EVIDENCE_SUFFIXES = (".py", ".js", ".css", ".html", ".toml", ".json")


def _code_names(path, text):
    """Names the code itself uses, comments and string literals excluded.

    Python is tokenised properly; anything else falls back to a plain scan,
    which is looser but still beats trusting prose.
    """
    if path.endswith(".py"):
        try:
            return {t.string for t in
                    tokenize.generate_tokens(io.StringIO(text).readline)
                    if t.type == _token.NAME}
        except (tokenize.TokenError, IndentationError, SyntaxError):
            pass                      # unparseable: fall through to the scan
    return set(_TOKEN_RE.findall(text))


def vocabulary(sources):
    """Every name the repository evidences: identifiers, paths, path parts.

    Built once per session and reused across every note checked -- rebuilding
    it per line would make verification cost more than the notes are worth.
    """
    vocab = set()
    for path, text in sources.items():
        vocab.add(path)
        for part in path.replace("/", " ").replace(".", " ").split():
            vocab.add(part)
        if path.endswith(EVIDENCE_SUFFIXES):
            vocab.update(_code_names(path, text))
    return vocab


def _supported(word, vocab):
    """True when the repo evidences this word, whole or by each of its parts."""
    if word in vocab:
        return True
    parts = [p for p in re.split(r"[./-]", word) if p]
    return bool(parts) and all(p in vocab for p in parts)


def _is_checkable(word):
    """A word that asserts something the repo can confirm or deny: an
    identifier, a path, or a name that is not plain lowercase prose."""
    if not _IDENTIFIER_RE.match(word):
        return False
    return ("_" in word or "/" in word or "." in word
            or word != word.lower())


def verify(note_text, vocab):
    """The first token the repository cannot evidence, or None.

    Catches invented names -- the observed failure, MEMORY.md having asserted a
    FastAPI backend for two days. It does NOT catch a false causal claim built
    from real identifiers; nothing mechanical would, and pretending otherwise
    repeats the mistake of trusting the prose.
    """
    for quoted in _BACKTICKED_RE.findall(note_text):
        q = quoted.strip()
        if q and not _supported(q, vocab):
            return q
    for sentence in _SENTENCE_SPLIT_RE.split(_BACKTICKED_RE.sub(" ",
                                                                note_text)):
        for i, raw in enumerate(_WORD_RE.findall(sentence)):
            word = raw.strip(".,;:!?")
            if not word:
                continue
            if i == 0 and word[:1].isupper() and not (
                    "_" in word or "/" in word or "." in word):
                continue          # sentence opener, capitalised by grammar
            if _is_checkable(word) and not _supported(word, vocab):
                return word
    return None


_NOTE_RE = re.compile(r"^- `(?P<path>[^`]+)` @(?P<blob>\S+) : (?P<text>.*)$")


def parse(text):
    """Notes from the map block. Unreadable lines are skipped, never fatal:
    a hand-edited MEMORY.md must not cost a session its whole map."""
    start, end = text.find(BEGIN), text.find(END)
    if start == -1 or end == -1 or end < start:
        return []
    notes = []
    for line in text[start + len(BEGIN):end].splitlines():
        m = _NOTE_RE.match(line.strip())
        if m:
            notes.append({"path": m.group("path"), "blob": m.group("blob"),
                          "text": m.group("text"), "last_seen": 0.0})
    return notes


def update(text, notes):
    """`text` with the map block replaced. Everything outside the markers is
    returned byte-for-byte: above them is the operator's file."""
    lines = [BEGIN, PREAMBLE, ""]
    for n in sorted(notes, key=lambda x: x["path"]):
        lines.append("- `{}` @{} : {}".format(
            n["path"], n["blob"], (n["text"] or "").strip()[:MAX_NOTE_CHARS]))
    block = "\n".join(lines + [END])
    start, end = text.find(BEGIN), text.find(END)
    if start == -1 or end == -1 or end < start:
        sep = "" if not text or text.endswith("\n") else "\n"
        return text + sep + block + "\n"
    return text[:start] + block + text[end + len(END):]
