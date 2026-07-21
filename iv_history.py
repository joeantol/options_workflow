"""
iv_history.py

Self-collected daily IV history per underlying, for computing IV Rank once
enough data has accumulated. No free source of historical per-stock implied
volatility exists (confirmed: Public.com has no historical endpoints;
yfinance only gives a live chain snapshot) — so this starts a local time
series today rather than trying to backfill, same "no synthetic backfill,
just start collecting" call made for the entry-snapshot Greeks feature.

One sample per ticker per day (first call each day wins; later calls that
day are no-ops) — cheap enough to call from every get_eval_data() run
without worrying about write volume from frequent polling.

Not yet wired into NB/Claude's prompts — there's no meaningful history yet.
Once IV_RANK_MIN_DAYS worth of samples exist for a ticker, get_iv_rank()
starts returning real numbers; wire that into build_position_context /
build_unborn_context / the NB CSV upload once it's actually usable.
"""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path

_HISTORY_FILE = Path(__file__).parent / ".iv_history.json"

# Below this many days of samples, a "rank" is too noisy to act on — callers
# should treat get_iv_rank()'s return as informational-only until then.
IV_RANK_MIN_DAYS = 20
IV_RANK_WINDOW_DAYS = 252  # ~1 trading year


def _load() -> dict[str, list[dict]]:
    try:
        with open(_HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(data: dict[str, list[dict]]) -> None:
    try:
        with open(_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def record_iv_snapshot(ticker: str, iv: float | None, on_date: str | None = None) -> None:
    """Record today's IV reading for `ticker`, if one hasn't been recorded yet
    today. Silently no-ops on bad input — this must never disrupt the caller
    (get_eval_data, called on every refresh/poll)."""
    if not ticker or iv is None or iv <= 0:
        return
    ticker = ticker.upper()
    today = on_date or datetime.date.today().isoformat()

    data = _load()
    series = data.setdefault(ticker, [])
    if series and series[-1].get("date") == today:
        return  # already have today's sample
    series.append({"date": today, "iv": round(float(iv), 4)})
    # Trim to the window we'd ever use, so the file doesn't grow forever
    if len(series) > IV_RANK_WINDOW_DAYS:
        data[ticker] = series[-IV_RANK_WINDOW_DAYS:]
    _save(data)


VIX_TICKER = "VIX"


def ensure_vix_backfilled() -> None:
    """
    One-time backfill of VIX history from yfinance's ^VIX ticker. Unlike
    per-stock implied volatility, VIX has abundant free historical data, so
    there's no need to wait weeks for a meaningful rank — this fills the same
    storage record_iv_snapshot/get_iv_rank already use, keyed "VIX", so the
    normal daily-snapshot path (record_iv_snapshot("VIX", ...)) just keeps it
    current from here on. Safe to call on every startup — no-ops once VIX
    already has history.
    """
    data = _load()
    if data.get(VIX_TICKER):
        return  # already backfilled (or already collecting) — don't overwrite
    try:
        import yfinance as yf
        hist = yf.Ticker(f"^{VIX_TICKER}").history(period=f"{IV_RANK_WINDOW_DAYS}d")
        if hist is None or hist.empty:
            return
        series = [
            {"date": idx.strftime("%Y-%m-%d"), "iv": round(float(close), 4)}
            for idx, close in hist["Close"].items()
        ]
        data[VIX_TICKER] = series[-IV_RANK_WINDOW_DAYS:]
        _save(data)
    except Exception:
        pass  # best-effort — daily snapshots will start a fresh series if this fails


def get_iv_rank(ticker: str, current_iv: float | None = None) -> dict | None:
    """
    IV Rank = where current_iv sits between the trailing window's low and
    high, as a 0-100 percentage. Uses whatever history exists (up to
    IV_RANK_WINDOW_DAYS) — returns None if there's no history at all, and
    flags `sufficient: False` (still returns a value) below IV_RANK_MIN_DAYS
    samples so callers can label it as preliminary rather than hide it.
    """
    ticker = ticker.upper()
    data = _load()
    series = data.get(ticker, [])
    if not series:
        return None

    ivs = [s["iv"] for s in series]
    if current_iv is not None and current_iv > 0:
        ivs = ivs + [current_iv]  # include today's live reading even if not yet persisted
    lo, hi = min(ivs), max(ivs)
    latest = current_iv if current_iv is not None else ivs[-1]
    rank = 50.0 if hi == lo else round((latest - lo) / (hi - lo) * 100, 1)

    return {
        "iv_rank": rank,
        "n_days": len(series),
        "window_days": IV_RANK_WINDOW_DAYS,
        "sufficient": len(series) >= IV_RANK_MIN_DAYS,
        "low": lo,
        "high": hi,
    }
