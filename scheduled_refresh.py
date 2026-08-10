#!/Users/joeandbabs/public-env/bin/python
"""
scheduled_refresh.py

Runs at 10:30 and 14:30 ET on weekdays (via cron on the iMac).
Fires against the running option_dashboard.py server on localhost:5051.

Three passes:
  1. Unborn tickers already in the dashboard's unborn_rows.json display
     → clear unborn cache entry, re-trigger /api/unborn
  2. Flagged positions (ATR breach, delta breach, DTE warning, etc.)
     → clear analysis cache entry, re-trigger /api/analyze
  3. Remaining open positions (not flagged, not in unborn display)
     → clear unborn cache entry, trigger /api/unborn
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import time

import requests

BASE_URL = "http://localhost:5051"
SCRIPT_DIR = Path(__file__).parent
UNBORN_ROWS_FILE = SCRIPT_DIR / "unborn_rows.json"
LOG_FILE = SCRIPT_DIR / "logs" / "scheduled_refresh.log"
HTTP_TIMEOUT = 360  # seconds per individual HTTP call (LLM analysis can take 60-180s+)
# Must exceed the dashboard's own outer per-analysis timeout (700s, see
# option_dashboard.py's _run_analysis closures) or this gives up polling
# while the background thread is still legitimately working — the analysis
# still completes and gets cached, just after this script already logged a
# false-negative "Poll timeout".
POLL_MAX    = 750   # seconds total before giving up on a single analysis

# ── Logging ──────────────────────────────────────────────────────────────────
os.makedirs(LOG_FILE.parent, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
    ],
)
log = logging.getLogger("scheduled_refresh")


def _strike_str(strike) -> str:
    """Match JS String(p.strike) — drop trailing .0 for whole numbers."""
    try:
        f = float(strike)
        return str(int(f)) if f == int(f) else str(f)
    except (TypeError, ValueError):
        return str(strike or "")


def build_pos_key(pos: dict) -> str:
    return "|".join([
        pos["symbol"].upper(),
        pos["option_type"].lower(),
        _strike_str(pos["strike"]),
        str(pos.get("expiry") or ""),
    ])


def _post(url: str, payload: dict) -> int:
    """POST JSON, return HTTP status code (0 on connection error)."""
    try:
        r = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
        return r.status_code
    except Exception as exc:
        log.error("POST %s failed: %s", url, exc)
        return 0


def _post_and_poll(url: str, payload: dict) -> int:
    """POST JSON and poll on 202 until done, mirroring the browser polling loop."""
    _, status = _post_and_poll_result(url, payload)
    return status


def _post_and_poll_result(url: str, payload: dict) -> tuple[dict, int]:
    """POST JSON, poll on 202, and return (response_json, status_code)."""
    deadline = time.time() + POLL_MAX
    attempts = 0
    while True:
        try:
            r = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
        except requests.exceptions.Timeout as exc:
            attempts += 1
            if attempts <= 1:
                log.warning("POST %s timed out, retrying once …", url)
                continue
            log.error("POST %s timed out after retry: %s", url, exc)
            return {}, 0
        except Exception as exc:
            log.error("POST %s failed: %s", url, exc)
            return {}, 0
        if r.status_code == 202:
            try:
                body = r.json()
                retry_after = body.get("retry_after", 5)
            except Exception:
                retry_after = 5
            if time.time() + retry_after > deadline:
                log.warning("Poll timeout after %ds for %s", POLL_MAX, url)
                return {}, 202
            log.info("    in_progress, retrying in %ds …", retry_after)
            time.sleep(retry_after)
            continue
        try:
            return r.json(), r.status_code
        except Exception:
            return {}, r.status_code


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skipunborn", action="store_true", help="Skip Pass 1 (unborn rows)")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("Scheduled refresh starting")

    # ── 0. Self-heal the NB next-month Fed calendar source on month rollover ──
    try:
        r = requests.post(f"{BASE_URL}/api/ensure-fed-source", timeout=30)
        log.info("POST /api/ensure-fed-source -> HTTP %d", r.status_code)
    except Exception as exc:
        log.warning("ensure-fed-source failed: %s", exc)

    # ── 1. Fetch all current positions ───────────────────────────────────────
    log.info("GET %s/api/eval ...", BASE_URL)
    try:
        resp = requests.get(f"{BASE_URL}/api/eval", timeout=120)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.error("Cannot reach dashboard: %s", exc)
        log.error("Is option_dashboard.py running on port 5051?")
        sys.exit(1)

    positions = data.get("positions", [])
    log.info(
        "Fetched %d positions (%d flagged)",
        len(positions), data.get("flagged_count", 0),
    )

    # ── 2. Load unborn_rows.json ─────────────────────────────────────────────
    unborn_rows: dict = {}
    if UNBORN_ROWS_FILE.exists():
        with open(UNBORN_ROWS_FILE) as f:
            unborn_rows = json.load(f)
    unborn_tickers = {v["symbol"].upper() for v in unborn_rows.values()}
    log.info(
        "Unborn display: %d rows, tickers: %s",
        len(unborn_rows), ", ".join(sorted(unborn_tickers)) or "(none)",
    )

    # ── Pass 1: Re-run unborn for displayed unborn rows ──────────────────────
    import datetime as _dt
    if args.skipunborn:
        log.info("--- Pass 1: skipped (--skipunborn) ---")
    else:
        log.info("--- Pass 1: unborn display rows (%d) ---", len(unborn_rows))
        updated_unborn = dict(unborn_rows)  # copy; we'll update in-place and save at end
        for row_key, row in unborn_rows.items():
            ticker = row["symbol"].upper()
            strat = row_key.split("|")[1] if "|" in row_key else "CC"
            if strat not in ("CC", "CSP"):
                strat = "CC"
            qty = int(row.get("_qty") or 1)
            cb  = float(row.get("_ul_cost_basis") or 0)
            log.info("  %s  strat=%s qty=%d cb=%.2f", ticker, strat, qty, cb)
            result, status = _post_and_poll_result(
                f"{BASE_URL}/api/unborn",
                {"ticker": ticker, "qty": qty, "strat": strat, "cost_basis": cb, "force": True},
            )
            log.info("    → /api/unborn HTTP %d", status)
            # display_chain (when present) reflects the highest-priority
            # SELL/WAIT across Claude/Luna/NotebookLM, not just Claude's own
            # — see run_unborn_for_ticker's docstring in option_dashboard.py.
            # This write runs AFTER the server's own _persist_unborn_display_row
            # (which already prefers display_chain), so using the plain
            # Claude-only "chain" here would silently clobber it back.
            chain = result.get("display_chain") or result.get("chain") or []
            if chain:
                now_et = _dt.datetime.now(_dt.timezone.utc).astimezone(
                    _dt.timezone(_dt.timedelta(hours=-4))  # ET (EDT)
                )
                run_at = now_et.strftime("%-m/%-d %-I:%M %p ET")
                updated_unborn[row_key] = dict(chain[0], **{
                    "_qty": qty,
                    "_ul_cost_basis": row.get("_ul_cost_basis"),
                    "_ubKey": row.get("_ubKey"),
                    "_run_at": run_at,
                })
                log.info("    updated display row for %s → %s", ticker, run_at)
        # Persist updated display rows so the browser picks them up on next refresh
        try:
            resp = requests.post(
                f"{BASE_URL}/api/unborn-rows",
                json=updated_unborn,
                timeout=HTTP_TIMEOUT,
            )
            log.info("Saved updated unborn rows → HTTP %d", resp.status_code)
        except Exception as exc:
            log.error("Failed to save unborn rows: %s", exc)

    # ── Pass 2: Re-analyze flagged (warning) positions ────────────────────────
    # /api/analyze now runs Claude, then Luna, then NotebookLM inline in
    # sequence itself (see run_roll_for_position's docstring) — no separate
    # back-to-back /api/openai-compare call needed here anymore; that used
    # to be how Luna ran at all, now it'd just be a redundant second Luna
    # call on top of the one /api/analyze already made.
    flagged = [p for p in positions if p.get("flagged") and not p.get("dont_analyze")]
    skipped_dont_analyze = [p for p in positions if p.get("flagged") and p.get("dont_analyze")]
    if skipped_dont_analyze:
        log.info(
            "  Skipping %d flagged position(s) marked don't-analyze: %s",
            len(skipped_dont_analyze),
            ", ".join(build_pos_key(p) for p in skipped_dont_analyze),
        )
    log.info("--- Pass 2: flagged positions (%d) ---", len(flagged))
    for pos in flagged:
        pk = build_pos_key(pos)
        reasons = "; ".join(pos.get("reasons", []))
        log.info("  %s  [%s]", pk, reasons)
        _post(f"{BASE_URL}/api/reset-cache-entry", {"position_key": pk})
        status = _post_and_poll(f"{BASE_URL}/api/analyze", {"position_key": pk})
        log.info("    → /api/analyze HTTP %d", status)

    # ── Pass 3: Run unborn for remaining open positions ──────────────────────
    seen: set[str] = set()
    remaining = [
        p for p in positions
        if not p.get("flagged") and p["symbol"].upper() not in unborn_tickers
    ]
    log.info("--- Pass 3: remaining positions (%d unique tickers) ---",
             len({p["symbol"].upper() for p in remaining}))
    for pos in remaining:
        ticker = pos["symbol"].upper()
        if ticker in seen:
            continue
        seen.add(ticker)
        opt_type = pos.get("option_type", "CALL").upper()
        strat    = "CC" if opt_type == "CALL" else "CSP"
        qty      = abs(int(pos.get("net_qty") or 1))
        cb       = float(pos.get("ul_cost_basis") or 0)
        log.info("  %s  strat=%s qty=%d cb=%.2f", ticker, strat, qty, cb)
        status = _post(
            f"{BASE_URL}/api/unborn",
            {"ticker": ticker, "qty": qty, "strat": strat, "cost_basis": cb, "force": True},
        )
        log.info("    → /api/unborn HTTP %d", status)

    log.info("Scheduled refresh complete.")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
