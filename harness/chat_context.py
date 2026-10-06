"""Prompt-side context management for chat/agent sessions (spec 2026-07-12).

Three layers, cheapest first, all at prompt-build time -- the session file on
disk always keeps the full record: (1) stub old tool outputs, (2) replace the
oldest span with a stored summary, (3) trim to the budget and report what was
dropped. Ollama must never be the one truncating: it does so silently.

Pure functions, no I/O. Token counts are estimates (stdlib only, no
tokenizer): chars / ratio, where ratio is calibrated per session from the
previous turn's prompt_eval_count.
"""
import json

# Room for the reply; thinking models run long. 8192 was half the jobs lane's
# cap against a measured 35b median of 6492 tokens on a module-scale contract
# (banc A, 2026-07-25), so a third of its turns were being cut -- and a cut
# turn used to lose its tool calls outright. At CHAT_NUM_CTX 65536 this still
# leaves 48k of prompt, so the guarantee costs nothing that matters.
OUTPUT_RESERVE = 16384
COMPACT_THRESHOLD = 0.7    # compaction fires above this share of the budget
VERBATIM_TAIL = 6          # newest messages are always sent as-is
EVICT_KEEP = 12            # tool outputs within the last N messages survive
DEFAULT_RATIO = 3.0        # chars per token, conservative for French + code
_MSG_OVERHEAD = 4          # rough per-message token cost of role/formatting

# Names the harness as the author and states the recovery. The old text --
# "[résultat d'outil effacé]" -- said neither, and the model read its own
# elided history as the USER withholding information: sessions audited on
# 2026-07-27 contain the model complaining it was "given truncated context"
# while re-reading the same file 29 times to rebuild what it thought was
# missing. What is elided is recoverable; it has to say so.
EVICTED_STUB = ("[sortie d'outil élaguée par le harnais pour tenir dans le "
                "contexte -- ce n'est pas une omission de l'utilisateur ; "
                "relance l'outil si tu en as besoin]")

DROPPED_NOTE = ("[{n} message(s) plus ancien(s) ne tiennent pas dans cette "
                "fenêtre et ont été retirés par le harnais -- ce n'est pas "
                "une omission de l'utilisateur. Le transcript complet est sur "
                "disque ; relance un outil si tu as besoin d'un fait perdu.]")

SUMMARY_PROMPT = """Tu résumes une conversation pour qu'elle puisse continuer \
avec moins de contexte. Réponds UNIQUEMENT avec le résumé, dans la langue de \
la conversation, structuré ainsi :
Objectif : ce que l'utilisateur veut obtenir.
Décisions : les choix actés et leurs raisons.
Faits : informations précises à retenir (noms, chemins, valeurs, préférences).
État : où en est le travail.
Suite : la prochaine étape attendue."""


def ratio(session):
    """Chars-per-token from the last turn's real prefill_count, clamped:
    a wild estimate must degrade to caution, not to a broken budget."""
    cal = session.get("calibration") or {}
    chars, tokens = cal.get("chars", 0), cal.get("tokens", 0)
    if chars and tokens:
        return min(5.0, max(2.0, chars / tokens))
    return DEFAULT_RATIO


def prompt_budget(num_ctx):
    """Tokens available for the prompt; the floor keeps small windows usable."""
    return max(num_ctx - OUTPUT_RESERVE, num_ctx // 4)


def wire_chars(m):
    """Chars a message really costs on the wire: content plus tool_calls.
    An agent write_file carries the whole file in its arguments; counting it
    as zero is how the 2026-07-14 session filled 65536 tokens without ever
    triggering compaction."""
    n = len(m.get("content") or "")
    if m.get("tool_calls"):
        n += len(json.dumps(m["tool_calls"], ensure_ascii=False))
    return n


def estimate(messages, r):
    """Token estimate for a list of wire messages, or a bare string."""
    if isinstance(messages, str):
        return int(len(messages) / r)
    return sum(_MSG_OVERHEAD + int(wire_chars(m) / r) for m in messages)


def _strip_calls(calls):
    return [dict(c, function=dict(c.get("function") or {}, arguments={}))
            for c in calls]


def evict_tool_outputs(messages, keep_last=EVICT_KEEP):
    """Copy of `messages` with tool traffic outside the last `keep_last`
    stubbed: tool outputs are replaced by a stub, tool_call arguments are
    emptied (the name stays). Old tool exchanges are bulky and perishable:
    the assistant message that reacted to them carries the conclusion.
    Returns (copy, count)."""
    cut = max(0, len(messages) - keep_last)
    out, evicted = [], 0
    for i, m in enumerate(messages):
        if i < cut and m.get("role") == "tool":
            m = dict(m, content=EVICTED_STUB)
            evicted += 1
        elif i < cut and m.get("tool_calls"):
            m = dict(m, tool_calls=_strip_calls(m["tool_calls"]))
            evicted += 1
        out.append(m)
    return out, evicted


def _live(messages, summary):
    return messages[summary["covers_until"]:] if summary else list(messages)


def summary_message(summary):
    return {"role": "system",
            "content": "Résumé de la conversation antérieure :\n"
                       + summary["content"]}


def needs_compaction(messages, summary, budget, r, force=False):
    """True when the live span crowds the window. Measured on the real
    history, NOT on the evicted copy: eviction is a transport optimisation
    that stubs old tool output, and in an agent session that IS the bulk, so
    measuring after it made the trigger unreachable (session c_1a722191:
    425 messages, zero compactions, the model left with ~2k tokens of stubs).
    What eviction drops is exactly what a summary has to preserve.
    `force` means the previous turn's REAL token counters overflowed: the
    estimate is overruled, but there must still be a span to summarize."""
    live = _live(messages, summary)
    if len(live) <= VERBATIM_TAIL:
        return False
    return force or estimate(live, r) > COMPACT_THRESHOLD * budget


def compaction_span(messages, summary):
    """(messages to summarize, new covers_until): everything not already
    covered, minus the verbatim tail. Indexes are into `messages`."""
    start = summary["covers_until"] if summary else 0
    covers_until = max(start, len(messages) - VERBATIM_TAIL)
    return messages[start:covers_until], covers_until


def build_prompt(system_msgs, summary, messages, budget, r):
    """Assemble what is actually sent: system + summary + the newest live
    messages that fit. Messages covered by the summary are never sent. The
    newest message goes out even over budget -- sending nothing is worse."""
    live, evicted = evict_tool_outputs(_live(messages, summary))
    base = list(system_msgs)
    if summary:
        base.append(summary_message(summary))
    left = budget - estimate(base, r)
    tail, dropped = [], 0
    for i in range(len(live) - 1, -1, -1):
        cost = estimate([live[i]], r)
        if left - cost < 0 and tail:
            dropped = i + 1
            break
        tail.insert(0, live[i])
        left -= cost
    if dropped:
        # Said out loud, for the same reason as EVICTED_STUB: a span that just
        # vanishes reads as the user withholding it, and the model sets out to
        # rebuild what it believes it was denied.
        base = base + [{"role": "system", "content": DROPPED_NOTE.format(
            n=dropped)}]
    sent = base + tail
    return sent, {"evicted": evicted, "dropped": dropped,
                  "used_summary": bool(summary),
                  "est_tokens": estimate(sent, r)}
