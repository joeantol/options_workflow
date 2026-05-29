#!/usr/bin/env python3
"""
get_option_chain.py

Fetches option chains from the Public.com API and saves them to a CSV file.
Optionally uploads the CSV to a Google NotebookLM notebook.

Usage:
    python get_option_chain.py --ticker IBM [--num 10] [--upload]

Requirements:
    - Set the PUBLIC_API_SECRET environment variable to your Public.com Secret Token.
      Generate one at: https://public.com (Settings → API)
    - pip install requests

    For --upload:
    - pip install "notebooklm-py[browser]" && playwright install chromium
    - Run once: notebooklm login   (opens browser for Google sign-in)
    - Set the NOTEBOOKLM_NOTEBOOK_ID environment variable to your notebook ID.

The script will:
    1. Exchange your Secret Token for a short-lived Access Token.
    2. Look up your account ID from the Public.com API.
    3. Retrieve up to `num` expiration dates for the given ticker.
    4. Fetch the full option chain (calls + puts) for each expiration.
    5. Write all data to {TICKER}.csv in the current directory.
    6. (If --upload) Upload the CSV as a source to your NotebookLM notebook.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime
import os
import re
import sys
import textwrap


BASE_URL = "https://api.public.com"
AUTH_URL = "https://api.public.com/userapiauthservice/personal/access-tokens"

# Journal DB lives one level up from the script's directory
JOURNAL_DB = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "option_trade_journal", "trades.db"
)

# CSV columns — one row per contract (call or put)
CSV_COLUMNS = [
    "option_type",        # CALL or PUT
    "expiration_date",
    "symbol",
    "strike_price",
    "last",
    "last_timestamp",
    "bid",
    "bid_size",
    "ask",
    "ask_size",
    "mid_price",
    "volume",
    "open_interest",
    "previous_close",
    "day_change",
    "day_change_pct",
    "delta",
    "gamma",
    "theta",
    "vega",
    "rho",
    "implied_volatility",
]


def get_access_token(secret: str, validity_minutes: int = 60) -> str:
    import requests
    """Exchange a long-lived Secret Token for a short-lived Access Token (JWT)."""
    response = requests.post(
        AUTH_URL,
        headers={"Content-Type": "application/json"},
        json={"secret": secret, "validityInMinutes": validity_minutes},
    )
    if response.status_code == 401:
        print(
            "ERROR: Unauthorized. Your PUBLIC_API_SECRET is invalid or revoked.\n"
            "  Generate a Secret Token at: https://public.com/settings (API section)",
            file=sys.stderr,
        )
        sys.exit(1)
    if response.status_code == 429:
        print("ERROR: Rate limited by Public.com auth endpoint. Try again shortly.", file=sys.stderr)
        sys.exit(1)
    if response.status_code != 200:
        print(
            f"ERROR: Failed to obtain access token — HTTP {response.status_code}: {response.text}",
            file=sys.stderr,
        )
        sys.exit(1)
    access_token = response.json().get("accessToken")
    if not access_token:
        print("ERROR: No accessToken in auth response.", file=sys.stderr)
        sys.exit(1)
    print(f"Access token obtained (valid {validity_minutes} min).")
    return access_token


def get_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def get_account_id(token: str) -> str:
    """Retrieve the first brokerage account ID for the authenticated user."""
    import requests
    url = f"{BASE_URL}/userapigateway/trading/account"
    response = requests.get(url, headers=get_headers(token))
    if response.status_code == 401:
        print("ERROR: Unauthorized. Check your PUBLIC_API_TOKEN.", file=sys.stderr)
        sys.exit(1)
    if response.status_code != 200:
        print(f"ERROR: Failed to fetch accounts — HTTP {response.status_code}: {response.text}", file=sys.stderr)
        sys.exit(1)
    data = response.json()
    accounts = data.get("accounts", [])
    if not accounts:
        print("ERROR: No accounts found for this token.", file=sys.stderr)
        sys.exit(1)
    account_id = accounts[0]["accountId"]
    print(f"Using account ID: {account_id}")
    return account_id


def get_expirations(token: str, account_id: str, ticker: str) -> list[str]:
    """Retrieve all available option expiration dates for the given ticker."""
    import requests
    url = f"{BASE_URL}/userapigateway/marketdata/{account_id}/option-expirations"
    payload = {
        "instrument": {
            "symbol": ticker.upper(),
            "type": "EQUITY",
        }
    }
    response = requests.post(url, headers=get_headers(token), json=payload)
    if response.status_code != 200:
        print(
            f"ERROR: Failed to fetch expirations for {ticker} — HTTP {response.status_code}: {response.text}",
            file=sys.stderr,
        )
        sys.exit(1)
    data = response.json()
    expirations = data.get("expirations", [])
    if not expirations:
        print(f"WARNING: No option expirations found for {ticker}.")
    return expirations


def get_option_chain(token: str, account_id: str, ticker: str, expiration_date: str) -> dict:
    """Fetch the option chain (calls + puts) for a ticker and expiration date."""
    import requests
    url = f"{BASE_URL}/userapigateway/marketdata/{account_id}/option-chain"
    payload = {
        "instrument": {
            "symbol": ticker.upper(),
            "type": "EQUITY",
        },
        "expirationDate": expiration_date,
    }
    response = requests.post(url, headers=get_headers(token), json=payload)
    if response.status_code != 200:
        print(
            f"WARNING: Failed to fetch chain for {ticker} {expiration_date} — "
            f"HTTP {response.status_code}: {response.text}",
            file=sys.stderr,
        )
        return {}
    return response.json()


def contract_to_row(contract: dict, option_type: str, expiration_date: str) -> dict:
    """Flatten a single contract dict into a CSV row dict."""
    details = contract.get("optionDetails") or {}
    greeks = details.get("greeks") or {}
    day_change = contract.get("oneDayChange") or {}
    instrument = contract.get("instrument") or {}

    return {
        "option_type": option_type,
        "expiration_date": expiration_date,
        "symbol": instrument.get("symbol", ""),
        "strike_price": details.get("strikePrice", ""),
        "last": contract.get("last", ""),
        "last_timestamp": contract.get("lastTimestamp", ""),
        "bid": contract.get("bid", ""),
        "bid_size": contract.get("bidSize", ""),
        "ask": contract.get("ask", ""),
        "ask_size": contract.get("askSize", ""),
        "mid_price": details.get("midPrice", ""),
        "volume": contract.get("volume", ""),
        "open_interest": contract.get("openInterest", ""),
        "previous_close": contract.get("previousClose", ""),
        "day_change": day_change.get("change", ""),
        "day_change_pct": day_change.get("percentChange", ""),
        "delta": greeks.get("delta", ""),
        "gamma": greeks.get("gamma", ""),
        "theta": greeks.get("theta", ""),
        "vega": greeks.get("vega", ""),
        "rho": greeks.get("rho", ""),
        "implied_volatility": greeks.get("impliedVolatility", ""),
    }


def read_open_position(ticker: str) -> list[dict]:
    """
    Return open positions for ticker from trades.db (read-only).

    Joins positions + trades, aggregates per position:
      net_qty    = SUM(sell qty) - SUM(buy qty)   [positive = net short]
      avg_price  = net credit/debit per share
      entry_date = earliest trade date

    Excludes test trades (is_test = 1).
    """
    import sqlite3

    db_path = os.path.normpath(JOURNAL_DB)
    if not os.path.exists(db_path):
        print(f"ERROR: Journal not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    sql = """
        SELECT
            p.id,
            p.symbol,
            p.option_type,
            p.strike,
            p.expiry,
            p.notes,
            -- net_qty: positive means net short (more sells than buys)
            SUM(CASE WHEN t.action = 'sell' THEN  t.quantity
                     WHEN t.action = 'buy'  THEN -t.quantity
                     ELSE 0 END)                                    AS net_qty,
            -- weighted average price (credit received or debit paid per share)
            SUM(CASE WHEN t.action = 'sell' THEN  t.price * t.quantity
                     WHEN t.action = 'buy'  THEN -t.price * t.quantity
                     ELSE 0 END)
            / NULLIF(ABS(SUM(CASE WHEN t.action = 'sell' THEN  t.quantity
                                  WHEN t.action = 'buy'  THEN -t.quantity
                                  ELSE 0 END)), 0)                  AS avg_price,
            MIN(t.trade_date)                                       AS entry_date
        FROM positions p
        JOIN trades t ON t.position_id = p.id
        WHERE UPPER(p.symbol) = UPPER(?)
          AND p.status = 'open'
          AND t.is_test = 0
        GROUP BY p.id
        ORDER BY p.id
    """
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(sql, (ticker,)).fetchall()
        con.close()
    except sqlite3.OperationalError as exc:
        print(f"ERROR: Could not read journal — {exc}", file=sys.stderr)
        sys.exit(1)

    return [dict(row) for row in rows]


def format_position(pos: dict) -> str:
    """Return a one-line human-readable summary of a position row."""
    net_qty = pos.get("net_qty") or 0
    side    = "Short" if net_qty > 0 else "Long"
    qty_abs = abs(net_qty)
    avg     = pos.get("avg_price")
    price_str = f"${avg:.2f}" if avg is not None else "N/A"
    line = (
        f"{side} {qty_abs}x {pos['symbol'].upper()} "
        f"{pos['option_type'].upper()} "
        f"${float(pos['strike']):.2f} exp {pos['expiry']} "
        f"@ {price_str} (entered {pos['entry_date']})"
    )
    if pos.get("notes"):
        line += f"  — {pos['notes']}"
    return line


def print_positions(ticker: str, positions: list[dict]) -> None:
    """Display open positions in a formatted box."""
    if not positions:
        lines = [f"  No open positions found for {ticker} in the journal."]
    else:
        lines = [f"  {format_position(p)}" for p in positions]
    print_box(lines, title=f"  {ticker} — Open Positions  ")


def get_key_dates(ticker: str) -> dict:
    """
    Return the next earnings date and next dividend ex-date for ticker.
    Uses yfinance; falls back to estimation from historical data if live
    dates are unavailable.  Always returns a dict with keys:
        earnings_date   : str  (YYYY-MM-DD or estimated)
        earnings_source : str  ("confirmed" | "estimated")
        exdiv_date      : str  (YYYY-MM-DD or estimated)
        exdiv_source    : str  ("confirmed" | "estimated")
        dividend_amount : str  (last known quarterly amount, or "N/A")
    """
    try:
        import yfinance as yf
    except ImportError:
        print(
            "ERROR: yfinance is not installed.\n"
            "  Install it with:  pip install yfinance",
            file=sys.stderr,
        )
        sys.exit(1)

    today = datetime.date.today()
    result = {
        "earnings_date": "Unknown",
        "earnings_source": "unknown",
        "exdiv_date": "Unknown",
        "exdiv_source": "unknown",
        "dividend_amount": "N/A",
    }

    t = yf.Ticker(ticker)

    # ── Earnings date ──────────────────────────────────────────────────────────
    try:
        cal = t.calendar  # dict or DataFrame depending on yfinance version
        # Newer yfinance returns a dict; older returns a DataFrame
        if isinstance(cal, dict):
            dates = cal.get("Earnings Date") or cal.get("earningsDate") or []
        else:
            # DataFrame: columns are dates, row 0 is "Earnings Date"
            dates = list(cal.loc["Earnings Date"]) if "Earnings Date" in cal.index else []
        # Keep only future dates
        future = []
        for d in dates:
            try:
                dt = d.date() if hasattr(d, "date") else datetime.date.fromisoformat(str(d)[:10])
                if dt >= today:
                    future.append(dt)
            except Exception:
                pass
        if future:
            result["earnings_date"] = str(min(future))
            result["earnings_source"] = "confirmed"
    except Exception:
        pass

    # Estimate earnings if not found: last earnings + ~91 days
    if result["earnings_source"] != "confirmed":
        try:
            hist_earnings = []
            fin = t.quarterly_income_stmt
            if fin is not None and not fin.empty:
                for col in fin.columns:
                    try:
                        dt = col.date() if hasattr(col, "date") else datetime.date.fromisoformat(str(col)[:10])
                        hist_earnings.append(dt)
                    except Exception:
                        pass
            if hist_earnings:
                last = max(hist_earnings)
                estimated = last + datetime.timedelta(days=91)
                result["earnings_date"] = str(estimated)
                result["earnings_source"] = "estimated"
        except Exception:
            pass

    # ── Dividend ex-date ───────────────────────────────────────────────────────
    try:
        info = t.info or {}
        ex_ts = info.get("exDividendDate")
        if ex_ts:
            ex_date = datetime.date.fromtimestamp(int(ex_ts))
            if ex_date >= today:
                result["exdiv_date"] = str(ex_date)
                result["exdiv_source"] = "confirmed"
                result["dividend_amount"] = f"${info.get('lastDividendValue', 'N/A')}"
    except Exception:
        pass

    # Estimate ex-date if not found: last ex-date + ~91 days
    if result["exdiv_source"] != "confirmed":
        try:
            divs = t.dividends
            if divs is not None and not divs.empty:
                last_amount = float(divs.iloc[-1])
                result["dividend_amount"] = f"${last_amount:.4f}".rstrip("0").rstrip(".")
                past_dates = []
                for idx in divs.index:
                    try:
                        dt = idx.date() if hasattr(idx, "date") else datetime.date.fromisoformat(str(idx)[:10])
                        if dt < today:
                            past_dates.append(dt)
                    except Exception:
                        pass
                if past_dates:
                    last_ex = max(past_dates)
                    estimated = last_ex + datetime.timedelta(days=91)
                    result["exdiv_date"] = str(estimated)
                    result["exdiv_source"] = "estimated"
        except Exception:
            pass

    return result


def print_key_dates(ticker: str, dates: dict) -> None:
    """Print earnings and dividend dates in a formatted box."""
    def fmt(date_str: str, source: str) -> str:
        label = "(estimated)" if source == "estimated" else "(confirmed)"
        return f"{date_str}  {label}"

    lines = [
        f"  Next Earnings   : {fmt(dates['earnings_date'], dates['earnings_source'])}",
        f"  Next Ex-Div     : {fmt(dates['exdiv_date'], dates['exdiv_source'])}",
        f"  Dividend Amount : {dates['dividend_amount']} per share",
    ]
    print_box(lines, title=f"  {ticker} — Key Dates  ")


async def upload_to_notebooklm(file_path: str, notebook_id: str) -> None:
    """Upload a file as a new source to the specified NotebookLM notebook."""
    try:
        from notebooklm import NotebookLMClient
    except ImportError:
        print(
            "ERROR: notebooklm-py is not installed.\n"
            "  Install it with:  pip install \"notebooklm-py[browser]\"\n"
            "  Then run:         playwright install chromium\n"
            "  Then log in:      notebooklm login",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"\nUploading {file_path} to NotebookLM notebook {notebook_id} ...")
    try:
        async with await NotebookLMClient.from_storage() as client:
            await client.sources.add_file(notebook_id, file_path, wait=True)
        print("Upload complete.")
    except Exception as exc:
        print(f"ERROR: Upload failed — {exc}", file=sys.stderr)
        sys.exit(1)


STRAT_LABELS = {
    "CC":   "Covered Call",
    "CSP":  "Cash-Secured Put",
    "ROLL": "Roll",
}


COL_WIDTH = 80  # total terminal width for the output box

# Matches footnote references like [1], [6, 19], [3,6,20]
_FOOTNOTE_RE = re.compile(r"\[\s*\d+(?:\s*,\s*\d+)*\s*\]")


def _clean_inline(text: str) -> str:
    """Strip markdown inline formatting and footnote references from a string."""
    text = _FOOTNOTE_RE.sub("", text)       # remove [6, 19] etc.
    text = re.sub(r"\*{1,3}(.*?)\*{1,3}", r"\1", text)   # **bold**, *italic*, ***both***
    text = re.sub(r"`([^`]+)`", r"\1", text)              # `code`
    text = re.sub(r"\s{2,}", " ", text)                   # collapse extra spaces
    return text.strip()


def _is_table_row(line: str) -> bool:
    return line.strip().startswith("|") and line.strip().endswith("|")


def _is_separator_row(line: str) -> bool:
    return _is_table_row(line) and re.fullmatch(r"[\|\s\-:]+", line.strip()) is not None


def _parse_table_row(line: str) -> list[str]:
    cells = line.strip().strip("|").split("|")
    return [_clean_inline(c) for c in cells]


def _render_table(rows: list[list[str]], inner_width: int) -> list[str]:
    """Format a list of table rows as evenly spaced terminal columns."""
    if not rows:
        return []
    num_cols = max(len(r) for r in rows)
    # Pad rows to same length
    rows = [r + [""] * (num_cols - len(r)) for r in rows]
    # Calculate column widths
    col_widths = [max(len(r[c]) for r in rows) for c in range(num_cols)]
    # Scale to fit inner_width if needed
    total = sum(col_widths) + (num_cols - 1) * 3  # 3 chars between cols
    if total > inner_width and num_cols > 1:
        scale = (inner_width - (num_cols - 1) * 3) / max(sum(col_widths), 1)
        col_widths = [max(4, int(w * scale)) for w in col_widths]
    lines = []
    for i, row in enumerate(rows):
        parts = [row[c].ljust(col_widths[c]) for c in range(num_cols)]
        lines.append("  " + "   ".join(parts).rstrip())
        if i == 0:  # underline header
            lines.append("  " + "   ".join("-" * col_widths[c] for c in range(num_cols)))
    return lines


def _markdown_to_lines(text: str, inner_width: int) -> list[str]:
    """Convert markdown text to plain terminal lines ready for the output box."""
    result: list[str] = []
    raw_lines = text.split("\n")
    i = 0
    while i < len(raw_lines):
        line = raw_lines[i]

        # --- Markdown table block ---
        if _is_table_row(line):
            table_rows: list[list[str]] = []
            while i < len(raw_lines) and _is_table_row(raw_lines[i]):
                if not _is_separator_row(raw_lines[i]):
                    table_rows.append(_parse_table_row(raw_lines[i]))
                i += 1
            result.extend(_render_table(table_rows, inner_width))
            result.append("")
            continue

        # --- Heading (## / ###) ---
        heading_match = re.match(r"^#{1,6}\s+(.*)", line)
        if heading_match:
            heading = _clean_inline(heading_match.group(1)).upper()
            result.append("")
            result.append(heading)
            result.append("-" * min(len(heading), inner_width))
            i += 1
            continue

        # --- Bullet / numbered list item ---
        bullet_match = re.match(r"^(\s*[-*+]|\s*\d+\.)\s+(.*)", line)
        if bullet_match:
            content = _clean_inline(bullet_match.group(2))
            indent = "  • "
            wrapped = textwrap.wrap(content, width=inner_width - len(indent))
            for j, seg in enumerate(wrapped):
                result.append((indent if j == 0 else "    ") + seg)
            i += 1
            continue

        # --- Blank line ---
        if not line.strip():
            result.append("")
            i += 1
            continue

        # --- Normal paragraph line ---
        result.append(_clean_inline(line))
        i += 1

    # Collapse consecutive blank lines
    collapsed: list[str] = []
    prev_blank = False
    for line in result:
        is_blank = not line.strip()
        if is_blank and prev_blank:
            continue
        collapsed.append(line)
        prev_blank = is_blank
    return collapsed


def print_box(lines: list[str], title: str = "") -> None:
    """Print content in a fixed-width box with aligned columns."""
    inner = COL_WIDTH - 2  # space between the border pipes
    border = "+" + "-" * inner + "+"
    print(border)
    if title:
        print("|" + title.center(inner) + "|")
        print(border)
    for line in lines:
        # Wrap each line to fit inside the box
        wrapped = textwrap.wrap(line, width=inner) if line.strip() else [""]
        for segment in wrapped:
            print("|" + segment.ljust(inner) + "|")
    print(border)


async def query_notebooklm(
    notebook_id: str,
    ticker: str,
    strat: str,
    dates: dict,
    positions: list[dict] | None = None,
) -> None:
    """Ask NotebookLM for the best CC or CSP choice given the ticker source."""
    try:
        from notebooklm import NotebookLMClient
    except ImportError:
        print(
            "ERROR: notebooklm-py is not installed.\n"
            "  Install it with:  pip install \"notebooklm-py[browser]\"\n"
            "  Then run:         playwright install chromium\n"
            "  Then log in:      notebooklm login",
            file=sys.stderr,
        )
        sys.exit(1)

    strat_label = STRAT_LABELS[strat]

    # Embed known dates so the LLM can reason about expiry selection precisely
    earnings_note = (
        f"the next earnings announcement is {dates['earnings_date']} "
        f"({dates['earnings_source']})"
    )
    exdiv_note = (
        f"the next ex-dividend date is {dates['exdiv_date']} "
        f"({dates['exdiv_source']}, {dates['dividend_amount']} per share)"
    )

    if strat == "ROLL":
        # Summarise current open positions for context
        if positions:
            pos_summary = "; ".join(format_position(p) for p in positions)
            pos_context = f"The current open position(s) are: {pos_summary}. "
            # Total contracts across all open positions (absolute net qty)
            total_contracts = sum(abs(p.get("net_qty") or 0) for p in positions)
        else:
            pos_context = "No open positions were found in the journal for this ticker. "
            total_contracts = 1  # fallback so the formula still appears

        pnl_instruction = (
            f"In the Execution Instructions section, add as the final bullet point "
            f"the potential PnL if the recommended rolled-to option is bought back at "
            f"40% of its sale price. Use this formula and show the working: "
            f"PnL = Sale Price × 0.60 × 100 × {total_contracts} contract(s). "
            f"Label it 'Target 40% Profit (buy-back at 40% of premium)'."
        )
        question = (
            f"Based on the updated {ticker} source, the latest economic release calendar, "
            f"and the latest PLAN and REVIEW sources, determine the best roll or "
            f"do nothing strategy. "
            f"{pos_context}"
            f"Note that {earnings_note} and {exdiv_note}. "
            f"{pnl_instruction}"
        )
    else:
        question = (
            f"Given the {ticker} source, what is the best {strat_label} choice, "
            f"taking into consideration the economic calendar releases, "
            f"upcoming dividends and/or earnings releases? "
            f"Note that {earnings_note} and {exdiv_note}."
        )

    print(f"\nQuerying NotebookLM...")

    try:
        async with await NotebookLMClient.from_storage() as client:
            result = await client.chat.ask(notebook_id, question)
    except Exception as exc:
        print(f"ERROR: Query failed — {exc}", file=sys.stderr)
        sys.exit(1)

    answer = getattr(result, "answer", None) or str(result)

    inner = COL_WIDTH - 2
    output_lines = _markdown_to_lines(answer, inner_width=inner)

    # Prepend ticker to the first non-empty, non-heading line
    for idx, line in enumerate(output_lines):
        if line.strip() and not line.startswith("-"):
            output_lines[idx] = f"{ticker}: {line}"
            break

    # Trim leading/trailing blank lines
    while output_lines and not output_lines[0].strip():
        output_lines.pop(0)
    while output_lines and not output_lines[-1].strip():
        output_lines.pop()

    title = f"  {ticker} — {strat_label} Recommendation  "
    print_box(output_lines, title=title)


_HELP_DESCRIPTION = """\
Options Chain Tool — fetches option chains from Public.com and optionally
uploads them to Google NotebookLM for AI-powered strategy recommendations.

REQUIRED ENVIRONMENT VARIABLES
  PUBLIC_API_SECRET        Your Public.com secret token (Settings → API).
                           Used to mint a short-lived access token each run.
  NOTEBOOKLM_NOTEBOOK_ID  Your NotebookLM notebook ID.
                           Required when using --upload, --onlyload, or --strat.

OPTIONS
"""

_HELP_EPILOG = """\
EXAMPLES
  Fetch 5 expirations for IBM and save to IBM.csv:
    python get_option_chain.py --ticker IBM --num 5

  Fetch, save, and upload to NotebookLM:
    python get_option_chain.py --ticker IBM --num 5 --upload

  Skip fetch — upload an existing IBM.csv to NotebookLM:
    python get_option_chain.py --ticker IBM --onlyload

  Ask NotebookLM for the best Covered Call (source already uploaded):
    python get_option_chain.py --ticker IBM --strat CC

  Full pipeline — fetch, upload, then get a CSP recommendation:
    python get_option_chain.py --ticker IBM --num 5 --upload --strat CSP

  Roll analysis — fetch fresh chain, upload, read journal, query for roll/hold:
    python get_option_chain.py --ticker IBM --num 5 --upload --strat ROLL

  Roll analysis using existing CSV (no re-fetch):
    python get_option_chain.py --ticker IBM --onlyload --strat ROLL

JOURNAL
  Open positions are read (read-only) from:
    ../option_trade_journal/trades.db
  Tables used: positions (symbol, option_type, strike, expiry, status)
               trades    (action, quantity, price, trade_date, is_test)

NOTEBOOKLM SETUP (one-time)
  pip install "notebooklm-py[browser]"
  playwright install chromium
  notebooklm login

DEPENDENCIES
  pip install requests yfinance
  pip install "notebooklm-py[browser]"  (only for --upload / --onlyload / --strat)
"""


def main():
    parser = argparse.ArgumentParser(
        description=_HELP_DESCRIPTION,
        epilog=_HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=True,
    )
    parser.add_argument(
        "--ticker",
        required=True,
        metavar="SYMBOL",
        help="Stock ticker symbol, e.g. IBM  (required)",
    )
    parser.add_argument(
        "--num",
        type=int,
        default=10,
        metavar="N",
        help="Number of expiration dates to fetch  (default: 10)",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Upload the output CSV to NotebookLM after fetching",
    )
    parser.add_argument(
        "--onlyload",
        action="store_true",
        help="Skip fetching — upload an existing {TICKER}.csv to NotebookLM",
    )
    parser.add_argument(
        "--strat",
        choices=["CC", "CSP", "ROLL"],
        metavar="CC|CSP|ROLL",
        help="Query NotebookLM for the best strategy:\n"
             "  CC   = Covered Call\n"
             "  CSP  = Cash-Secured Put\n"
             "  ROLL = Roll or hold — reads open position from the journal,\n"
             "         downloads/loads the chain, then queries NotebookLM",
    )
    args = parser.parse_args()

    ticker = args.ticker.upper()
    num = args.num
    output_file = f"{ticker}.csv"

    # If only --ticker + --strat are given (no fetch/upload flags), skip straight
    # to key dates + NotebookLM — no option chain work needed.
    strat_only = args.strat and not args.upload and not args.onlyload

    if strat_only:
        pass  # fall through to NotebookLM steps below
    elif args.onlyload:
        # Skip all API fetching — just upload the existing file
        if not os.path.exists(output_file):
            print(f"ERROR: {output_file} not found. Run without --onlyload first to generate it.", file=sys.stderr)
            sys.exit(1)
        print(f"Skipping fetch — using existing {output_file}")
    else:
        # Load Secret Token from environment, then exchange for a short-lived Access Token
        secret = os.environ.get("PUBLIC_API_SECRET")
        if not secret:
            print(
                "ERROR: PUBLIC_API_SECRET environment variable not set.\n"
                "  1. Go to https://public.com (Settings → API)\n"
                "  2. Generate a Secret Token\n"
                "  3. Run:  export PUBLIC_API_SECRET=your_secret_here",
                file=sys.stderr,
            )
            sys.exit(1)

        print(f"Fetching option chains for {ticker} ({num} expiration dates)...")

        # Step 1 — Exchange secret for a short-lived access token
        token = get_access_token(secret)

        # Step 2 — Get account ID
        account_id = get_account_id(token)

        # Step 3 — Get expiration dates (capped at num)
        all_expirations = get_expirations(token, account_id, ticker)
        expirations = all_expirations[:num]
        print(f"Found {len(all_expirations)} expiration(s); fetching {len(expirations)}.")

        if not expirations:
            print("No expirations to process. Exiting.")
            sys.exit(0)

        # Step 4 — Fetch chains and collect rows
        all_rows = []
        for exp_date in expirations:
            print(f"  Fetching chain for {exp_date}...", end=" ", flush=True)
            chain = get_option_chain(token, account_id, ticker, exp_date)
            if not chain:
                print("skipped (no data).")
                continue

            calls = chain.get("calls", [])
            puts = chain.get("puts", [])
            rows_before = len(all_rows)

            for contract in calls:
                all_rows.append(contract_to_row(contract, "CALL", exp_date))
            for contract in puts:
                all_rows.append(contract_to_row(contract, "PUT", exp_date))

            added = len(all_rows) - rows_before
            print(f"{len(calls)} calls, {len(puts)} puts ({added} rows).")

        # Step 5 — Write CSV
        with open(output_file, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(all_rows)

        print(f"\nDone! {len(all_rows)} rows written to {output_file}")

    # Resolve notebook_id once if any NotebookLM step is needed
    needs_notebook = args.upload or args.onlyload or args.strat
    notebook_id = None
    if needs_notebook:
        notebook_id = os.environ.get("NOTEBOOKLM_NOTEBOOK_ID")
        if not notebook_id:
            print(
                "ERROR: NOTEBOOKLM_NOTEBOOK_ID environment variable not set.\n"
                "  Set it with:  export NOTEBOOKLM_NOTEBOOK_ID=your_notebook_id",
                file=sys.stderr,
            )
            sys.exit(1)

    # Step 6 — For ROLL: read open positions from journal
    open_positions = None
    if args.strat == "ROLL":
        print(f"\nReading open positions for {ticker} from journal...")
        open_positions = read_open_position(ticker)
        print_positions(ticker, open_positions)
        if not open_positions:
            print("  (No open positions found — proceeding with ROLL query anyway.)")

    # Step 7 — Fetch and display key dates (when --strat is used)
    key_dates = None
    if args.strat:
        print(f"\nLooking up key dates for {ticker}...")
        key_dates = get_key_dates(ticker)
        print_key_dates(ticker, key_dates)

    # Step 8 — Optionally upload to NotebookLM
    if not strat_only and (args.upload or args.onlyload):
        asyncio.run(upload_to_notebooklm(output_file, notebook_id))

    # Step 9 — Optionally query NotebookLM for strategy recommendation
    if args.strat:
        asyncio.run(query_notebooklm(notebook_id, ticker, args.strat, key_dates, open_positions))


if __name__ == "__main__":
    main()
