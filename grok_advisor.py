"""
grok_advisor.py

A third, independent roll/hold/assignment opinion via xAI's Grok 4.1 Fast
(reasoning variant) — for comparison against Claude and Luna on the analyze
page. Replaces NotebookLM as the automatic third opinion (see
option_dashboard._THIRD_OPINION_PROVIDER) — query_notebooklm and its
upload/source-management machinery are left in place, untouched, so flipping
that one constant back is the entire revert.

Uses the reasoning variant, not the non-reasoning one originally shipped
here, after a confirmed-live miss: on a real LEU covered-call analysis, the
non-reasoning model correctly worked through delta/DTE/yield/liquidity/
earnings/calendar but silently dropped the cost-basis instruction in its
own prompt, recommending a strike that would lock in a loss on assignment
— something Claude and Luna both caught. Re-run on the identical prompt,
grok-4-1-fast-reasoning caught it too. The price difference is negligible:
both variants bill the same per-token rate, and since these prompts are
input-dominated (~72K tokens of CORE/weekly/Fed context vs. a few hundred
output tokens), the extra hidden reasoning tokens (~600-800, measured live)
add only ~3% to the per-call cost.

Same design as claude_advisor.py/openai_advisor.py in every way that
matters: deliberately does its own PnL/premium arithmetic for NOTHING, treats
given position data as authoritative, never invents a price it wasn't
handed.

Deliberately reuses claude_advisor.py's context-building functions (CORE
manual extraction/caching, weekly Plan/Review resolution, Fed calendar
fetch, chain-candidate filtering, position/unborn context formatting) and
exact system prompts, same reasoning as openai_advisor.py: unlike
NotebookLM, Grok has no uploaded notebook sources of its own to reason over
— it only ever sees what's in the prompt — so the full CORE/weekly/Fed
context has to be inlined here exactly as it is for Claude and Luna, not
just the trimmed "exact live figures" tail NotebookLM used to get on top of
its own uploaded sources.

Cost design: xAI's prompt caching is automatic (longest-common-prefix
match, like OpenAI's) — hence the same static CORE/weekly/Fed block placed
before the per-position tail in one user message, so repeat calls within
the cache window reuse it at a reduced rate. The x-grok-conv-id header is
xAI's documented way to improve cache hit rates for repeat requests sharing
that same static prefix. See
https://docs.x.ai/developers/advanced-api-usage/prompt-caching.
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

_GROK_MODEL = "grok-4-1-fast-reasoning"
_XAI_BASE_URL = "https://api.x.ai/v1"
# Fixed cache-partition key, sent as xAI's x-grok-conv-id header — improves
# cache hit rates for repeat requests sharing the same static prefix (our
# CORE/weekly/Fed block), rather than leaving cache routing to chance.
_PROMPT_CACHE_KEY = "options-workflow-grok-advisor"


def _build_cached_prefix() -> str:
    """The three static-ish layers (CORE manuals, weekly plan/review, Fed
    calendar) concatenated into one block — placed before the per-call tail
    so it forms a stable prefix for xAI's automatic prompt caching to match
    against. Mirrors openai_advisor._build_cached_prefix exactly (same
    layers, same order) so Claude/Luna/Grok are all reasoning over
    identical facts."""
    core_text = _core_docs_text()
    weekly_text = _weekly_docs_text()
    fed_text = _fed_calendar_text()
    return (
        f"=== Core Strategy Manuals ===\n{core_text}\n\n"
        f"{weekly_text or '(no current-week Plan/Review found)'}\n\n"
        f"=== NY Fed Economic Indicators Calendar (this month) ===\n{fed_text}"
    )


def _get_client():
    api_key = os.environ.get("XAI_API_KEY", "")
    if not api_key:
        return None
    from openai import OpenAI
    return OpenAI(api_key=api_key, base_url=_XAI_BASE_URL)


def _call_grok(system_prompt: str, tail_text: str, valid_recs: tuple[str, ...]) -> dict:
    """Shared Grok call: static CORE/weekly/Fed prefix plus an uncached
    tail, mirroring claude_advisor._call_claude's/openai_advisor._call_
    openai's shape and return dict so callers can treat all three advisors
    interchangeably."""
    client = _get_client()
    if client is None:
        return {"error": "XAI_API_KEY not set", "recommendation": None, "text": ""}

    try:
        prefix = _build_cached_prefix()
    except Exception as exc:
        return {"error": f"doc extraction failed: {exc}", "recommendation": None, "text": ""}

    user_content = f"{prefix}\n\n{tail_text}"

    try:
        resp = client.chat.completions.create(
            model=_GROK_MODEL,
            max_tokens=3000,
            # Deterministic — see claude_advisor._post_to_claude's matching
            # comment. Unlike Luna's GPT-5.6 model (which 400s on any
            # temperature other than its default of 1), grok-4-1-fast-
            # reasoning accepts 0 fine — confirmed live, despite also being
            # a reasoning-tier model.
            temperature=0,
            extra_headers={"x-grok-conv-id": _PROMPT_CACHE_KEY},
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
        # (**HOLD**) — same tolerance claude_advisor/openai_advisor use,
        # since all three advisors share the same formatting instruction.
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
    query_grok_advisor/query_grok_unborn_advisor call. Reconstructs the full
    turn history the same way claude_advisor._ask_followup/openai_advisor.
    _ask_followup do, so the cached CORE/weekly/Fed prefix (identical to the
    original call's) still hits cache instead of a full-price rewrite."""
    client = _get_client()
    if client is None:
        return {"error": "XAI_API_KEY not set", "answer": ""}

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
            model=_GROK_MODEL,
            max_tokens=3000,
            # Deterministic — see claude_advisor._post_to_claude's matching
            # comment. Unlike Luna's GPT-5.6 model (which 400s on any
            # temperature other than its default of 1), grok-4-1-fast-
            # reasoning accepts 0 fine — confirmed live, despite also being
            # a reasoning-tier model.
            temperature=0,
            extra_headers={"x-grok-conv-id": _PROMPT_CACHE_KEY},
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
    """Follow-up question against an existing-position Grok analysis (see
    query_grok_advisor) — same system prompt as Claude's/Luna's, so it keeps
    ROLL/HOLD/ASSIGNMENT framing and the roll-direction/earnings-coverage
    rules identical across all three advisors."""
    return _ask_followup(_SYSTEM_PROMPT, original_tail_text, original_response_text, qa_thread, question)


def ask_unborn_followup(
    original_tail_text: str, original_response_text: str, qa_thread: list[dict], question: str,
) -> dict:
    """Follow-up question against an unborn/new-position Grok analysis (see
    query_grok_unborn_advisor) — same system prompt as Claude's/Luna's, so
    it keeps SELL/WAIT framing."""
    return _ask_followup(_UNBORN_SYSTEM_PROMPT, original_tail_text, original_response_text, qa_thread, question)


def query_grok_advisor(tail_text: str) -> dict:
    """
    Ask Grok for a roll/hold/assignment recommendation on an EXISTING
    position. tail_text is the already-fully-assembled prompt tail (the
    exact same string Claude/Luna use — claude_advisor.build_position_
    context's output plus the chain-candidates block, or whatever the
    caller cached from an earlier query_claude_advisor/query_openai_advisor
    call) — Grok has no uploaded sources of its own, so unlike
    query_notebooklm it can't fall back on partial context. Returns
    {"recommendation": "ROLL"|"HOLD"|"ASSIGNMENT"|None, "text": str,
    "error": str|None, "tail_text": str} — tail_text is echoed back so a
    later ask_position_followup() call can replay this same first turn.
    """
    result = _call_grok(_SYSTEM_PROMPT, tail_text, ("ROLL", "HOLD", "ASSIGNMENT"))
    result["tail_text"] = tail_text
    return result


def query_grok_unborn_advisor(tail_text: str) -> dict:
    """
    Ask Grok whether to open a NEW covered-call/CSP position on a ticker
    with no existing position — the 'unborn'/former-position case. Same
    tail_text contract as query_grok_advisor. Returns {"recommendation":
    "SELL"|"WAIT"|None, "text": str, "error": str|None, "tail_text": str}.
    """
    result = _call_grok(_UNBORN_SYSTEM_PROMPT, tail_text, ("SELL", "WAIT"))
    result["tail_text"] = tail_text
    return result
