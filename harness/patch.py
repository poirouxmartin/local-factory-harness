"""Extract file writes from a model reply.

The model asks for files to be written. It is the least trusted party in the
system, so the spec's target list is an allowlist and every path is re-checked
for escapes -- even one the spec itself named.
"""
import difflib
import re
import unicodedata

from factory_config import is_unsafe_path

_HEADER = re.compile(r"^#{1,6}\s*FILE:\s*(.+?)\s*$", re.M)
_FENCE = re.compile(r"```(?:python|py|typescript|tsx?|javascript|jsx?|mjs|cjs)?\s*\n(.*?)```", re.S)
# First fence to the LAST closing fence. A file that itself contains ``` (a
# fence-matching regex, a markdown sample...) is shredded by the non-greedy
# pattern -- job j_bc119315 truncated loop.py at its extract_code line, ten
# attempts running. The greedy span is the only candidate that survives; the
# compile() check below is what tells it apart from a merge of unrelated blocks.
_FENCE_GREEDY = re.compile(r"```(?:python|py|typescript|tsx?|javascript|jsx?|mjs|cjs)?\s*\n(.*)```", re.S)


class PatchError(Exception):
    pass


class NoCode(PatchError):
    pass


class UnknownTarget(PatchError):
    pass


class AnchorNotFound(PatchError):
    pass


class AmbiguousAnchor(PatchError):
    pass


class BrokenResult(PatchError):
    pass


def _best_block(text, path):
    """The largest candidate that parses as Python, or None if text has no fence.

    Candidates still holding a stray ``` never compile, so size order plus the
    syntax gate picks the intact file. Known residual: a tiny valid block (a
    usage snippet) can win only when every larger candidate is broken -- the
    spec suite then fails the attempt, as it did before this gate existed.
    """
    candidates = _FENCE.findall(text)
    greedy = _FENCE_GREEDY.search(text)
    if greedy:
        candidates.append(greedy.group(1))
    if not candidates:
        return None
    candidates = sorted({c.strip() + "\n" for c in candidates}, key=len, reverse=True)
    if not path.endswith(".py"):
        return candidates[0]
    for code in candidates:
        try:
            compile(code, path, "exec")
            return code
        except SyntaxError:
            continue
    raise NoCode("no code block in the reply parses as valid Python for {!r}; "
                 "return the COMPLETE file in one block".format(path))


# ---- SEARCH/REPLACE edit mode (spec 2026-07-17) ----
#
# Whole-file re-emission starves the thinking budget on large targets
# (j_c32e89fd: ~7-9k output tokens of code per attempt; the 35b died
# mid-deliberation without emitting a line). Diff mode asks for anchored
# edits instead. Anchors are exact on purpose: the model must reason about
# a file state it can predict, and a failed anchor produces feedback it
# can act on.

_SR_BLOCK = re.compile(
    r"^<<<<<<<+ SEARCH\s*\n(.*?)^=======\s*\n(.*?)^>>>>>>>+ REPLACE\s*$",
    re.M | re.S)


def extract_edits(reply, target_files):
    reply = reply.replace("\r\n", "\n")
    sections = []
    headers = list(_HEADER.finditer(reply))
    if not headers:
        if len(target_files) != 1:
            raise NoCode("{} targets but no `### FILE:` headers to tell them "
                         "apart".format(len(target_files)))
        sections.append((target_files[0], reply))
    else:
        for i, m in enumerate(headers):
            end = headers[i + 1].start() if i + 1 < len(headers) else len(reply)
            path = m.group(1).strip().strip("`")
            if is_unsafe_path(path) or path not in target_files:
                raise UnknownTarget("model tried to edit {!r}, which is not a "
                                    "target".format(path))
            sections.append((path, reply[m.end():end]))
    edits = {}
    for path, body in sections:
        for search, replace in _SR_BLOCK.findall(body):
            edits.setdefault(path, []).append((search, replace))
    if not edits:
        raise NoCode("no SEARCH/REPLACE block in the reply; emit edits as\n"
                     "<<<<<<< SEARCH\\n<exact current lines>\\n=======\\n"
                     "<replacement>\\n>>>>>>> REPLACE")
    return edits


def _closest_region(text, search):
    """A few file lines around the best fuzzy match for the anchor's first
    line -- the feedback that turns 'not found' into a copy-paste fix."""
    probe = next((l for l in search.splitlines() if l.strip()), "").strip()
    lines = text.splitlines()
    if not probe or not lines:
        return ""
    best = max(range(len(lines)), key=lambda i: difflib.SequenceMatcher(
        None, probe, lines[i].strip()).ratio())
    return "\n".join(lines[max(0, best - 2):best + 3])


# Tiered anchor matching (audit 2026-07-17): exact anchors lost every edit on
# the crgpd rig -- the 30b "fixes" accents and whitespace while copying, drift
# the model cannot see in its own reply. Exact stays first; a looser tier only
# fires when every stricter one found nothing, so exact behavior never
# regresses. The tier that matched is recorded for the attempt log.

def _fold_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


# The exact tier is substring-based (an anchor may start mid-line); the
# looser tiers are whole-line, because "modulo whitespace/accents" is only
# well-defined line by line.
_TIERS = (
    ("whitespace", lambda l: l.rstrip()),
    ("indent", lambda l: l.strip()),
    ("unicode", lambda l: _fold_accents(l.strip())),
)


def _reindent(replace, search_lines, matched_lines):
    """Shift the replacement by the indent delta of the first non-blank
    matched pair, so an indent-tier match does not splice mis-indented code."""
    for s, f in zip(search_lines, matched_lines):
        if s.strip():
            s_ind = s[:len(s) - len(s.lstrip())]
            f_ind = f[:len(f) - len(f.lstrip())]
            break
    else:
        return replace
    if s_ind == f_ind:
        return replace
    out = []
    for line in replace.split("\n"):
        if line.startswith(s_ind) and line.strip():
            line = f_ind + line[len(s_ind):]
        out.append(line)
    return "\n".join(out)


def _apply_one(text, search, replace, path, tier_log):
    n = text.count(search)
    if n > 1:
        raise AmbiguousAnchor(
            "SEARCH text appears {} times in {!r}; add surrounding "
            "lines until it is unique".format(n, path))
    if n == 1:
        tier_log.append("exact")
        return text.replace(search, replace, 1)
    if search.endswith("\n") and text.endswith(search[:-1]):
        # The file's last line has no trailing newline; the anchor is
        # still exact.
        tier_log.append("exact")
        return text[:len(text) - len(search) + 1] + replace

    file_ke = text.splitlines(True)
    file_lines = [l.rstrip("\r\n") for l in file_ke]
    search_lines = search.splitlines()
    k = len(search_lines)
    for tier, norm in _TIERS:
        ns = [norm(l) for l in search_lines]
        if not any(ns):
            continue  # a whitespace-only anchor matched loosely means nothing
        nf = [norm(l) for l in file_lines]
        locs = [i for i in range(len(nf) - k + 1) if nf[i:i + k] == ns]
        if len(locs) > 1:
            raise AmbiguousAnchor(
                "SEARCH text appears {} times in {!r} ({} match); add "
                "surrounding lines until it is unique".format(
                    len(locs), path, tier))
        if locs:
            i = locs[0]
            if tier in ("indent", "unicode"):
                replace = _reindent(replace, search_lines, file_lines[i:i + k])
            start = sum(map(len, file_ke[:i]))
            end = start + sum(map(len, file_ke[i:i + k]))
            tier_log.append(tier)
            return text[:start] + replace + text[end:]
    # The one near-miss worth naming: the anchor IS in the file, but its last
    # line stops mid-line, so accepting it would splice `} else {` down to `}`.
    # Refusing is right; saying only "closest region" is not -- the 35b made
    # the identical truncation twice running on j_bcf15a3c and lost the case.
    body = search.rstrip("\n")
    if body and body in text:
        rest = text[text.index(body) + len(body):].split("\n")[0]
        if rest:
            raise AnchorNotFound(
                "SEARCH text not found in {!r}: your last SEARCH line is "
                "incomplete. You wrote {!r} but the file line continues -- it "
                "reads {!r}. Copy that line in full.".format(
                    path, body.split("\n")[-1], body.split("\n")[-1] + rest))
    raise AnchorNotFound(
        "SEARCH text not found in {!r}. Closest region:\n{}\n"
        "Copy the region EXACTLY as it appears in the current "
        "file.".format(path, _closest_region(text, search)))


def apply_edits(current, edits, tier_log=None):
    """All-or-nothing: any bad anchor raises before a single file is returned,
    so a retry always reasons about the state it was shown.

    tier_log, when given, receives one tier name per applied edit, in order.
    """
    out = {}
    tier_log = [] if tier_log is None else tier_log
    for path, blocks in edits.items():
        text = current.get(path, "")
        for search, replace in blocks:
            if search == "":
                if text.strip():
                    raise AnchorNotFound(
                        "an empty SEARCH creates a new file, but {!r} already "
                        "has content; copy the region you want to change "
                        "EXACTLY".format(path))
                text = replace
                tier_log.append("exact")
                continue
            text = _apply_one(text, search, replace, path, tier_log)
        if path.endswith(".py"):
            try:
                compile(text, path, "exec")
            except SyntaxError as e:
                raise BrokenResult(
                    "after applying your edits {!r} is not valid Python "
                    "({}); re-check the edits".format(path, e))
        out[path] = text
    return out


def extract_files(reply, target_files):
    headers = list(_HEADER.finditer(reply))
    if not headers:
        if len(target_files) != 1 and _FENCE.search(reply):
            raise NoCode("{} targets but no `### FILE:` headers to tell them apart".format(
                len(target_files)))
        block = _best_block(reply, target_files[0])
        if block is None:
            raise NoCode("reply contained no code block")
        return {target_files[0]: block}

    files = {}
    for i, m in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(reply)
        path = m.group(1).strip().strip("`")
        if is_unsafe_path(path) or path not in target_files:
            raise UnknownTarget("model tried to write {!r}, which is not a target".format(path))
        block = _best_block(reply[m.end():end], path)
        if block is None:
            continue
        files[path] = block
    if not files:
        raise NoCode("every `### FILE:` header was missing its code block")
    return files
