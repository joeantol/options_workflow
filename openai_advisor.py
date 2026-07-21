"""
openai_advisor.py

A second, independent roll/hold/assignment opinion via OpenAI's GPT-5.6 Luna
tier, for comparison against Claude's recommendation on the analyze page (now
the primary advisor — see claude_advisor.py) — not a replacement. Same
design as claude_advisor.py in every way that matters: deliberately does its
own PnL/premium arithmetic for NOTHING, treats given position data as
authoritative, never invents a price it wasn't handed.

Deliberately reuses claude_advisor.py's context-building functions (CORE
manual extraction/caching, weekly Plan/Review resolution, Fed calendar
fetch, chain-candidate filtering, position/unborn context formatting) rather
than duplicating them — none of that logic is Claude-specific, it's plain
text assembly, and having two copies drift would risk the two advisors
reasoning over subtly different facts. Same reasoning for reusing the exact
system prompts unchanged: identical rules (roll-direction verbatim labeling,
earnings-coverage note, date/weekday verification, formatting) between the
two advisors is what makes this a genuine second opinion rather than two
differently-instructed models.

Cost design: GPT-5.6's prompt caching is automatic (no explicit
cache_control needed) and keyed by matching the longest common prefix of the
input — hence the CORE manuals + weekly plan/review + Fed calendar are
concatenated into one static block placed BEFORE the per-position tail in a
single user message, so repeat calls within the ~30-minute cache window
reuse it at a fraction of the input price. See
https://developers.openai.com/api/docs/guides/prompt-caching.
"""

from __future__ import annotations

import os
import re

from claude_advisor import (
    _SYSTEM_PROMPT,
    _UNBORN_SYSTEM_PROMPT,
    _core_docs_text,
    _weekly_docs_text,
    _fed_calendar_text,
    build_chain_candidates_text,  # noqa: F401 — re-exported for callers that import it from here
    build_position_context,  # noqa: F401
    build_unborn_context,  # noqa: F401
)

_OPENAI_MODEL = "gpt-5.6-luna"
# Fixed cache-partition key — GPT-5.6's docs recommend setting this to
# improve cache hit rates for repeat requests sharing the same static prefix
# (our CORE/weekly/Fed block), rather than leaving cache routing to chance.
_PROMPT_CACHE_KEY = "options-workflow-openai-advisor"


def _build_cached_prefix() -> str:
    """The three static-ish layers (CORE manuals, weekly plan/review, Fed
    calendar) concatenated into one block — placed before the per-call tail
    so it forms a stable prefix for OpenAI's automatic prompt caching to
    match against. Mirrors claude_advisor._build_cached_content_block's
    three-layer structure, just flattened to plain text since GPT-5.6
    caching needs no explicit per-block cache_control markers."""
    core_text = _core_docs_text()
    weekly_text = _weekly_docs_text()
    fed_text = _fed_calendar_text()
    return (
        f"=== Core Strategy Manuals ===\n{core_text}\n\n"
        f"{weekly_text or '(no current-week Plan/Review found)'}\n\n"
        f"=== NY Fed Economic Indicators Calendar (this month) ===\n{fed_text}"
    )


def _get_client():
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return None
    from openai import OpenAI
    return OpenAI(api_key=api_key)


def _call_openai(system_prompt: str, tail_text: str, valid_recs: tuple[str, ...]) -> dict:
    """Shared OpenAI call: static CORE/weekly/Fed prefix plus an uncached
    tail, mirroring claude_advisor._call_claude's shape and return dict so
    callers can treat the two advisors interchangeably."""
    client = _get_client()
    if client is None:
        return {"error": "OPENAI_API_KEY not set", "recommendation": None, "text": ""}

    try:
        prefix = _build_cached_prefix()
    except Exception as exc:
        return {"error": f"doc extraction failed: {exc}", "recommendation": None, "text": ""}

    user_content = f"{prefix}\n\n{tail_text}"

    try:
        resp = client.chat.completions.create(
            model=_OPENAI_MODEL,
            max_completion_tokens=1500,
            prompt_cache_key=_PROMPT_CACHE_KEY,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        )
        text = (resp.choices[0].message.content or "").strip()
        if not text:
            return {"error": "empty response", "recommendation": None, "text": ""}
        rec_pattern = "|".join(valid_recs)
        # \*{0,2} tolerates the recommendation word itself being bolded
        # (**HOLD**) — same tolerance claude_advisor uses, since both
        # advisors share the same formatting instruction.
        m = re.search(rf"Recommendation:\s*\*{{0,2}}({rec_pattern})\*{{0,2}}", text, re.IGNORECASE)
        rec = m.group(1).upper() if m else None
        usage = resp.usage
        cached_tokens = None
        if usage is not None and getattr(usage, "prompt_tokens_details", None) is not None:
            cached_tokens = getattr(usage.prompt_tokens_details, "cached_tokens", None)
        return {
            "error": None,
            "recommendation": rec,
            "text": text,
            "cache_read_tokens": cached_tokens,
        }
    except Exception as exc:
        return {"error": str(exc), "recommendation": None, "text": ""}


def _ask_followup(
    system_prompt: str,
    original_tail_text: str,
    original_response_text: str,
    qa_thread: list[dict],
    question: str,
) -> dict:
    """Ask a follow-up question in the same conversation as an original
    query_openai_advisor/query_openai_unborn_advisor call. Reconstructs the
    full turn history the same way claude_advisor._ask_followup does, so the
    cached CORE/weekly/Fed prefix (identical to the original call's) still
    hits cache instead of a full-price rewrite."""
    client = _get_client()
    if client is None:
        return {"error": "OPENAI_API_KEY not set", "answer": ""}

    try:
        prefix = _build_cached_prefix()
    except Exception as exc:
        return {"error": f"doc extraction failed: {exc}", "answer": ""}

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"{prefix}\n\n{original_tail_text}"},
        {"role": "assistant", "content": original_response_text},
    ]
    for turn in qa_thread:
        messages.append({"role": "user", "content": turn.get("q", "")})
        messages.append({"role": "assistant", "content": turn.get("a", "")})
    messages.append({"role": "user", "content": question})

    try:
        resp = client.chat.completions.create(
            model=_OPENAI_MODEL,
            max_completion_tokens=1500,
            prompt_cache_key=_PROMPT_CACHE_KEY,
            messages=messages,
        )
        text = (resp.choices[0].message.content or "").strip()
        if not text:
            return {"error": "empty response", "answer": ""}
        return {"error": None, "answer": text}
    except Exception as exc:
        return {"error": str(exc), "answer": ""}


def ask_position_followup(
    original_tail_text: str, original_response_text: str, qa_thread: list[dict], question: str,
) -> dict:
    """Follow-up question against an existing-position Luna analysis (see
    query_openai_advisor) — same system prompt as Claude's, so it keeps
    ROLL/HOLD/ASSIGNMENT framing and the roll-direction/earnings-coverage
    rules identical between the two advisors."""
    return _ask_followup(_SYSTEM_PROMPT, original_tail_text, original_response_text, qa_thread, question)


def ask_unborn_followup(
    original_tail_text: str, original_response_text: str, qa_thread: list[dict], question: str,
) -> dict:
    """Follow-up question against an unborn/new-position Luna analysis (see
    query_openai_unborn_advisor) — same system prompt as Claude's, so it
    keeps SELL/WAIT framing."""
    return _ask_followup(_UNBORN_SYSTEM_PROMPT, original_tail_text, original_response_text, qa_thread, question)


def query_openai_advisor(position_context: str, chain_candidates_text: str | None = None) -> dict:
    """
    Ask Luna for a roll/hold/assignment recommendation on an EXISTING
    position. Returns {"recommendation": "ROLL"|"HOLD"|"ASSIGNMENT"|None,
    "text": str, "error": str|None, "tail_text": str} — tail_text is the
    exact uncached prompt tail used, worth storing so a later
    ask_position_followup() call can replay this same first turn.
    """
    tail_text = position_context
    if chain_candidates_text:
        tail_text += f"\n\n=== Live Candidate Strikes (same chain snapshot) ===\n{chain_candidates_text}"
    result = _call_openai(_SYSTEM_PROMPT, tail_text, ("ROLL", "HOLD", "ASSIGNMENT"))
    result["tail_text"] = tail_text
    return result


def query_openai_unborn_advisor(context: str, chain_candidates_text: str | None = None) -> dict:
    """
    Ask Luna whether to open a NEW covered-call/CSP position on a ticker
    with no existing position — the 'unborn'/former-position case. Returns
    {"recommendation": "SELL"|"WAIT"|None, "text": str, "error": str|None,
    "tail_text": str}.
    """
    tail_text = context
    if chain_candidates_text:
        tail_text += f"\n\n=== Live Candidate Strikes (same chain snapshot) ===\n{chain_candidates_text}"
    result = _call_openai(_UNBORN_SYSTEM_PROMPT, tail_text, ("SELL", "WAIT"))
    result["tail_text"] = tail_text
    return result
