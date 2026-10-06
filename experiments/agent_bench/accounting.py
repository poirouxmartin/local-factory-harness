"""What one turn's prompt costs, line item by line item.

Pure functions, no I/O: the tokenizer arrives as a callable so this module is
testable without a GPU, and the counters arrive as plain dicts.

The rule: never estimate. `chat_context.estimate` is chars/ratio and exists to
budget transport conservatively; it is not a measurement. Counts here come
from the server's own tokenizer, and what the blocks fail to explain is
reported as `overhead` rather than absorbed -- the chat template's role
wrappers and its own tool serialisation are real tokens, and pretending the
sum is exact would hide them.
"""
import json


def _message_text(m):
    """What a message really carries: content plus tool_call arguments. An
    agent write_file puts the whole file in the arguments (2026-07-14)."""
    text = m.get("content") or ""
    if m.get("tool_calls"):
        text += json.dumps(m["tool_calls"], ensure_ascii=False)
    return text


def segments(system_parts, tools, summary, messages):
    """The prompt in wire order as [(name, text)].

    `tools` is priced from its JSON: the template serialises schemas its own
    way, so this is an approximation, and `reconcile` is what keeps it honest.
    """
    segs = list(system_parts)
    segs.append(("tools", json.dumps(tools, ensure_ascii=False) if tools else ""))
    if summary:
        segs.append(("summary", summary.get("content") or ""))
    if messages:
        segs.append(("messages",
                     "\n".join(_message_text(m) for m in messages)))
    return segs


def last_call_messages(messages):
    """The history as the LAST model call of the turn saw it.

    The bench prices the session once the turn is over, but the final answer
    is appended after that call built its prompt. Charging it would make the
    blocks exceed the wire and report a negative template overhead -- a
    decomposition bigger than what was sent is a bug, not a finding. An
    assistant message that still carries a tool call was in the prompt: only
    the trailing answers are dropped.
    """
    end = len(messages)
    while end and messages[end - 1].get("role") == "assistant" \
            and not messages[end - 1].get("tool_calls"):
        end -= 1
    return messages[:end]


def count(segments_, tokenize):
    """[(name, tokens)] -- `tokenize` is str -> int, injected."""
    return [(name, tokenize(text)) for name, text in segments_]


def reconcile(counted, prompt_count):
    """The blocks against the wire. `overhead` is what the chat template adds
    and the blocks cannot explain; it is a number to look at, not to bury."""
    blocks = sum(n for _, n in counted)
    wire = prompt_count or 0
    overhead = wire - blocks
    return {"blocks": blocks, "wire": wire, "overhead": overhead,
            "overhead_pct": int(100 * overhead / wire) if wire else 0}


def cache_report(metrics):
    """What this turn repaid versus what the KV prefix gave back for free.
    `prompt_count` is prompt_n + cache_n; `prefill_count` is prompt_n alone
    (llama_client.py:107-111)."""
    wire = metrics.get("prompt_count") or 0
    repaid = metrics.get("prefill_count") or 0
    reused = wire - repaid
    return {"wire": wire, "repaid": repaid, "reused": reused,
            "hit_pct": int(100 * reused / wire) if wire else 0}
