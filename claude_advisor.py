"""
claude_advisor.py

A second, independent roll/hold/assignment opinion via Claude Haiku, for
comparison against NotebookLM's recommendation on the analyze page — not a
replacement. Deliberately does its own PnL/premium arithmetic for NOTHING;
it's told to treat any numbers in the position data as authoritative, same
reasoning as why chain-PnL math elsewhere in this project is deterministic
Python, never LLM-computed.

Cost design: Anthropic prompt caching (1-hour TTL, refreshed for free on
every cache hit — NOT a fixed expiry from write time) means the bulk of the
context here — the 00_CORE strategy manuals (effectively static) and this
week's Plan/Review (rotates weekly) — is only paid for at full price once
per cache-cold call; every call within an hour of the last one reads it back
at ~10% of the input-token price. Only the per-position data at the end of
the prompt is ever uncached. This is why callers should batch calls together
(e.g. the scheduled refresh sweep) rather than spacing them out — a gap over
an hour forces a full-price rewrite of the cached layers.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

OPTIONS_DIR = Path("/Users/joeandbabs/work/retirement/options")
_CORE_CACHE_FILE = Path(__file__).parent / ".core_docs_cache.json"
_WEEKLY_CACHE_FILE = Path(__file__).parent / ".weekly_docs_cache.json"
_FED_CACHE_FILE = Path(__file__).parent / ".fed_calendar_cache.json"

# Same URL source already uploaded to the NotebookLM notebook (verified via
# the notebooklm client's sources.list()) — mirrored here rather than picking
# a different Fed calendar, so both advisors are reasoning over the same
# economic-events source, not two different ones that could disagree on dates.
_FED_CALENDAR_URL = "https://www.newyorkfed.org/research/calendars/nationalecon_cal"

_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"


def _extract_pdf_text(path: Path) -> str:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _core_docs_text() -> str:
    """
    Extracted text of all 00_CORE_*.pdf strategy manuals, cached to disk keyed
    by each file's mtime so this only re-extracts when a doc actually changes
    — they're effectively static (dated Mar-Apr, rarely touched), so in
    practice this almost never re-runs the (slow, PDF-parsing) extraction.
    """
    core_files = sorted(OPTIONS_DIR.glob("00_CORE_*.pdf"))
    fingerprint = {f.name: f.stat().st_mtime for f in core_files}

    cached: dict = {}
    if _CORE_CACHE_FILE.exists():
        try:
            cached = json.loads(_CORE_CACHE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            cached = {}

    if cached.get("_fingerprint") == fingerprint and cached.get("text"):
        return cached["text"]

    parts = []
    for f in core_files:
        title = f.stem.replace("00_CORE_", "").replace("_", " ").title()
        parts.append(f"=== {title} ===\n{_extract_pdf_text(f)}")
    text = "\n\n".join(parts)

    try:
        _CORE_CACHE_FILE.write_text(json.dumps({"_fingerprint": fingerprint, "text": text}))
    except OSError:
        pass
    return text


_WEEK_FILE_PAT = re.compile(r"(PLAN|REVIEW)_.*_Week_(\d+)\.(pdf|csv)$")


def _current_week_files() -> dict[str, Path]:
    """The most recent PLAN (csv preferred, pdf fallback) and most recent
    REVIEW pdf, keyed 'PLAN_csv'/'PLAN_pdf'/'REVIEW_pdf'. PLAN and REVIEW are
    found independently, NOT required to share a week number: REVIEW_N covers
    the week that just ended (published at the start of week N+1, alongside
    PLAN_N+1), so the latest of each is normally one week number apart —
    bucketing them together would silently drop the review every week."""
    by_kind_week: dict[str, dict[int, Path]] = {}
    for f in OPTIONS_DIR.iterdir():
        m = _WEEK_FILE_PAT.match(f.name)
        if not m:
            continue
        kind, week, ext = m.group(1), int(m.group(2)), m.group(3)
        by_kind_week.setdefault(f"{kind}_{ext}", {})[week] = f

    result: dict[str, Path] = {}
    for key, weeks in by_kind_week.items():
        result[key] = weeks[max(weeks)]
    return result


def _week_num_of(f: Path) -> str:
    m = re.search(r"Week_(\d+)", f.name)
    return m.group(1) if m else "?"


def _weekly_docs_text() -> str:
    """
    Text for the latest Plan (CSV — compact structured table, much cheaper
    than parsing the PDF — pdf fallback otherwise) and latest Review (PDF —
    no CSV equivalent). These are normally a week apart (REVIEW_N covers the
    week PLAN_N already superseded). Cached to disk keyed by the exact pair
    of week numbers in use, so it re-extracts only when either one rotates,
    not on every call.
    """
    files = _current_week_files()
    if not files:
        return ""
    plan_csv = files.get("PLAN_csv")
    plan_pdf = files.get("PLAN_pdf")
    plan_file = plan_csv or plan_pdf
    review_pdf = files.get("REVIEW_pdf")
    plan_week = _week_num_of(plan_file) if plan_file else None
    review_week = _week_num_of(review_pdf) if review_pdf else None
    cache_key = f"plan{plan_week}_review{review_week}"

    cached: dict = {}
    if _WEEKLY_CACHE_FILE.exists():
        try:
            cached = json.loads(_WEEKLY_CACHE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            cached = {}
    if cached.get("_key") == cache_key and cached.get("text"):
        return cached["text"]

    parts = []
    if plan_csv:
        parts.append(
            f"=== Current Management Plan (Week {plan_week}) ===\n"
            + plan_csv.read_text(errors="ignore")
        )
    elif plan_pdf:
        parts.append(
            f"=== Current Management Plan (Week {plan_week}) ===\n"
            + _extract_pdf_text(plan_pdf)
        )
    if review_pdf:
        parts.append(
            f"=== Most Recent Weekly Review (Week {review_week}) ===\n"
            + _extract_pdf_text(review_pdf)
        )
    text = "\n\n".join(parts)

    try:
        _WEEKLY_CACHE_FILE.write_text(json.dumps({"_key": cache_key, "text": text}))
    except OSError:
        pass
    return text


def fed_month_url(d) -> str:
    """URL for a specific month's NY Fed calendar page, e.g. i-aug26.html.
    Shared with option_dashboard.py's NotebookLM-source management, so both
    advisors resolve the same month URLs the same way."""
    return f"https://www.newyorkfed.org/research/calendars/i-{d.strftime('%b').lower()}{d.strftime('%y')}.html"


def _fetch_fed_page_text(url: str) -> str:
    import requests
    from bs4 import BeautifulSoup
    resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    lines = [l.strip() for l in soup.get_text(separator="\n").split("\n") if l.strip()]
    return "\n".join(lines)


def _fed_calendar_text() -> str:
    """
    NY Fed's Economic Indicators Calendar — current month AND next month, so
    a 1-week-ahead view is always covered even when today is near month-end
    (the calendar page only ever shows one month; late in the month, "next 7
    days" spills into the next one). Same source already uploaded to the
    NotebookLM notebook (see option_dashboard.py's _ensure_fed_calendar_source,
    which keeps a matching next-month source there too). Cached by date, so
    this refetches at most once per day — the page itself says "dates and
    times are tentative and subject to immediate change", so daily is a
    reasonable freshness bar without refetching on every call.
    """
    import datetime
    today_d = datetime.date.today()
    today = today_d.isoformat()
    cached: dict = {}
    if _FED_CACHE_FILE.exists():
        try:
            cached = json.loads(_FED_CACHE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            cached = {}
    if cached.get("_date") == today and cached.get("text"):
        return cached["text"]

    next_month_d = (today_d.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    urls = [
        ("This month", fed_month_url(today_d)),
        ("Next month", fed_month_url(next_month_d)),
    ]
    parts = []
    for label, url in urls:
        try:
            parts.append(f"[{label}]\n{_fetch_fed_page_text(url)}")
        except Exception as exc:
            parts.append(f"[{label}] fetch failed: {exc}")

    text = "\n\n".join(parts)
    if not any("fetch failed" not in p for p in parts):
        # Both fetches failed — fall back to yesterday's cache rather than
        # sending Claude a page of error text.
        return cached.get("text") or text

    try:
        _FED_CACHE_FILE.write_text(json.dumps({"_date": today, "text": text}))
    except OSError:
        pass
    return text


_SYSTEM_PROMPT = (
    "You are a second opinion on an options covered-call/CSP roll-or-hold "
    "decision, for comparison against another advisor's recommendation on "
    "the same position — you are not the sole decision-maker. You are given: "
    "(1) the trader's core strategy manuals, (2) this week's management plan "
    "and last week's review, (3) the NY Fed's Economic Indicators Calendar "
    "for this month (CPI, FOMC-adjacent releases, employment data, etc. — "
    "the same source the other advisor uses), (4) live data for one specific "
    "position, and (5) sometimes a list of real candidate strikes/expiries "
    "from the current option chain. Using ONLY these sources and the rules "
    "they establish, decide ROLL, HOLD, or ASSIGNMENT for this position — "
    "factor in whether a major economic release falls within the position's "
    "remaining DTE the same way you'd factor in an earnings date.\n\n"
    "Assignment mechanics — do not get this backwards: a short PUT is "
    "assigned if the underlying closes BELOW the strike at expiry; a short "
    "CALL is assigned if the underlying closes ABOVE the strike. Always check "
    "the position's option type before describing what triggers assignment.\n\n"
    "Do not compute your own PnL, premium, or yield figures — treat every "
    "number given in the position data as authoritative; if you reference a "
    "number, use one that was given to you. If you recommend ROLL and a "
    "candidate-strikes list was provided, name a specific strike/expiry from "
    "that list (with its real mid-price) as your suggested STO leg — this is "
    "real chain data, not a guess. If no candidate list was provided, "
    "describe the target in terms of the rules (delta range, DTE window) "
    "instead of inventing a specific live price you were not given.\n\n"
    "Roll direction — if a candidate list was provided, each row's LAST "
    "column (roll_direction_vs_current) already tells you whether that "
    "specific candidate is OUT (more time), IN (less time), or SAME versus "
    "the current position — this was computed for you from the actual "
    "dates precisely because 'roll out' is easy to reuse as a stock phrase "
    "from the manuals without checking it against a nearer-dated candidate. "
    "Use that column's label verbatim in your prose (say 'roll in' or "
    "'shorten duration', never 'roll out', for any candidate marked IN) — "
    "do not independently judge or override it, and do not say 'roll out' "
    "for the overall recommendation if the candidate you actually chose is "
    "marked IN.\n\n"
    "Dates and time — you are given the current date, weekday, and time; do "
    "not compute or assume a weekday for any other date yourself, and do "
    "not invent a specific calendar deadline (e.g. 'by EOD Friday, [date]') "
    "unless you are certain both the date and its weekday are correct. This "
    "also applies to date RANGES — never pair a weekday range with a "
    "calendar-date range (e.g. 'Mon-Fri (Jul 13-19)') unless every date in "
    "the range is verified against its correct weekday; these are just as "
    "easy to get wrong as a single deadline (e.g. pairing Friday with a "
    "date that is actually a Sunday). Prefer relative phrasing like 'within "
    "1-2 trading days' or 'the rest of this week' over naming a specific "
    "date+weekday combination or date range you have not verified. Start "
    "your reply's very first line with 'It is [weekday], [date] @ [time].' "
    "using exactly the date/time you were given, before the "
    "'Recommendation: ...' line — this lets the trader confirm at a glance "
    "that you have the correct current date/time.\n\n"
    "Formatting: use **bold** (double asterisks) for the key conclusion and "
    "specific figures/strikes, *italics* (single asterisks) for caveats or "
    "secondary asides, and '- ' for bullet points, where it improves "
    "readability — don't force it into every sentence. After the 'It is "
    "[weekday]...' line, the SECOND line must read exactly "
    "'Recommendation: ROLL' (or HOLD, or ASSIGNMENT), then explain your "
    "reasoning against the manuals and this week's plan in a few short "
    "paragraphs."
)


_CANDIDATE_DELTA_RANGE = (0.10, 0.45)
_CANDIDATE_DTE_RANGE = (14, 60)
_CANDIDATE_MAX_ROWS = 60


def build_chain_candidates_text(
    all_rows: list[dict], cur_type: str, current_expiry: str | None = None,
) -> str | None:
    """
    Compact candidate-strike text from chain rows ALREADY fetched for the
    NotebookLM upload (all_rows, from run_roll_for_position) — no separate
    Public.com call. Filtered to a sensible delta/DTE window (not the full
    chain, which can be 1000+ rows for heavily-optioned tickers like MSFT)
    so this stays cheap; same instrument type as the current leg only, since
    rolls essentially never change call<->put.

    current_expiry, when given, adds a deterministic OUT/IN/SAME column —
    computed here rather than left for Claude to reason about, after a real
    case where a candidate expiring EARLIER than the current position got
    called "roll out" anyway (the manuals' stock phrase "roll out and down"
    pattern-matched regardless of the actual date comparison).
    """
    import datetime
    today = datetime.date.today()
    lo_delta, hi_delta = _CANDIDATE_DELTA_RANGE
    lo_dte, hi_dte = _CANDIDATE_DTE_RANGE
    try:
        current_expiry_d = datetime.date.fromisoformat(current_expiry) if current_expiry else None
    except (TypeError, ValueError):
        current_expiry_d = None

    candidates = []
    for r in all_rows:
        if str(r.get("option_type", "")).upper() != cur_type.upper():
            continue
        try:
            delta = abs(float(r.get("delta") or 0))
            exp = datetime.date.fromisoformat(r.get("expiration_date"))
            dte = (exp - today).days
            mid = float(r.get("mid_price") or r.get("last") or 0)
        except (TypeError, ValueError):
            continue
        if not (lo_delta <= delta <= hi_delta and lo_dte <= dte <= hi_dte and mid > 0):
            continue
        direction = ""
        if current_expiry_d:
            if exp > current_expiry_d:
                direction = "OUT(more time)"
            elif exp < current_expiry_d:
                direction = "IN(less time)"
            else:
                direction = "SAME"
        candidates.append({
            "strike": r.get("strike_price"), "expiry": r.get("expiration_date"),
            "dte": dte, "delta": round(delta, 3), "mid": mid, "direction": direction,
        })
    if not candidates:
        return None

    candidates.sort(key=lambda c: (c["dte"], abs(c["delta"] - 0.30)))
    header = "strike,expiry,dte,delta,mid_price"
    if current_expiry_d:
        header += ",roll_direction_vs_current"
    lines = [f"{header} ({cur_type} candidates from the live chain)"]
    for c in candidates[:_CANDIDATE_MAX_ROWS]:
        row = f"{c['strike']},{c['expiry']},{c['dte']},{c['delta']},{c['mid']}"
        if current_expiry_d:
            row += f",{c['direction']}"
        lines.append(row)
    return "\n".join(lines)


def _build_cached_content_block(tail_text: str) -> list[dict]:
    """The three cached layers (CORE manuals, weekly plan/review, Fed
    calendar) plus an uncached tail, as a single user-turn content list.
    Shared by the initial call and follow-up questions — reusing the exact
    same cached prefix is what makes follow-ups cheap (cache hit) instead of
    a full-price rewrite."""
    core_text = _core_docs_text()
    weekly_text = _weekly_docs_text()
    fed_text = _fed_calendar_text()
    return [
        {
            "type": "text",
            "text": f"=== Core Strategy Manuals ===\n{core_text}",
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        },
        {
            "type": "text",
            "text": weekly_text or "(no current-week Plan/Review found)",
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        },
        {
            "type": "text",
            "text": f"=== NY Fed Economic Indicators Calendar (this month) ===\n{fed_text}",
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        },
        {
            # Anthropic rejects a text content block with an empty string
            # (confirmed: reproduces the exact "400 Bad Request" a user hit
            # on a follow-up whose cached tail_text had gone missing — e.g.
            # a stale cache entry from before this field existed). Never let
            # an empty/falsy tail_text reach the API silently; a follow-up
            # against a position with no real tail_text is a genuine
            # inconsistency worth surfacing, not masking.
            "type": "text",
            "text": tail_text or "(no position context available for this follow-up)",
        },
    ]


def _call_claude(system_prompt: str, tail_text: str, valid_recs: tuple[str, ...]) -> dict:
    """Shared Claude call: three cached layers (CORE manuals, weekly plan/
    review, Fed economic calendar) plus an uncached tail. Used by both the
    existing-position (ROLL/HOLD/ASSIGNMENT) and unborn/new-position
    (SELL/WAIT) advisors below — same caching economics, different
    vocabulary and system prompt."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY not set", "recommendation": None, "text": ""}

    import requests
    try:
        content_block = _build_cached_content_block(tail_text)
    except Exception as exc:
        return {"error": f"doc extraction failed: {exc}", "recommendation": None, "text": ""}

    messages = [{"role": "user", "content": content_block}]

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": _ANTHROPIC_MODEL,
                "max_tokens": 1500,
                "system": system_prompt,
                "messages": messages,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data.get("content", [])
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
        if not text:
            return {"error": "empty response", "recommendation": None, "text": ""}
        rec_pattern = "|".join(valid_recs)
        # \*{0,2} tolerates the recommendation word itself being bolded
        # (**HOLD**) — the formatting instruction below asks Claude to bold
        # "the key conclusion", which sometimes means this exact word.
        m = re.search(rf"Recommendation:\s*\*{{0,2}}({rec_pattern})\*{{0,2}}", text, re.IGNORECASE)
        rec = m.group(1).upper() if m else None
        usage = data.get("usage", {})
        return {
            "error": None,
            "recommendation": rec,
            "text": text,
            "cache_read_tokens": usage.get("cache_read_input_tokens"),
            "cache_write_tokens": usage.get("cache_creation_input_tokens"),
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
    """
    Ask a follow-up question in the same conversation as an original
    query_claude_advisor/query_claude_unborn_advisor call. Reconstructs the
    full turn history — original context+question, original answer, each
    prior Q&A pair, then the new question — so the cached CORE/weekly/Fed
    layers (identical to the original call's) hit cache instead of a
    full-price rewrite, and Claude has its own prior reasoning as context
    for follow-ups ("what if I used the 30 strike instead?" etc.).

    qa_thread: [{"q": str, "a": str}, ...] — prior follow-ups in this thread,
    oldest first. Returns {"answer": str, "error": str|None}.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY not set", "answer": ""}

    import requests
    try:
        content_block = _build_cached_content_block(original_tail_text)
    except Exception as exc:
        return {"error": f"doc extraction failed: {exc}", "answer": ""}

    messages = [
        {"role": "user", "content": content_block},
        {"role": "assistant", "content": original_response_text},
    ]
    for turn in qa_thread:
        messages.append({"role": "user", "content": turn.get("q", "")})
        messages.append({"role": "assistant", "content": turn.get("a", "")})
    messages.append({"role": "user", "content": question})

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": _ANTHROPIC_MODEL,
                "max_tokens": 1500,
                "system": system_prompt,
                "messages": messages,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data.get("content", [])
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
        if not text:
            return {"error": "empty response", "answer": ""}
        return {"error": None, "answer": text}
    except Exception as exc:
        return {"error": str(exc), "answer": ""}


def ask_position_followup(
    original_tail_text: str, original_response_text: str, qa_thread: list[dict], question: str,
) -> dict:
    """Follow-up question against an existing-position Claude analysis (see
    query_claude_advisor) — same system prompt, so it keeps ROLL/HOLD/
    ASSIGNMENT framing and the roll-direction/earnings-coverage rules."""
    return _ask_followup(_SYSTEM_PROMPT, original_tail_text, original_response_text, qa_thread, question)


def ask_unborn_followup(
    original_tail_text: str, original_response_text: str, qa_thread: list[dict], question: str,
) -> dict:
    """Follow-up question against an unborn/new-position Claude analysis (see
    query_claude_unborn_advisor) — same system prompt, so it keeps SELL/WAIT
    framing."""
    return _ask_followup(_UNBORN_SYSTEM_PROMPT, original_tail_text, original_response_text, qa_thread, question)


def query_claude_advisor(position_context: str, chain_candidates_text: str | None = None) -> dict:
    """
    Ask Claude (Haiku) for a roll/hold/assignment recommendation on an
    EXISTING position. Returns {"recommendation": "ROLL"|"HOLD"|"ASSIGNMENT"
    |None, "text": str, "error": str|None, "tail_text": str} — tail_text is
    the exact uncached prompt tail used, worth storing so a later
    ask_claude_followup() call can replay this same first turn.
    """
    tail_text = position_context
    if chain_candidates_text:
        tail_text += f"\n\n=== Live Candidate Strikes (same chain snapshot) ===\n{chain_candidates_text}"
    result = _call_claude(_SYSTEM_PROMPT, tail_text, ("ROLL", "HOLD", "ASSIGNMENT"))
    result["tail_text"] = tail_text
    return result


_UNBORN_SYSTEM_PROMPT = (
    "You are a second opinion on whether to open a NEW covered call or "
    "cash-secured put position — there is no existing position yet, so this "
    "is purely about entering one. This is for comparison against another "
    "advisor's recommendation on the same ticker — you are not the sole "
    "decision-maker. You are given: (1) the trader's core strategy manuals, "
    "(2) this week's management plan and last week's review, (3) the NY "
    "Fed's Economic Indicators Calendar for this month (CPI, FOMC-adjacent "
    "releases, employment data, etc. — the same source the other advisor "
    "uses), (4) live data for the ticker and intended strategy, and (5) "
    "usually a list of real candidate strikes/expiries from the current "
    "option chain. Using ONLY these sources and the rules they establish, "
    "decide SELL or WAIT: SELL if a specific candidate meets the manuals' "
    "criteria (delta range, DTE window, yield vs. T-bill hurdle, liquidity) "
    "well enough to act now — and clears the event calendar (no major "
    "economic release inside the DTE window, same as the earnings-blackout "
    "rule); WAIT if nothing qualifies yet or conditions call for patience.\n\n"
    "Assignment mechanics — do not get this backwards: a short PUT is "
    "assigned if the underlying closes BELOW the strike at expiry; a short "
    "CALL is assigned if the underlying closes ABOVE the strike.\n\n"
    "Do not compute your own PnL, premium, or yield figures — treat every "
    "number given as authoritative. If you recommend SELL, name a specific "
    "strike/expiry from the candidate list (with its real mid-price and "
    "delta) — this is real chain data, not a guess. Never invent a price you "
    "were not given.\n\n"
    "Dates and time — you are given the current date, weekday, and time; do "
    "not compute or assume a weekday for any other date yourself, and do "
    "not invent a specific calendar deadline (e.g. 'by EOD Friday, [date]') "
    "unless you are certain both the date and its weekday are correct. This "
    "also applies to date RANGES — never pair a weekday range with a "
    "calendar-date range (e.g. 'Mon-Fri (Jul 13-19)') unless every date in "
    "the range is verified against its correct weekday; these are just as "
    "easy to get wrong as a single deadline (e.g. pairing Friday with a "
    "date that is actually a Sunday). Prefer relative phrasing like 'within "
    "1-2 trading days' or 'the rest of this week' over naming a specific "
    "date+weekday combination or date range you have not verified. Start "
    "your reply's very first line with 'It is [weekday], [date] @ [time].' "
    "using exactly the date/time you were given — this lets the trader "
    "confirm at a glance that you have the correct current date/time.\n\n"
    "Formatting: use **bold** (double asterisks) for the key conclusion and "
    "specific figures/strikes, *italics* (single asterisks) for caveats or "
    "secondary asides, and '- ' for bullet points, where it improves "
    "readability. After the 'It is [weekday]...' line, the SECOND line must "
    "read exactly 'Recommendation: SELL' (or WAIT), then explain your "
    "reasoning against the manuals and this week's plan in a few short "
    "paragraphs."
)


def query_claude_unborn_advisor(context: str, chain_candidates_text: str | None = None) -> dict:
    """
    Ask Claude (Haiku) whether to open a NEW covered-call/CSP position on a
    ticker with no existing position — the 'unborn'/former-position case.
    Returns {"recommendation": "SELL"|"WAIT"|None, "text": str, "error":
    str|None, "tail_text": str} — tail_text is the exact uncached prompt
    tail used, worth storing so a later ask_claude_followup() call can
    replay this same first turn.
    """
    tail_text = context
    if chain_candidates_text:
        tail_text += f"\n\n=== Live Candidate Strikes (same chain snapshot) ===\n{chain_candidates_text}"
    result = _call_claude(_UNBORN_SYSTEM_PROMPT, tail_text, ("SELL", "WAIT"))
    result["tail_text"] = tail_text
    return result


def _key_dates_lines(key_dates: dict | None) -> list[str]:
    """Format get_key_dates()'s output (earnings/ex-div, yfinance-backed) into
    prompt lines — shared by both context builders below. Source (confirmed
    vs. estimated) is passed through so Claude can weight it appropriately,
    same distinction the dashboard UI already shows the user."""
    if not key_dates:
        return ["Next earnings date: unknown", "Next ex-dividend date: unknown"]
    e_date = key_dates.get("earnings_date", "Unknown")
    e_src  = key_dates.get("earnings_source", "unknown")
    x_date = key_dates.get("exdiv_date", "Unknown")
    x_src  = key_dates.get("exdiv_source", "unknown")
    return [
        f"Next earnings date: {e_date} ({e_src})",
        f"Next ex-dividend date: {x_date} ({x_src})",
    ]


def _earnings_coverage_note(expiry: str | None, key_dates: dict | None) -> str:
    """
    Deterministic check for a real reasoning error caught in practice: a
    position whose CURRENT expiry is already past the next earnings date
    doesn't gain anything earnings-wise from rolling to ANY candidate that's
    also past that date — you're already holding through earnings either
    way. Left to free-text reasoning, this got missed (a roll to a NEARER
    date, still after earnings, was justified as "getting past earnings" —
    true of the position it already was). Compute it once here instead of
    trusting prose to notice.
    """
    import datetime
    if not expiry or not key_dates:
        return ""
    e_date = key_dates.get("earnings_date")
    if not e_date or str(e_date).lower() in ("unknown", "n/a", ""):
        return ""
    try:
        expiry_d = datetime.date.fromisoformat(expiry)
        earnings_d = datetime.date.fromisoformat(str(e_date))
    except (TypeError, ValueError):
        return ""
    if expiry_d >= earnings_d:
        return (
            f"IMPORTANT: the CURRENT position's expiry ({expiry}) is already on or "
            f"after the next earnings date ({e_date}) — you are already holding "
            f"through that earnings event regardless of any roll. 'Rolling past "
            f"earnings' is NOT a valid reason to roll to a different expiry unless "
            f"the candidate is BEFORE the earnings date (closing the position "
            f"ahead of it) — do not cite earnings-avoidance as a rationale for "
            f"rolling to another expiry that is also on or after {e_date}."
        )
    return f"Note: the current position's expiry ({expiry}) is BEFORE the next earnings date ({e_date}) — it does not currently hold through earnings."


def _cost_basis_line(is_call: bool, ul_cost_basis: float | None) -> str:
    """
    Cost basis matters only for calls (covered-call strikes should sit at or
    above cost basis to avoid a loss on assignment) — never for puts, where no
    shares are owned yet. Mirrors query_notebooklm's cost_basis_clause in
    option_dashboard.py so both advisors get the same treatment: a missing/
    zero cost basis is flagged as a gap rather than silently read as "$0",
    which would otherwise make every share look like pure profit.
    """
    if not is_call:
        return "Underlying cost basis: not applicable (put — no underlying shares owned)."
    cb = ul_cost_basis or 0
    if cb > 0:
        return (
            f"Underlying cost basis: ${cb:.2f} per share — factor this in: avoid "
            f"recommending a roll/hold/strike that risks assignment below this "
            f"level (a loss on the shares)."
        )
    return (
        "Underlying cost basis: not available (0 or unset) — flag this gap "
        "rather than assuming the shares have a $0 cost basis."
    )


def build_unborn_context(ticker: str, strat: str, ul_price: float | None,
                          ul_cost_basis: float | None, vix: float | None,
                          atr: float | None = None, key_dates: dict | None = None) -> str:
    """Compact plain-text summary for the unborn (no existing position) case —
    the uncached tail of the prompt."""
    import datetime

    def _fmt(v, digits=2):
        return "unknown" if v is None else f"{v:.{digits}f}"

    strategy = "Covered Call (CC)" if strat == "CC" else "Cash-Secured Put (CSP)"
    lines = [
        f"Today's date: {datetime.date.today().isoformat()} ({datetime.date.today().strftime('%A')})",
        f"Current time: {datetime.datetime.now().strftime('%-I:%M %p ET')}",
        f"VIX: {_fmt(vix)}",
        "",
        f"Ticker: {ticker}",
        f"Intended strategy: {strategy}",
        f"Current underlying price: {_fmt(ul_price)}",
        _cost_basis_line(strat == "CC", ul_cost_basis),
        f"14-day ATR: {_fmt(atr)}"
        + (f"  (1.5x ATR buffer: {atr * 1.5:.2f})" if atr else ""),
        *_key_dates_lines(key_dates),
        "No existing option position on this ticker — this is a decision to open one, not manage one.",
    ]
    return "\n".join(lines)


def build_position_context(pos: dict, vix: float | None, key_dates: dict | None = None) -> str:
    """Compact plain-text summary of one position's live data/Greeks — the
    uncached tail of the prompt. Mirrors the fields already computed by
    get_eval_data() so this needs no separate market-data fetch (ATR/buffer
    are already on `pos`; only earnings/ex-div need a fresh yfinance call,
    passed in via key_dates)."""
    import datetime

    def _fmt(v, digits=3):
        return "unknown" if v is None else f"{v:.{digits}f}"

    atr = pos.get("atr")
    buffer_ = pos.get("buffer")
    lines = [
        f"Today's date: {datetime.date.today().isoformat()} ({datetime.date.today().strftime('%A')})",
        f"Current time: {datetime.datetime.now().strftime('%-I:%M %p ET')}",
        f"VIX: {_fmt(vix, 2)}",
        "",
        f"Position: {abs(int(pos.get('net_qty') or 0))}x "
        f"{pos.get('symbol')} {pos.get('strike')} {str(pos.get('option_type','')).upper()} "
        f"exp {pos.get('expiry')} ({'short' if (pos.get('net_qty') or 0) > 0 else 'long'})",
        f"Assignment for this leg happens if the underlying closes "
        f"{'BELOW' if str(pos.get('option_type','')).lower() == 'put' else 'ABOVE'} "
        f"the {pos.get('strike')} strike at expiry.",
        f"Days to expiration: {pos.get('dte')}",
        f"Underlying price: {_fmt(pos.get('underlying'), 2)}",
        _cost_basis_line(str(pos.get('option_type', '')).lower() == 'call', pos.get('ul_cost_basis')),
        f"14-day ATR: {_fmt(atr, 2)}"
        + (f"  (1.5x ATR buffer: {buffer_:.2f})" if buffer_ else ""),
        *_key_dates_lines(key_dates),
        _earnings_coverage_note(pos.get("expiry"), key_dates),
        f"Current option price: {_fmt(pos.get('current_price'), 2)}",
        f"Average entry price (this leg): {_fmt(pos.get('avg_price'), 2)}",
        f"P&L on this leg: {_fmt(pos.get('pct_pnl'), 1)}%",
        f"Delta: {_fmt(pos.get('delta'))}",
        f"Gamma: {_fmt(pos.get('gamma'), 4)}",
        f"Theta (per contract/day): {_fmt(pos.get('opt_theta'), 4)}",
        f"Implied volatility: {_fmt(pos.get('current_iv'), 3)}",
        f"Dividend amount (quarterly, if any): {_fmt(pos.get('dividend_amount'), 2)}",
        f"Net chain cash collected to date (this roll chain): {_fmt(pos.get('chain_cash'), 2)}",
        f"Flagged conditions: {'; '.join(pos.get('reasons') or []) or 'none'}",
    ]
    return "\n".join(lines)
