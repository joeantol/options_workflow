#!/Users/joeandbabs/public-env/bin/python
"""
option_dashboard.py

Fetches option chains from the Public.com API and saves them to a CSV file.
Optionally uploads the CSV to a Google NotebookLM notebook.

Usage:
    python option_dashboard.py --ticker IBM [--num 10] [--upload]

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
import logging
import os
import re
import sys
import textwrap
import threading
from functools import wraps
from pathlib import Path

import greeks_pricing
import claude_advisor
import openai_advisor
import iv_history

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass  # python-dotenv not installed; rely on environment variables


BASE_URL = "https://api.public.com"
AUTH_URL = "https://api.public.com/userapiauthservice/personal/access-tokens"

# Journal DB lives one level up from the script's directory
JOURNAL_DB = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "option_trade_journal", "trades.db"
)

# Logs directory sits alongside the script
_LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

# Module-level logger — call setup_logging() to activate file output
log = logging.getLogger("options")

# Tracks when each ticker's CSV was last uploaded to NotebookLM {ticker: datetime}
_last_upload_time: dict[str, datetime.datetime] = {}
_last_source_ids:  dict[str, str] = {}          # ticker → most recent uploaded source_id
_UPLOAD_TTL_MINUTES = 45  # skip re-upload if source is fresher than this


def setup_logging(level: int = logging.DEBUG) -> str:
    """
    Configure logging to both console and a timestamped log file.

    Creates  logs/options_YYYYMMDD_HHMMSS.log  on every call (i.e. every
    server bounce) and updates the  logs/latest.log  symlink to point at it.

    Returns the path to the new log file.
    """
    os.makedirs(_LOGS_DIR, exist_ok=True)

    stamp    = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(_LOGS_DIR, f"options_{stamp}.log")
    link     = os.path.join(_LOGS_DIR, "latest.log")

    fmt = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler — new file every bounce
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(fmt)

    # Console handler — CRITICAL only
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.CRITICAL)
    ch.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(fh)
    root.addHandler(ch)

    # Symlink latest.log → options_YYYYMMDD_HHMMSS.log
    try:
        if os.path.islink(link) or os.path.exists(link):
            os.remove(link)
        os.symlink(os.path.basename(log_file), link)
    except OSError as exc:
        log.warning("Could not create latest.log symlink: %s", exc)

    log.info("Logging started — %s", log_file)
    return log_file

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
        msg = (
            "Unauthorized — PUBLIC_API_SECRET is invalid or revoked. "
            "Generate a new one at https://public.com/settings (API section)."
        )
        print(f"ERROR: {msg}", file=sys.stderr)
        raise RuntimeError(msg)
    if response.status_code == 429:
        msg = "Rate limited by Public.com auth endpoint. Try again shortly."
        print(f"ERROR: {msg}", file=sys.stderr)
        raise RuntimeError(msg)
    if response.status_code != 200:
        msg = f"Failed to obtain access token — HTTP {response.status_code}: {response.text[:200]}"
        print(f"ERROR: {msg}", file=sys.stderr)
        raise RuntimeError(msg)
    access_token = response.json().get("accessToken")
    if not access_token:
        msg = "No accessToken in auth response."
        print(f"ERROR: {msg}", file=sys.stderr)
        raise RuntimeError(msg)
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
        raise RuntimeError("Unauthorized fetching account — check PUBLIC_API_SECRET.")
    if response.status_code != 200:
        raise RuntimeError(
            f"Failed to fetch accounts — HTTP {response.status_code}: {response.text[:200]}"
        )
    data = response.json()
    accounts = data.get("accounts", [])
    if not accounts:
        raise RuntimeError("No accounts found for this token.")
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
        raise RuntimeError(
            f"Failed to fetch expirations for {ticker} — "
            f"HTTP {response.status_code}: {response.text[:200]}"
        )
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
            COALESCE(p.ul_cost_basis, 0)                           AS ul_cost_basis,
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


def get_all_open_positions(ticker: str | None = None) -> list[dict]:
    """
    Return all open positions from trades.db, optionally filtered by ticker.

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

    ticker_clause = "AND UPPER(p.symbol) = UPPER(:ticker)" if ticker else ""
    params: dict = {"ticker": ticker} if ticker else {}

    sql = f"""
        SELECT
            p.id,
            p.symbol,
            p.option_type,
            p.strike,
            p.expiry,
            p.notes,
            p.spread_id,
            COALESCE(p.ul_cost_basis, 0)                           AS ul_cost_basis,
            p.entry_iv,
            p.entry_underlying_price,
            p.entry_delta,
            p.entry_gamma,
            p.entry_theta,
            p.entry_vega,
            p.entry_snapshot_at,
            SUM(CASE WHEN t.action = 'sell' THEN  t.quantity
                     WHEN t.action = 'buy'  THEN -t.quantity
                     ELSE 0 END)                                    AS net_qty,
            SUM(CASE WHEN t.action = 'sell' THEN  t.price * t.quantity
                     WHEN t.action = 'buy'  THEN -t.price * t.quantity
                     ELSE 0 END)
            / NULLIF(ABS(SUM(CASE WHEN t.action = 'sell' THEN  t.quantity
                                  WHEN t.action = 'buy'  THEN -t.quantity
                                  ELSE 0 END)), 0)                  AS avg_price,
            MIN(t.trade_date)                                       AS entry_date
        FROM positions p
        JOIN trades t ON t.position_id = p.id
        WHERE p.status = 'open'
          AND t.is_test = 0
          {ticker_clause}
        GROUP BY p.id
        HAVING net_qty != 0
        ORDER BY p.symbol, p.expiry, p.strike
    """

    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(sql, params).fetchall()
        con.close()
    except sqlite3.OperationalError as exc:
        print(f"ERROR: Could not read journal — {exc}", file=sys.stderr)
        sys.exit(1)

    return [dict(row) for row in rows]


# Mirrors the journal's autoComm/autoFees (templates/index.html, "Auto fee /
# commission calculation (Fidelity schedule)") exactly, so a PROJECTED roll's
# commission/fee estimate lines up with what the journal would actually record
# once the trades are entered for real. Keep these two in sync if the schedule
# ever changes on the journal side.
_FEE_INDEX_SYMS = {"SPX", "SPXW", "XSP", "NDX", "NDXP", "VIX", "VIXW", "RUT", "RUTW", "MNX", "MNXW"}
_FEE_COMM_RATE  = 0.65      # $ per contract
_FEE_ORF_RATE   = 0.03      # $ per contract — Options Regulatory Fee (all options)
_FEE_SEC_RATE   = 0.000020  # $ per $ of principal — SRO/SEC assessment (sell orders only)
_FEE_INDEX_FEE  = 0.25      # $ per contract — index/proprietary surcharge


def auto_comm(action: str, price: float, qty: int, is_close: bool) -> float:
    """Estimated commission for a trade that hasn't happened yet, per the same
    schedule the journal auto-fills with. Fidelity waives commission on a
    buy-to-close at $0.65/share or under."""
    if qty <= 0:
        return 0.0
    if is_close and action == "buy" and price <= _FEE_COMM_RATE:
        return 0.0
    return round(_FEE_COMM_RATE * qty, 2)


def auto_fees(action: str, price: float, qty: int, symbol: str) -> float:
    """Estimated regulatory fees for a trade that hasn't happened yet, per the
    same schedule the journal auto-fills with."""
    if qty <= 0:
        return 0.0
    principal = price * 100 * qty
    fees = _FEE_ORF_RATE * qty
    if action == "sell":
        fees += _FEE_SEC_RATE * principal
    if (symbol or "").upper().strip() in _FEE_INDEX_SYMS:
        fees += _FEE_INDEX_FEE * qty
    return round(fees, 2)


def get_chain_net_cash(spread_id, fallback_pos_id: int | None = None) -> dict:
    """
    Return a breakdown of net cash for all positions sharing the same spread_id.
    If spread_id is NULL, falls back to just fallback_pos_id's own trades (a
    standalone position with no roll chain) — NOT the ticker's whole trading
    history, which would silently pull in unrelated past chains on the same symbol.

    Commission and fees are netted into collected/paid per trade, matching the
    journal's CF_SQL, so this figure reconciles with the journal's own totals.

    Returns dict with:
        net_cash     : float  — (collected − paid), each already net of commission/fees
        collected    : float  — net sell proceeds (× 100, minus commission/fees)
        paid         : float  — net buy cost (× 100, plus commission/fees)
        num_positions: int    — number of positions in the chain
        scoped_by    : str    — 'spread_id' or 'position' (indicates which filter was used)
    Positive net_cash means net credit received.
    """
    import sqlite3

    db_path = os.path.normpath(JOURNAL_DB)
    if not os.path.exists(db_path):
        return {"net_cash": 0.0, "collected": 0.0, "paid": 0.0, "num_positions": 0, "scoped_by": "none"}

    if spread_id is not None:
        where  = "p.spread_id = ?"
        param  = spread_id
        scoped = "spread_id"
        log.debug("get_chain_net_cash: using spread_id=%r", spread_id)
    elif fallback_pos_id is not None:
        where  = "p.id = ?"
        param  = fallback_pos_id
        scoped = "position"
        log.debug("get_chain_net_cash: spread_id is NULL, falling back to pos_id=%r", fallback_pos_id)
    else:
        return {"net_cash": 0.0, "collected": 0.0, "paid": 0.0, "num_positions": 0, "scoped_by": "none"}

    # Cash flow netted per trade against commission/fees — mirrors the journal's CF_SQL
    # exactly (sell proceeds minus fees; buy cost plus fees) so totals reconcile.
    sql = f"""
        SELECT
            SUM(CASE WHEN t.action = 'sell' THEN (t.price * t.quantity * 100) - t.commission - t.fees ELSE 0 END) AS collected,
            SUM(CASE WHEN t.action = 'buy'  THEN (t.price * t.quantity * 100) + t.commission + t.fees ELSE 0 END) AS paid,
            COUNT(DISTINCT p.id) AS num_positions
        FROM positions p
        JOIN trades t ON t.position_id = p.id
        WHERE {where}
          AND t.is_test = 0
    """
    # Second query: include test trades to get simulated (test-close) PnL
    sql_sim = f"""
        SELECT
            SUM(CASE WHEN t.action = 'sell' THEN (t.price * t.quantity * 100) - t.commission - t.fees ELSE 0 END) AS collected,
            SUM(CASE WHEN t.action = 'buy'  THEN (t.price * t.quantity * 100) + t.commission + t.fees ELSE 0 END) AS paid
        FROM positions p
        JOIN trades t ON t.position_id = p.id
        WHERE {where}
    """
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row     = con.execute(sql,     (param,)).fetchone()
        row_sim = con.execute(sql_sim, (param,)).fetchone()
        con.close()
        collected = round(float(row[0] or 0.0), 2)
        paid      = round(float(row[1] or 0.0), 2)
        n_pos     = int(row[2] or 0)
        sim_collected = round(float(row_sim[0] or 0.0), 2)
        sim_paid      = round(float(row_sim[1] or 0.0), 2)
        has_test  = (sim_collected != collected or sim_paid != paid)
        log.debug("get_chain_net_cash: collected=%.2f paid=%.2f positions=%d has_test=%s",
                  collected, paid, n_pos, has_test)
        return {
            "net_cash":      round(collected - paid, 2),
            "collected":     collected,
            "paid":          paid,
            "num_positions": n_pos,
            "scoped_by":     scoped,
            "sim_net_cash":  round(sim_collected - sim_paid, 2),
            "has_test":      has_test,
        }
    except sqlite3.OperationalError as exc:
        log.error("get_chain_net_cash query failed: %s", exc)
        return {"net_cash": 0.0, "collected": 0.0, "paid": 0.0, "num_positions": 0,
                "scoped_by": "error", "sim_net_cash": 0.0, "has_test": False}


def get_pos_hypo_cash(pos_id: int) -> dict:
    """
    Return cash flows for a single position including test trades.
    Mirrors the journal's HYPO_CLOSED logic: when a test trade exists,
    the position-level PnL (real open + test close) is what the row should show.

    Returns:
        pos_cash : float  — (sell proceeds − buy costs) × 100 for this position only
        has_test : bool   — True if any test trade exists on this position
    """
    import sqlite3
    db_path = os.path.normpath(JOURNAL_DB)
    if not os.path.exists(db_path):
        return {"pos_cash": None, "has_test": False}
    # Mirror journal CF_SQL: include commissions and fees so dashboard matches journal
    sql = """
        SELECT
            SUM(CASE WHEN t.action='sell'
                     THEN (t.price * t.quantity * 100) - t.commission - t.fees
                     ELSE 0 END)                          AS collected,
            SUM(CASE WHEN t.action='buy'
                     THEN (t.price * t.quantity * 100) + t.commission + t.fees
                     ELSE 0 END)                          AS paid,
            MAX(CASE WHEN t.is_test=1 THEN 1 ELSE 0 END) AS has_test
        FROM trades t
        WHERE t.position_id = ?
    """
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = con.execute(sql, (pos_id,)).fetchone()
        con.close()
        if row is None or row[0] is None:
            return {"pos_cash": None, "has_test": False}
        collected = round(float(row[0] or 0.0), 2)
        paid      = round(float(row[1] or 0.0), 2)
        has_test  = bool(row[2])
        return {"pos_cash": round(collected - paid, 2), "has_test": has_test}
    except Exception as exc:
        log.error("get_pos_hypo_cash query failed: %s", exc)
        return {"pos_cash": None, "has_test": False}


def get_ul_cost_basis_from_db(ticker: str) -> float:
    """Look up the underlying cost basis for ticker from trades.db.
    Checks open positions first, then any historical position with ul_cost_basis set.
    Returns 0.0 if not found."""
    import sqlite3
    db_path = os.path.normpath(JOURNAL_DB)
    if not os.path.exists(db_path):
        return 0.0
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        # Prefer a position that still has open trades; fall back to any row with cb set
        row = con.execute(
            """
            SELECT p.ul_cost_basis
            FROM positions p
            JOIN trades t ON t.position_id = p.id
            WHERE UPPER(p.symbol) = UPPER(?)
              AND p.ul_cost_basis > 0
            ORDER BY t.trade_date DESC
            LIMIT 1
            """,
            (ticker,),
        ).fetchone()
        if not row:
            # Fallback: any position row for this ticker with cb set (no trade join)
            row = con.execute(
                "SELECT ul_cost_basis FROM positions WHERE UPPER(symbol) = UPPER(?) AND ul_cost_basis > 0 ORDER BY id DESC LIMIT 1",
                (ticker,),
            ).fetchone()
        con.close()
        return float(row[0]) if row and row[0] else 0.0
    except Exception as exc:
        log.warning("get_ul_cost_basis_from_db(%s) failed: %s", ticker, exc)
        return 0.0


def get_stock_qty_from_db(ticker: str) -> int | None:
    """Return the number of CC contracts available for ticker from trades.db.

    Queries open positions where option_type is not CALL/PUT (i.e. stock/share
    positions).  The DB stores contracts directly — no division needed.
    Returns None if no position is found.
    """
    import sqlite3

    db_path = os.path.normpath(JOURNAL_DB)
    if not os.path.exists(db_path):
        log.warning("get_stock_qty_from_db: DB not found at %s", db_path)
        return None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        # Use the most recent call position for this ticker (open or closed)
        # to determine the contract qty for a new CC.
        # Use the sell quantity from the most recent call position (open or closed).
        # Net qty is 0 for closed positions, so we look at the original sell trade.
        row = con.execute(
            """
            SELECT t.quantity
            FROM trades t
            JOIN positions p ON t.position_id = p.id
            WHERE UPPER(p.symbol) = UPPER(?)
              AND UPPER(p.option_type) = 'CALL'
              AND t.action = 'sell'
            ORDER BY p.id DESC
            LIMIT 1
            """,
            (ticker,),
        ).fetchone()
        con.close()
        if row and row[0]:
            qty = int(row[0])
            log.info("get_stock_qty_from_db(%s): found %d contracts from last call position", ticker, qty)
            return qty if qty > 0 else None
        log.info("get_stock_qty_from_db(%s): no call position found", ticker)
        return None
    except Exception as exc:
        log.warning("get_stock_qty_from_db(%s) failed: %s", ticker, exc)
        return None


# Cache underlying prices within a session to avoid redundant yfinance calls
_price_cache: dict[str, float | None] = {}


def _fetch_yf_price(ticker: str) -> float | None:
    """Fetch current price via yfinance without touching the session cache.
    Uses fast_info first, then history() as fallback — avoids quoteSummary
    which 404s for ETFs (GDX, GLD, SLV, etc.)."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        fi = t.fast_info
        price = float(fi.last_price) if getattr(fi, "last_price", None) else None
        if not price:
            # 1-minute bars for today → last Close is the current intraday price
            hist = t.history(period="1d", interval="1m")
            if hist is not None and not hist.empty:
                price = float(hist["Close"].iloc[-1])
        if price is None:
            log.warning("[PRICE] yfinance returned None for %s (fast_info.last_price=%s)", ticker, getattr(fi, "last_price", "N/A"))
        else:
            log.info("[PRICE] %s = $%.2f", ticker, price)
        return price
    except Exception as exc:
        log.warning("[PRICE] yfinance error for %s: %s", ticker, exc)
        return None


def get_underlying_price(ticker: str) -> float | None:
    """Return the current underlying stock price via yfinance (cached per run)."""
    if ticker in _price_cache:
        return _price_cache[ticker]
    price = _fetch_yf_price(ticker)
    _price_cache[ticker] = price
    return price


def get_underlying_price_fresh(ticker: str) -> float | None:
    """Fetch a live underlying price from yfinance, bypassing the session cache."""
    price = _fetch_yf_price(ticker)
    if price is not None:
        _price_cache[ticker] = price  # keep cache warm for other callers
    return price


def get_vix() -> float | None:
    """Return the current VIX level via yfinance."""
    try:
        import yfinance as yf
        fi = yf.Ticker("^VIX").fast_info
        val = getattr(fi, "last_price", None)
        return float(val) if val else None
    except Exception:
        return None


def get_atr(ticker: str, period: int = 14) -> float | None:
    """
    Calculate the Average True Range for *ticker* over *period* trading days.

    True Range = max(High-Low, |High-PrevClose|, |Low-PrevClose|)
    ATR = simple average of TR over the last *period* bars.
    Returns None if insufficient data.
    """
    try:
        import yfinance as yf
        # Fetch enough bars to guarantee `period` complete TRs
        hist = yf.Ticker(ticker).history(period=f"{period + 5}d")
        if hist is None or len(hist) < period + 1:
            return None
        highs  = hist["High"].values
        lows   = hist["Low"].values
        closes = hist["Close"].values
        trs = []
        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i]  - closes[i - 1]),
            )
            trs.append(tr)
        if len(trs) < period:
            return None
        return sum(trs[-period:]) / period
    except Exception:
        return None


# Percentage distance from strike that counts as "near ATM"
ATM_THRESHOLD_PCT = 3.0


def build_osi_symbol(ticker: str, expiry: str, option_type: str, strike: float) -> str:
    """
    Build the OSI (OCC) normalised option symbol.
    Format: {root}{YYMMDD}{C|P}{strike*1000:>08d}  (ticker not space-padded)
    Example: IBM $190.00 Put 2025-06-20  →  "IBM250620P00190000"
    """
    import datetime
    root = ticker.upper()
    dt   = datetime.date.fromisoformat(expiry)
    cp   = "C" if option_type.upper() == "CALL" else "P"
    strike_int = round(float(strike) * 1000)
    return f"{root}{dt.strftime('%y%m%d')}{cp}{strike_int:08d}"


def get_equity_quotes_batch(token: str, account_id: str, tickers: list[str]) -> dict[str, float | None]:
    """
    Fetch current equity prices for a list of tickers via the Public.com quotes endpoint.
    Returns dict: TICKER -> price (float) or None on failure.
    Falls back to yfinance for any ticker not returned by the API.
    """
    import requests

    if not tickers:
        return {}

    instruments = [{"symbol": t.upper(), "type": "EQUITY"} for t in tickers]
    url = f"{BASE_URL}/userapigateway/marketdata/{account_id}/quotes"
    result: dict[str, float | None] = {}
    try:
        resp = requests.post(url, headers=get_headers(token), json={"instruments": instruments})
        if resp.status_code == 200:
            raw = resp.json()
            quotes_list = raw if isinstance(raw, list) else raw.get("quotes", [])
            for q in quotes_list:
                sym = (q.get("instrument") or {}).get("symbol", "").upper()
                if not sym:
                    continue
                bid  = q.get("bid")
                ask  = q.get("ask")
                last = q.get("last")
                try:
                    if bid is not None and ask is not None:
                        price = (float(bid) + float(ask)) / 2
                    elif last is not None:
                        price = float(last)
                    else:
                        price = None
                except (ValueError, TypeError):
                    price = None
                result[sym] = price
    except Exception:
        pass

    # yfinance fallback for any ticker missing from API response
    for t in tickers:
        sym = t.upper()
        if sym not in result or result[sym] is None:
            result[sym] = _fetch_yf_price(sym)

    return result


def get_option_quotes(token: str, account_id: str, positions: list[dict]) -> dict[str, dict]:
    """
    Fetch quotes (price + greeks) for a list of positions in a single API call.

    Uses POST /userapigateway/marketdata/{accountId}/quotes with type=OPTION.
    Returns a dict keyed by OSI symbol → quote dict.
    Silently skips positions whose OSI symbol cannot be constructed.
    """
    import requests

    instruments = []
    for pos in positions:
        try:
            osi = build_osi_symbol(
                pos["symbol"], pos["expiry"],
                pos["option_type"], float(pos["strike"]),
            )
            instruments.append({"symbol": osi, "type": "OPTION"})
        except Exception:
            pass

    if not instruments:
        return {}

    url = f"{BASE_URL}/userapigateway/marketdata/{account_id}/quotes"
    response = requests.post(
        url,
        headers=get_headers(token),
        json={"instruments": instruments},
    )
    if response.status_code != 200:
        print(
            f"WARNING: Quotes fetch failed — HTTP {response.status_code}: {response.text}",
            file=sys.stderr,
        )
        return {}

    result: dict[str, dict] = {}
    for q in response.json().get("quotes", []):
        sym = (q.get("instrument") or {}).get("symbol", "")
        if sym:
            result[sym.replace(" ", "")] = q   # normalise: strip any padding
    return result


def get_option_greeks_batch(token: str, account_id: str, osi_symbols: list[str]) -> dict[str, dict]:
    """
    Fetch greeks for a list of OSI symbols in one GET request.
    GET /userapigateway/option-details/{accountId}/greeks?osiSymbols=...
    Returns dict: normalised_osi → greeks dict  (delta, gamma, theta, vega, rho, impliedVolatility)
    """
    import requests

    if not osi_symbols:
        return {}

    url = f"{BASE_URL}/userapigateway/option-details/{account_id}/greeks"
    # osiSymbols is a repeated query parameter
    params = [("osiSymbols", sym) for sym in osi_symbols]
    response = requests.get(url, headers=get_headers(token), params=params)
    if response.status_code != 200:
        print(
            f"WARNING: Greeks fetch failed — HTTP {response.status_code}: {response.text}",
            file=sys.stderr,
        )
        return {}

    result: dict[str, dict] = {}
    for item in response.json().get("greeks", []):
        sym = item.get("symbol", "").replace(" ", "")
        if sym:
            result[sym] = item.get("greeks") or {}
    return result


def _render_advisor_markdown(text: str) -> str:
    """
    Minimal markdown -> HTML for the Luna second-opinion text: **bold**,
    *italic*, '- ' bullets (wrapped in a real <ul>), blank-line paragraph
    breaks. Deliberately separate from the main analysis body's line-by-line
    renderer (headers/tables/etc.) — Claude/Luna's output is plain prose per
    their shared system prompt, doesn't need that much machinery.
    """
    import html as _html_mod

    def _inline(s: str) -> str:
        s = _html_mod.escape(s)
        s = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', s)
        s = re.sub(r'\*(.+?)\*', r'<em>\1</em>', s)
        return s

    blocks: list[str] = []
    bullet_buf: list[str] = []

    def _flush_bullets():
        if bullet_buf:
            blocks.append('<ul style="margin:4px 0 4px 20px">' + "".join(bullet_buf) + '</ul>')
            bullet_buf.clear()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            _flush_bullets()
            continue
        if re.fullmatch(r'-{3,}|\*{3,}', line):
            _flush_bullets()
            blocks.append('<hr style="border:none;border-top:1px solid var(--border);margin:10px 0">')
            continue
        _heading_m = re.match(r'(#{1,4})\s+(.*)', line)
        if _heading_m:
            _flush_bullets()
            # Inline-styled — this card uses .chain-working, not .rec-body,
            # so it doesn't inherit the analyze-body heading CSS.
            blocks.append(
                f'<p style="margin-top:10px;font-weight:600;color:var(--accent)">'
                f'{_inline(_heading_m.group(2))}</p>'
            )
            continue
        if line.startswith("- ") or line.startswith("• "):
            bullet_buf.append(f"<li>{_inline(line[2:])}</li>")
            continue
        _flush_bullets()
        blocks.append(f'<p style="margin-bottom:10px">{_inline(line)}</p>')
    _flush_bullets()
    return "".join(blocks)


def _decay_opinion(
    gamma_risk: bool,
    decay_quality: float | None,
    pct_pnl: float | None,
    capital_efficiency: float | None,
    note: str | None = None,
) -> tuple[str, str]:
    """
    Close/hold opinion combining decay quality, gamma risk, and PnL — a
    genuinely different question from "is decay_quality high": a position can
    have 100% decay quality and still be near breakeven (nothing captured yet
    to attribute), or a fragile 10%-quality profit that's still fine to hold
    if it's nowhere near a normal profit target.

    Rule-of-thumb thresholds, not a formula — tune alongside gamma_risk's own
    (dte<=14, |delta|>=0.30) if these stop matching how you actually trade:
      - 50% profit captured is this trader's own established take-profit
        convention (referenced throughout the PLAN/REVIEW-driven analyses).
      - 5%/day capital efficiency is "very little value left to decay further"
        (see the LULU example: 2 DTE produced ~16.5%/day).
    """
    profit_target_hit = pct_pnl is not None and pct_pnl >= 50.0
    high_efficiency = capital_efficiency is not None and capital_efficiency >= 0.05

    if gamma_risk:
        if profit_target_hit:
            return ("Close", "Past the 50% profit target and gamma risk is elevated "
                              "this close to expiry — lock it in.")
        return ("Close/Roll", "Gamma risk is elevated (short DTE, near-the-money) "
                               "without a clear profit cushion to justify the convexity risk.")

    if decay_quality is None:
        why = f"decay quality unavailable — {note}" if note else "decay quality unavailable"
        if profit_target_hit:
            return ("Consider closing", f"Past the 50% profit target ({why}).")
        return ("Hold", f"No acute risk signal ({why}).")

    if decay_quality >= 0.70:
        if profit_target_hit or high_efficiency:
            return ("Close", "Profit is genuinely theta-driven and most of the available "
                              "decay looks captured — diminishing reward from holding further.")
        return ("Hold", "Profit is genuinely theta-driven and still building; no acute risk.")

    if decay_quality < 0.35:
        if profit_target_hit:
            return ("Hold (fragile)", "Profit looks good on paper but is mostly a directional "
                                       "move, not decay — it could reverse. Close now only if "
                                       "you want the directional risk off the table.")
        return ("Hold", "Early stage — not enough decay or profit captured yet "
                         "to have a strong opinion.")

    # mixed 0.35–0.70
    if profit_target_hit:
        return ("Consider closing", "Past the 50% profit target with mixed decay/directional quality.")
    return ("Hold", "Mixed profit quality (part decay, part directional) — no acute signal yet.")


def compute_decay_signals(
    pos: dict,
    dte: int | None,
    underlying: float | None,
    current_price: float | None,
    delta: float | None,
    gamma: float | None,
    opt_theta: float | None,
    opt_vega: float | None,
    current_iv: float | None,
    pct_pnl: float | None = None,
) -> dict:
    """
    Greeks-based decay-quality analysis for a short option position — replaces
    the flat "60% profit captured" rule of thumb with a P&L decomposition that
    distinguishes genuine time decay from a price move that could reverse.

    Decomposition method: sequential re-pricing ("waterfall" attribution) using
    greeks_pricing's BAW pricer, not a linear Taylor expansion off point Greeks —
    each step is an exact re-price along one dimension, so there's no truncation
    error to worry about getting the cross-terms (vanna, charm) exactly right in
    the additive breakdown. Standalone vanna/charm are still computed and
    returned as secondary risk flags, per the plan (their marginal value here is
    real but smaller than spot/time/vol attribution, and they're noisier on the
    thin, wide-spread small-cap chains several of these positions are on).

    Returns a dict; every field the entry snapshot or a live quote can't support
    is None — the UI renders that as "--", not a guess.
    """
    out = {
        "spot_component": None, "time_component": None, "vega_component": None,
        "residual": None, "decay_quality": None, "capital_efficiency": None,
        "dollar_gamma": None, "gamma_risk": False, "theta_per_day": None,
        "vanna": None, "charm": None, "note": None,
        "opinion": None, "opinion_reason": None,
    }

    net_qty = pos.get("net_qty") or 0
    qty = abs(net_qty)

    # Gamma-risk zone, theta/day, and capital efficiency only need CURRENT data —
    # compute regardless of whether an entry snapshot exists for this position.
    if gamma is not None and underlying:
        out["dollar_gamma"] = round(gamma * (underlying ** 2) / 100.0 * 100 * qty, 2)
    if delta is not None and dte is not None:
        out["gamma_risk"] = bool(dte <= 14 and abs(delta) >= 0.30)
    if opt_theta is not None and qty:
        out["theta_per_day"] = round(opt_theta * 100 * qty, 2)
    if out["theta_per_day"] is not None and current_price and qty:
        capital_at_risk = current_price * 100 * qty
        if capital_at_risk > 0:
            out["capital_efficiency"] = round(abs(out["theta_per_day"]) / capital_at_risk, 5)

    # Everything below either sets out["note"] and returns early, or runs to
    # completion and sets out["decay_quality"]. The opinion is computed exactly
    # once, in `finally`, from whatever state `out` is in at that point — this
    # way its message always matches the real reason decay_quality is None
    # (or isn't), instead of a "preliminary" guess taken before note was known.
    try:
        entry_iv = pos.get("entry_iv")
        entry_S  = pos.get("entry_underlying_price")
        if entry_iv is None or entry_S is None:
            out["note"] = "no entry snapshot captured for this position"
            return out

        strike      = pos.get("strike")
        expiry      = pos.get("expiry")
        option_type = (pos.get("option_type") or "").lower()
        avg_price   = pos.get("avg_price")
        if not (strike and expiry and option_type in ("call", "put")):
            out["note"] = "incomplete position data"
            return out
        if current_price is None or underlying is None or current_iv is None:
            out["note"] = "missing live quote"
            return out

        try:
            today = datetime.date.today()
            expiry_d = datetime.date.fromisoformat(expiry)
            T_now = max((expiry_d - today).days, 0) / 365.0
        except (ValueError, TypeError):
            out["note"] = "bad expiry"
            return out
        if T_now <= 0:
            out["note"] = "expired"
            return out

        entry_dt_str = (pos.get("entry_snapshot_at") or pos.get("entry_date") or "")[:10]
        try:
            entry_d = datetime.date.fromisoformat(entry_dt_str)
            T_entry = max((expiry_d - entry_d).days, 1) / 365.0
        except (ValueError, TypeError):
            out["note"] = "bad entry date"
            return out

        is_call = option_type == "call"
        q = greeks_pricing.dividend_yield_from_quarterly(pos.get("dividend_amount"), underlying)

        try:
            V0 = greeks_pricing.price(entry_S, strike, T_entry, entry_iv, is_call, q=q)     # entry, theoretical
            V1 = greeks_pricing.price(underlying, strike, T_entry, entry_iv, is_call, q=q)  # + spot moves
            V2 = greeks_pricing.price(underlying, strike, T_now, entry_iv, is_call, q=q)    # + time passes
            V3 = greeks_pricing.price(underlying, strike, T_now, current_iv, is_call, q=q)  # + vol moves (== current theoretical)
        except (ValueError, OverflowError, ZeroDivisionError):
            out["note"] = "pricing error"
            return out

        spot_component = V1 - V0
        time_component = V2 - V1
        vega_component = V3 - V2
        residual       = current_price - V3   # actual vs theoretical — bid/ask noise, model gap

        out["spot_component"] = round(spot_component, 4)
        out["time_component"] = round(time_component, 4)
        out["vega_component"] = round(vega_component, 4)
        out["residual"]       = round(residual, 4)

        # Decay quality: what share of the position's total favorable move (price
        # cheapening, good for a short seller) is attributable to time decay
        # specifically, vs a spot/vol move that could reverse. 0 whenever there's
        # no favorable move yet to attribute, OR the move exists but time_component
        # isn't negative (e.g. a brand-new position with almost no elapsed time —
        # nothing to credit to decay yet) — both are real "0" answers, not missing
        # data, so this must not leave decay_quality as None.
        total_move = (current_price - avg_price) if avg_price is not None else (V3 - V0) + residual
        price_drop = -total_move
        if price_drop > 0 and time_component < 0:
            out["decay_quality"] = round(min(1.0, max(0.0, (-time_component) / price_drop)), 3)
        else:
            out["decay_quality"] = 0.0

        try:
            g = greeks_pricing.greeks(underlying, strike, T_now, current_iv, is_call, q=q)
            out["vanna"] = round(g["vanna"], 5) if g.get("vanna") is not None else None
            out["charm"] = round(g["charm"], 5) if g.get("charm") is not None else None
        except Exception:
            pass

        return out
    finally:
        out["opinion"], out["opinion_reason"] = _decay_opinion(
            out["gamma_risk"], out["decay_quality"], pct_pnl, out["capital_efficiency"], out["note"]
        )

    return out


def get_eval_data(
    token: str,
    account_id: str,
    ticker: str | None = None,
    verbose: bool = True,
) -> dict:
    """
    Fetch live market data for every open position and evaluate risk criteria.
    Returns a dict with keys:
        positions     : list[dict]  — ALL active positions, each enriched with
                        dte, delta, underlying, atr, buffer, current_price,
                        abs_pnl, pct_pnl, reasons (list[str]), flagged (bool)
        skipped       : list[str]   — positions skipped (expired / bad data)
        flagged_count : int
        total_count   : int         — total from DB (including skipped)
        vix           : float | None
        fetched_at    : str         — ISO-8601 timestamp
    """
    import datetime

    def _log(msg: str, **kwargs) -> None:
        if verbose:
            print(msg, **kwargs)

    def _to_f(v: object) -> float | None:
        try:
            return float(v) if v not in (None, "") else None
        except (ValueError, TypeError):
            return None

    raw_positions = get_all_open_positions(ticker)
    if not raw_positions:
        return {
            "positions": [],
            "skipped": [],
            "flagged_count": 0,
            "total_count": 0,
            "vix": get_vix(),
            "fetched_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }

    today = datetime.date.today()
    unique_tickers = {pos["symbol"].upper() for pos in raw_positions}
    _log(
        f"\nEvaluating {len(raw_positions)} open position(s) across "
        f"{len(unique_tickers)} ticker(s)..."
    )

    # Pre-filter expired / unparseable positions
    active: list[dict] = []
    skipped: list[str] = []
    for pos in raw_positions:
        sym    = pos["symbol"].upper()
        expiry = pos["expiry"]
        try:
            expiry_date = datetime.date.fromisoformat(expiry)
        except ValueError:
            skipped.append(
                f"{sym} {pos['option_type'].upper()} ${pos['strike']} {expiry}"
                f" — unparseable expiry date"
            )
            continue
        dte = (expiry_date - today).days
        if dte < 0:
            skipped.append(
                f"{sym} {pos['option_type'].upper()} ${pos['strike']} {expiry}"
                f" — expired {abs(dte)}d ago"
            )
            continue
        active.append({**pos, "dte": dte})

    if not active:
        return {
            "positions": [],
            "skipped": skipped,
            "flagged_count": 0,
            "total_count": len(raw_positions),
            "vix": get_vix(),
            "fetched_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }

    # ── Batch API calls: quotes (price) + greeks (delta) ─────────────────────
    osi_list = [
        build_osi_symbol(
            p["symbol"].upper(), p["expiry"], p["option_type"].upper(), float(p["strike"])
        )
        for p in active
    ]
    _log(f"  Fetching quotes for {len(active)} contract(s)...", end=" ", flush=True)
    quotes = get_option_quotes(token, account_id, active)
    _log(f"{len(quotes)} received.")
    _log(f"  Fetching greeks for {len(osi_list)} contract(s)...", end=" ", flush=True)
    greeks_data = get_option_greeks_batch(token, account_id, osi_list)
    _log(f"{len(greeks_data)} received.")
    # ── Per-ticker caches (underlying price + ATR + dividend) ─────────────────
    price_cache_local: dict[str, float | None] = {}
    atr_cache: dict[str, tuple[float | None, float | None]] = {}
    div_cache: dict[str, float | None] = {}  # ticker -> dividend amount as float

    result_positions: list[dict] = []

    for pos in active:
        sym      = pos["symbol"].upper()
        expiry   = pos["expiry"]
        dte      = pos["dte"]
        opt_type = pos["option_type"].upper()
        strike   = float(pos["strike"])

        # Underlying price
        if sym not in price_cache_local:
            price_cache_local[sym] = get_underlying_price(sym)
        underlying = price_cache_local[sym]

        # 14-day ATR buffer
        if sym not in atr_cache:
            atr_val = get_atr(sym, period=14)
            atr_cache[sym] = (atr_val, atr_val * 1.5 if atr_val else None)
        atr_val, buffer = atr_cache[sym]

        # Dividend amount (for early-assignment risk warning on calls)
        if sym not in div_cache:
            try:
                kd = get_key_dates(sym)
                raw_div = kd.get("dividend_amount", "N/A")
                div_cache[sym] = float(raw_div.lstrip("$")) if raw_div not in ("N/A", "", None) else None
            except Exception:
                div_cache[sym] = None
        dividend_amount = div_cache[sym]

        osi = build_osi_symbol(sym, expiry, opt_type, strike).replace(" ", "")

        # Greeks + IV from the greeks endpoint (gamma/theta/vega/IV needed for the
        # entry-vs-current decay decomposition; delta was already used elsewhere).
        greeks = greeks_data.get(osi, {})
        delta: float | None = _to_f(greeks.get("delta"))
        gamma: float | None = _to_f(greeks.get("gamma"))
        opt_theta: float | None = _to_f(greeks.get("theta"))
        opt_vega: float | None = _to_f(greeks.get("vega"))
        current_iv: float | None = _to_f(greeks.get("impliedVolatility"))
        iv_history.record_iv_snapshot(sym, current_iv)

        # Current price: prefer mid=(bid+ask)/2, then last
        quote = quotes.get(osi, {})
        current_price: float | None = None
        bid  = _to_f(quote.get("bid"))
        ask  = _to_f(quote.get("ask"))
        last = _to_f(quote.get("last"))
        if bid is not None and ask is not None:
            current_price = (bid + ask) / 2
        elif last is not None and last > 0:
            current_price = last

        # ── PnL ───────────────────────────────────────────────────────────────
        # net_qty > 0  →  net short  (collected premium; profit when price ↓)
        # net_qty < 0  →  net long   (paid premium;      profit when price ↑)
        avg_price: float | None = pos.get("avg_price")
        net_qty: int = pos.get("net_qty") or 0
        abs_pnl: float | None = None
        pct_pnl: float | None = None
        if current_price is not None and avg_price is not None and avg_price != 0:
            qty = abs(net_qty)
            # avg_price is signed: positive = credit received (short), negative =
            # debit paid (long) — see read_open_position(). For longs it's already
            # negative, so adding it (not subtracting) nets out the debit paid.
            abs_pnl = (
                (avg_price - current_price) if net_qty > 0 else (current_price + avg_price)
            ) * 100 * qty
            pct_pnl = abs_pnl / (abs(avg_price) * 100 * qty) * 100

        reasons: list[str] = []

        # ── Condition 1: DTE ≤ 21 ────────────────────────────────────────────
        if dte <= 21:
            reasons.append(f"DTE = {dte}  (≤ 21 days to expiry)")

        # ── Condition 2: |delta| ≥ 0.40 ──────────────────────────────────────
        if delta is not None and abs(delta) >= 0.40:
            reasons.append(f"delta = {delta:+.3f}  (|Δ| ≥ 0.40)")

        # ── Condition 3: ITM ──────────────────────────────────────────────────
        if underlying is not None:
            if opt_type == "CALL" and underlying > strike:
                reasons.append(f"ITM: u/l ${underlying:.2f} > call strike ${strike:.2f}")
            elif opt_type == "PUT" and underlying < strike:
                reasons.append(f"ITM: u/l ${underlying:.2f} < put strike ${strike:.2f}")

        # ── Condition 4: Near ATM (skip if already ITM) ──────────────────────
        if underlying is not None:
            _is_itm_c4 = (opt_type == "CALL" and underlying > strike) or (opt_type == "PUT" and underlying < strike)
            if not _is_itm_c4:
                pct_away = abs(underlying - strike) / strike * 100
                if pct_away <= ATM_THRESHOLD_PCT:
                    reasons.append(
                        f"Near ATM: u/l ${underlying:.2f} vs strike ${strike:.2f}"
                        f" ({pct_away:.1f}% away)"
                    )

        # ── Condition 5: Within 1.5× ATR buffer ──────────────────────────────
        if underlying is not None and buffer is not None:
            if opt_type == "PUT":
                gap = underlying - strike
                if gap < buffer:
                    reasons.append(
                        f"Within ATR buffer of ${buffer:.2f} (1.5 × ATR ${atr_val:.2f})"
                    )
            elif opt_type == "CALL":
                gap = strike - underlying
                if gap < buffer:
                    reasons.append(
                        f"Within ATR buffer of ${buffer:.2f} (1.5 × ATR ${atr_val:.2f})"
                    )

        # ── Condition 6: Dividend ≥ Extrinsic Value (early assignment risk) ──
        # Warn only when ITM and dividend >= extrinsic (i.e. little time value left)
        if current_price is not None and dividend_amount is not None and underlying is not None:
            _is_itm = (opt_type == "CALL" and underlying > strike) or (opt_type == "PUT" and underlying < strike)
            if _is_itm:
                if opt_type == "CALL":
                    _intrinsic = max(0.0, underlying - strike)
                else:
                    _intrinsic = max(0.0, strike - underlying)
                _extrinsic = max(0.0, current_price - _intrinsic)
                if dividend_amount >= _extrinsic:
                    reasons.append(
                        f"dividend (${dividend_amount:.2f}) ≥ extrinsic (${_extrinsic:.2f})"
                        f" — early assignment risk"
                    )

        spread_id = pos.get("spread_id")
        if spread_id is not None:
            chain = get_chain_net_cash(spread_id)
            chain_cash      = chain["net_cash"]     if chain["scoped_by"] != "none" else None
            sim_chain_cash  = chain["sim_net_cash"] if chain["scoped_by"] != "none" else None
            has_test_trade  = chain["has_test"]     if chain["scoped_by"] != "none" else False
        else:
            chain_cash     = None
            sim_chain_cash = None
            has_test_trade = False

        # Per-position hypo cash: this position's own trades (real + test).
        # Always check, even if there's no spread_id — handles standalone positions
        # with a test trade (e.g. HAL Jul with no roll chain).
        pos_id = pos.get("id")
        if pos_id is not None:
            hypo = get_pos_hypo_cash(pos_id)
            if hypo["has_test"]:
                pos_hypo_cash = hypo["pos_cash"]
                # For no-spread_id positions, sim_chain_cash falls back to pos-level
                if not has_test_trade:
                    has_test_trade = True
                    sim_chain_cash = hypo["pos_cash"]
            else:
                pos_hypo_cash = None
        else:
            pos_hypo_cash = None

        decay = compute_decay_signals(pos, dte, underlying, current_price, delta, gamma, opt_theta, opt_vega, current_iv, pct_pnl)

        result_positions.append({
            **pos,
            "dte": dte,
            "delta": delta,
            "gamma": gamma,
            "opt_theta": opt_theta,
            "opt_vega": opt_vega,
            "current_iv": current_iv,
            "underlying": underlying,
            "atr": atr_val,
            "buffer": buffer,
            "current_price": current_price,
            "abs_pnl": abs_pnl,
            "pct_pnl": pct_pnl,
            "chain_cash":     chain_cash,
            "sim_chain_cash": sim_chain_cash,
            "has_test_trade": has_test_trade,
            "pos_hypo_cash":  pos_hypo_cash,
            "reasons": reasons,
            "flagged": bool(reasons),
            "dividend_amount": dividend_amount,
            "decay": decay,
        })

    flagged_count = sum(1 for p in result_positions if p["flagged"])
    vix = get_vix()
    iv_history.record_iv_snapshot(iv_history.VIX_TICKER, vix)
    return {
        "positions": result_positions,
        "skipped": skipped,
        "flagged_count": flagged_count,
        "total_count": len(raw_positions),
        "vix": vix,
        "fetched_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }


def eval_open_positions(token: str, account_id: str, ticker: str | None = None) -> None:
    """CLI output for portfolio evaluation. Calls get_eval_data() and prints."""
    raw_positions = get_all_open_positions(ticker)
    if not raw_positions:
        label = f"{ticker} positions" if ticker else "positions"
        print_box([f"  No open {label} found in the journal."], title="  Portfolio Evaluation  ")
        return

    data = get_eval_data(token, account_id, ticker, verbose=True)
    positions   = data["positions"]
    skipped     = data["skipped"]
    flagged     = [p for p in positions if p["flagged"]]
    total_count = data["total_count"]

    if not positions:
        print_box(["  No active (non-expired) open positions found."], title="  Portfolio Evaluation  ")
        return

    # ── Output ────────────────────────────────────────────────────────────────
    if not flagged:
        print_box(
            ["  All open positions are within normal parameters. No action needed."],
            title="  Portfolio Evaluation — No Alerts  ",
        )
    else:
        lines: list[str] = [
            f"  {len(flagged)} of {total_count} position(s) flagged for review:",
            "",
        ]
        for item in flagged:
            lines.append(f"  {format_position(item)}")
            cp = item.get("current_price")
            dl = item.get("delta")
            ap = item.get("abs_pnl")
            pp = item.get("pct_pnl")
            info_parts = []
            if cp is not None:
                info_parts.append(f"Current price: ${cp:.2f}")
            if dl is not None:
                info_parts.append(f"Δ = {dl:+.3f}")
            if ap is not None and pp is not None:
                sign = "+" if ap >= 0 else ""
                info_parts.append(f"PnL = {sign}${ap:,.2f}  ({sign}{pp:.1f}%)")
            if info_parts:
                lines.append("  " + "  |  ".join(info_parts))
            for r in item["reasons"]:
                lines.append(f"      >> {r}")
            lines.append("")
        while lines and not lines[-1].strip():
            lines.pop()
        print_box(lines, title=f"  Portfolio Evaluation — {len(flagged)} Alert(s)  ")

    if skipped:
        print_box(
            [f"  {s}" for s in skipped],
            title="  Skipped Positions  ",
        )


def _parse_recommended_option(
    text: str, chain_rows: list[dict], prefer_type: str | None = None,
    trust_any_date: bool = False,
) -> dict | None:
    """
    Try to extract the recommended strike and expiry from NotebookLM response text
    and return the matching chain row, or None if not found.

    prefer_type ("CALL"/"PUT"), when given, makes a wrong-type row effectively
    disqualifying during matching — without it, a same-strike/same-expiry row of
    the OTHER type can win purely because it happens to appear earlier in
    chain_rows (calls are listed before puts), which is a real option contract
    but not the one that was actually recommended.

    trust_any_date: only set True when `text` is already scoped to a single,
    specific sentence (e.g. the STO line) where any date mentioned is almost
    certainly the intended expiry. When False (the default, used for
    whole-text scans prone to picking up prose dates like "as of July 9,
    2026"), a date that isn't one of chain_rows' actual expiries is discarded
    and target_expiry falls back to None — which then makes the matching loop
    below treat EVERY row's expiry as an acceptable match. That's fine when no
    expiry was stated at all, but if a specific expiry WAS stated and just
    isn't in chain_rows (e.g. its chain fetch got rate-limited), discarding it
    silently lets a same-strike row at a completely different, wrong expiry
    win by strike alone (confirmed on a real MSFT roll: stated "Aug 21 2026"
    wasn't in the fetched chain, so a 400-strike row expiring 2026-07-10 won
    instead and got shown as a "confirmed" price for the wrong contract).
    """
    # Extract strike: $12.50, $12, 12.50 strike, strike of 12.50, etc.
    strike_match = re.search(
        r'\$\s*(\d+(?:\.\d+)?)\s*strike'
        r'|strike\s+(?:of\s+|price\s+(?:of\s+)?)?\$?\s*(\d+(?:\.\d+)?)'
        r'|\$\s*(\d+(?:\.\d+)?)\s+(?:put|call|strike)'
        r'|\b(\d+(?:\.\d+)?)\s+(?:calls?|puts?)\b',
        text, re.IGNORECASE
    )
    if not strike_match:
        return None
    strike_str = next(g for g in strike_match.groups() if g is not None)
    try:
        target_strike = float(strike_str)
    except ValueError:
        return None

    # Extract expiry: only accept dates that exist as actual expiries in the chain.
    # This prevents prose dates ("as of June 9, 2026") from being used as the expiry.
    _valid_expiries = {r.get("expiry") for r in chain_rows if r.get("expiry")}
    _DATE_PAT = (
        r'(\d{4}-\d{2}-\d{2})'
        r'|(\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?'
        r'|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
        r'\s+\d{1,2},?\s+\d{4})'
    )
    target_expiry = None
    _first_parsed_date = None  # first date found, even if not a known chain expiry
    for _dm in re.finditer(_DATE_PAT, text, re.IGNORECASE):
        _raw = next((g for g in _dm.groups() if g), None)
        if not _raw:
            continue
        for _fmt in ("%Y-%m-%d", "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"):
            try:
                _parsed = datetime.datetime.strptime(_raw.replace(",", ", ").strip(), _fmt).date()
                _parsed_str = _parsed.strftime("%Y-%m-%d")
                if _first_parsed_date is None:
                    _first_parsed_date = _parsed_str
                if _parsed_str in _valid_expiries:
                    target_expiry = _parsed_str
                    break
            except ValueError:
                continue
        if target_expiry:
            break
    if target_expiry is None and trust_any_date and _first_parsed_date is not None:
        target_expiry = _first_parsed_date

    # Find best matching row: must match strike exactly; prefer exact expiry too;
    # a type mismatch (when prefer_type is given) is effectively disqualifying.
    best = None
    best_dist = float("inf")
    for row in chain_rows:
        try:
            row_strike = float(row.get("strike") or 0)
        except (TypeError, ValueError):
            continue
        dist = abs(row_strike - target_strike)
        expiry_match = (target_expiry is None or row.get("expiry") == target_expiry)
        type_match = (prefer_type is None or str(row.get("option_type", "")).upper() == prefer_type.upper())
        score = dist + (0 if expiry_match else 1000) + (0 if type_match else 10000)
        if score < best_dist:
            best_dist = score
            best = row

    # If recommended strike isn't close to any chain row (or wrong expiry),
    # synthesize a row with the exact values from the text.
    # Threshold: >$10 away means the chain is missing that strike (often the API
    # truncates far-OTM contracts), so don't fall back to the "closest" wrong strike.
    _STRIKE_THRESHOLD = 10.0
    if best is None or best_dist >= _STRIKE_THRESHOLD:
        if target_strike:
            # If NLM didn't mention an expiry, pick the chain expiry closest to 30 DTE
            if not target_expiry and chain_rows:
                import datetime as _dt2
                _today2 = _dt2.date.today()
                _best_exp, _best_dte_dist = None, float("inf")
                for _r in chain_rows:
                    _exp = _r.get("expiry")
                    if _exp:
                        try:
                            _d = abs((_dt2.date.fromisoformat(_exp) - _today2).days - 30)
                            if _d < _best_dte_dist:
                                _best_dte_dist, _best_exp = _d, _exp
                        except ValueError:
                            pass
                target_expiry = _best_exp
        if target_strike and target_expiry:
            # Extract price from text: @ $X.XX or premium of $X.XX
            price_match = re.search(
                r'(?:@|premium\s+of|price\s+of|at)\s*\$?\s*(\d+(?:\.\d+)?)',
                text, re.IGNORECASE
            )
            opt_price = price_match.group(1) if price_match else None
            # Use ul_price from chain if available
            ul_price = chain_rows[0].get("ul_price") if chain_rows else None
            opt_type = prefer_type or (chain_rows[0].get("option_type", "CALL") if chain_rows else "CALL")
            try:
                today = datetime.date.today()
                dte = (datetime.date.fromisoformat(target_expiry) - today).days
            except ValueError:
                dte = None
            return {
                "symbol":      chain_rows[0].get("symbol", "") if chain_rows else "",
                "option_type": opt_type,
                "strike":      str(target_strike),
                "expiry":      target_expiry,
                "side":        "Short",
                "dte":         dte,
                "delta":       None,
                "ul_price":    ul_price,
                "opt_price":   opt_price,
                "_synthesized": True,
            }

    return best


def _parse_ideal_entry(text: str) -> str | None:
    """
    Extract a short 'when to enter' note from NotebookLM response text.
    Returns something like 'Mon/Tue AM – low IV' or 'Thu PM – pre-div' or None.
    """
    # Day abbreviations
    DAY_MAP = {
        "monday": "Mon", "tuesday": "Tue", "wednesday": "Wed",
        "thursday": "Thu", "friday": "Fri",
        "mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu", "fri": "Fri",
    }
    # Time-of-day
    TIME_MAP = {
        r'\bmorning\b': "AM", r'\bopen(?:ing)?\b': "open",
        r'\bafternoon\b': "PM", r'\bclose\b': "close",
        r'\bend\s+of\s+(?:the\s+)?(?:trading\s+)?day\b': "EOD",
        r'\bmid.?day\b': "mid",
    }

    # Look for sentences containing entry timing cues
    timing_sentences = []
    for sent in re.split(r'(?<=[.!?])\s+', text):
        low = sent.lower()
        if re.search(r'enter|sell|open\s+(?:the\s+)?(?:position|trade)|initiat|place\s+(?:the\s+)?trade', low):
            timing_sentences.append(sent)
    if not timing_sentences:
        # Broaden: any sentence with a day of week
        for sent in re.split(r'(?<=[.!?])\s+', text):
            if re.search(r'\b(?:mon|tue|wed|thu|fri)(?:day)?\b', sent, re.IGNORECASE):
                timing_sentences.append(sent)

    target = ' '.join(timing_sentences) if timing_sentences else text

    # Extract days
    days_found = []
    for word, abbr in DAY_MAP.items():
        if re.search(r'\b' + word + r'\b', target, re.IGNORECASE):
            if abbr not in days_found:
                days_found.append(abbr)
    # Keep day order Mon→Fri
    order = ["Mon","Tue","Wed","Thu","Fri"]
    days_found = [d for d in order if d in days_found]

    # Extract time of day
    time_found = None
    for pattern, label in TIME_MAP.items():
        if re.search(pattern, target, re.IGNORECASE):
            time_found = label
            break

    # Extract a short reason (IV, earnings, dividend, volatility spike, etc.)
    reason = None
    reason_patterns = [
        (r'\blow\s+(?:implied\s+)?(?:volatility|IV)\b', "low IV"),
        (r'\bhigh\s+(?:implied\s+)?(?:volatility|IV)\b', "high IV"),
        (r'\bIV\s+(?:spike|crush|expansion)\b', "IV spike"),
        (r'\bearning(?:s)?\b', "pre-earn"),
        (r'\bdividend\b', "pre-div"),
        (r'\bex.?div\b', "ex-div"),
        (r'\bvolatility\s+(?:spike|surge|expansion)\b', "vol spike"),
        (r'\bpre.?market\b', "pre-mkt"),
        (r'\bafter.?(?:hours|market)\b', "AH"),
        (r'\bweekly\s+(?:high|low)\b', "wkly lvl"),
    ]
    for pat, label in reason_patterns:
        if re.search(pat, target, re.IGNORECASE):
            reason = label
            break

    # Compose result
    parts = []
    if days_found:
        parts.append("/".join(days_found))
    if time_found:
        parts.append(time_found)
    timing = " ".join(parts) if parts else None

    if timing and reason:
        return f"{timing} – {reason}"
    if timing:
        return timing
    if reason:
        return reason
    return None


_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"


def _detect_recommendation_llm(text: str) -> str | None:
    """
    Classify the CURRENT recommendation in a NotebookLM analysis via Claude
    Haiku instead of the regex/word-count heuristic below. The regex approach
    reliably misfires on this text shape: the "Execution Instructions" section
    always discusses a *future*, contingent roll plan (e.g. "at 21 DTE,
    evaluate a roll") even when today's verdict is DO NOTHING/WATCH, and a
    naive roll-word count can't tell that apart from an active roll
    instruction (confirmed on a real MSFT analysis: "DO NOTHING (WATCH)" today,
    with a contingent roll plan for 21 DTE, misclassified as ROLL).

    Returns None — not a guess — if the API key is missing or the call fails,
    so the caller falls back to the regex heuristic instead of trusting a bad
    read.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    import requests
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
                "max_tokens": 8,
                "system": (
                    "You classify options-trading analysis text into exactly one "
                    "of: ROLL, HOLD, ASSIGNMENT. Classify the CURRENT, immediate "
                    "recommendation only — ignore any future/contingent plan "
                    "described for a later date (e.g. \"at 21 DTE, evaluate a "
                    "roll\" is NOT a current ROLL recommendation if today's verdict "
                    "is to do nothing/watch/hold). ASSIGNMENT means the advice is "
                    "to accept/allow assignment rather than act. Reply with "
                    "exactly one word: ROLL, HOLD, or ASSIGNMENT. No other text."
                ),
                "messages": [{"role": "user", "content": text[:6000]}],
            },
            timeout=15,
        )
        resp.raise_for_status()
        content = resp.json().get("content", [])
        reply = (content[0].get("text", "") if content else "").strip().upper()
        if reply in ("ROLL", "HOLD", "ASSIGNMENT"):
            return reply
        log.warning("[llm-classify] unexpected reply %r, falling back to regex", reply)
    except Exception as exc:
        log.warning("[llm-classify] failed, falling back to regex: %s", exc)
    return None


def _detect_unborn_action_llm(text: str) -> str | None:
    """
    Classify whether an "unborn" (no existing position yet) covered-call/CSP
    analysis recommends opening a new position or doing nothing this cycle.

    Deliberately separate from _detect_recommendation_llm: that one's
    ROLL/HOLD/ASSIGNMENT vocabulary is for managing an EXISTING position and
    has no accurate bucket for "sell this new contract to open" — both it and
    its regex fallback default to HOLD whenever the text doesn't mention
    "roll" or "assignment", which describes essentially every unborn
    recommendation regardless of whether it's actually telling you to sell
    something (confirmed on real UAMY and USAR analyses that named a specific
    strike/expiry/premium to sell and still got classified HOLD → badged
    "DO NOTHING").

    Returns None — not a guess — if the API key is missing or the call fails,
    so the caller falls back to the existing regex "do nothing" heuristic.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    import requests
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
                "max_tokens": 8,
                "system": (
                    "You classify options-trading analysis text about whether to "
                    "open a NEW covered call or cash-secured put position — there "
                    "is no existing position yet, so this is purely about entering "
                    "one. Reply with exactly one word: SELL if the text recommends "
                    "a specific contract (strike/expiry/premium) to sell/open right "
                    "now, or WAIT if it recommends not opening a position this "
                    "cycle (e.g. \"do nothing\", \"wait for a better setup\", no "
                    "candidate met the criteria). No other text."
                ),
                "messages": [{"role": "user", "content": text[:6000]}],
            },
            timeout=15,
        )
        resp.raise_for_status()
        content = resp.json().get("content", [])
        reply = (content[0].get("text", "") if content else "").strip().upper()
        if reply in ("SELL", "WAIT"):
            return reply
        log.warning("[llm-classify-unborn] unexpected reply %r, falling back to regex", reply)
    except Exception as exc:
        log.warning("[llm-classify-unborn] failed, falling back to regex: %s", exc)
    return None


def _detect_recommendation(text: str) -> str:
    """Classify a NotebookLM ROLL response as ROLL, HOLD, or ASSIGNMENT.

    Tries the LLM classifier first (see _detect_recommendation_llm); falls
    back to the regex/word-count heuristic below if the API key is missing or
    the call fails, so this never blocks on the network being unavailable.
    """
    llm_result = _detect_recommendation_llm(text)
    if llm_result is not None:
        return llm_result
    return _detect_recommendation_regex(text)


def _detect_recommendation_regex(text: str) -> str:
    """Parse a NotebookLM ROLL response and return ROLL, HOLD, or ASSIGNMENT.

    Priority order:
      1. Explicit assignment language anywhere
      2. First "primary recommendation" / bold heading in the text
      3. First "recommendation/strategy/action:" heading
      4. Negative-roll signals near the top (do not roll, no need to roll, …)
      5. Weighted word-count fallback (HOLD wins ties; "roll" mentions inside
         secondary/alternative sections are discounted)
    """
    low = text.lower()

    # ── 1. Assignment always wins ────────────────────────────────────────────
    if re.search(
        r"accept.{0,20}assignment|take.{0,20}assignment"
        r"|let.{0,10}assign|allow.{0,10}assignment",
        low
    ):
        return "ASSIGNMENT"

    # ── 2. Explicit "primary recommendation" label ───────────────────────────
    primary_match = re.search(
        r"primary\s+recommendation[:\s\*]+([^\n.]{1,80})", low
    )
    if primary_match:
        snippet = primary_match.group(1)
        if re.search(r"\bhold\b|\bdo nothing\b|\bno action\b|\bno roll\b", snippet):
            return "HOLD"
        if re.search(r"\broll\b", snippet):
            return "ROLL"
        if re.search(r"\bassignment\b", snippet):
            return "ASSIGNMENT"

    # ── 3. First bold/heading "recommendation / strategy / action" line ──────
    first_heading_match = re.search(
        r"(?:\*{1,2})?(?:recommendation|strategy|action|verdict)"
        r"(?:\*{1,2})?[:\s\*]+([^\n.]{1,120})",
        low
    )
    if first_heading_match:
        snippet = first_heading_match.group(1)
        # Strip stars/punctuation
        snippet = re.sub(r"[\*#]", "", snippet).strip()
        if re.search(r"\bhold\b|\bdo nothing\b|\bno action\b|\bno roll\b", snippet):
            return "HOLD"
        if re.search(r"\broll\b", snippet):
            return "ROLL"
        if re.search(r"\bassignment\b", snippet):
            return "ASSIGNMENT"

    # ── 4. Negative-roll phrases near the top (first 600 chars) ─────────────
    top = low[:600]
    if re.search(
        r"\bdo not roll\b|\bshould not roll\b|\bno need to roll\b"
        r"|\bnot roll\b|\bavoid rolling\b|\bno roll\b|\bdo nothing\b",
        top
    ):
        return "HOLD"

    # ── 5. Weighted word-count fallback ─────────────────────────────────────
    # Discount "roll" mentions that appear inside secondary/alternative clauses
    # by removing those sub-sentences before counting.
    # Patterns like "alternatively … roll", "secondary … roll", "if you want to roll"
    cleaned = re.sub(
        r"(?:alternatively|secondary\s+option|as\s+an?\s+alternative"
        r"|you\s+could\s+also|if\s+you\s+(?:prefer|want|choose)\s+to\s+roll"
        r"|consider\s+rolling)[^.!?\n]{0,200}",
        " ",
        low
    )
    strong_hold = len(re.findall(
        r"\bhold\s+(?:to\s+)?expir|\blet\s+it\s+expir|\bno\s+action\b|\bdo\s+nothing\b"
        r"|\bhold\s+(?:the\s+)?position\b|\bhold\s+(?:the\s+)?trade\b",
        cleaned
    ))
    roll_n   = len(re.findall(r"\broll(?:ing|ed)?\b", cleaned))
    do_n     = len(re.findall(r"\bdo nothing\b|\bhold\b", cleaned)) + strong_hold * 2
    assign_n = len(re.findall(r"\bassignment\b", cleaned))

    if assign_n > roll_n and assign_n > do_n:
        return "ASSIGNMENT"
    if roll_n > do_n:   # strictly greater — ties go to HOLD
        return "ROLL"
    return "HOLD"


async def run_roll_for_position(
    token: str,
    account_id: str,
    ticker: str,
    open_positions: list[dict],
    notebook_id: str,
    pos_key: str | None = None,
    max_days_out: int = 90,
) -> dict:
    """
    Full ROLL workflow for one position (web mode):
      1. Fetch & write option chain CSV
      2. Upload CSV to NotebookLM
      3. Get key dates + VIX
      4. Query NotebookLM with ROLL strategy (silent — no terminal output)
    Returns dict: {recommendation, text, ticker, error}
    """
    try:
        all_expirations = get_expirations(token, account_id, ticker)
        if not all_expirations:
            return {
                "error": f"No option expirations found for {ticker} on Public.com — "
                         "the ticker may not support options or may not be available in your account.",
                "recommendation": "HOLD",
                "text": "",
                "ticker": ticker,
            }
        # Include every expiration within max_days_out, not a fixed count — heavily
        # optioned names (MSFT, weeklies) can have 10+ expirations inside a single
        # month, while thin names have only a handful in 3 months. Cap at 40 as a
        # sanity ceiling against pathological (e.g. daily-expiry) chains.
        _cutoff = datetime.date.today() + datetime.timedelta(days=max_days_out)
        def _parse_exp(_e: str) -> datetime.date | None:
            try:
                return datetime.date.fromisoformat(_e)
            except (ValueError, TypeError):
                return None
        expirations = [
            e for e in all_expirations
            if (_d := _parse_exp(e)) is not None and _d <= _cutoff
        ][:40] or all_expirations[:10]

        all_rows: list[dict] = []
        for exp_date in expirations:
            chain = get_option_chain(token, account_id, ticker, exp_date)
            if not chain:
                continue
            for contract in chain.get("calls", []):
                all_rows.append(contract_to_row(contract, "CALL", exp_date))
            for contract in chain.get("puts", []):
                all_rows.append(contract_to_row(contract, "PUT", exp_date))

        if not all_rows:
            return {
                "error": f"Option chain returned no contracts for {ticker} — "
                         "data may be unavailable or markets may be closed.",
                "recommendation": "HOLD",
                "text": "",
                "ticker": ticker,
            }

        # Compact candidate-strike summary from this SAME chain fetch, for
        # Claude's comparison opinion (claude_advisor.py) — reuses all_rows
        # rather than a second Public.com hit. Current leg's own type, parsed
        # straight from pos_key (cheap, no need to wait for matched_pos below).
        chain_candidates_text = None
        if pos_key:
            _pk_parts = pos_key.split("|")
            if len(_pk_parts) == 4:
                chain_candidates_text = claude_advisor.build_chain_candidates_text(
                    all_rows, _pk_parts[1].upper(), current_expiry=_pk_parts[3]
                )

        key_dates = get_key_dates(ticker)
        vix = get_vix()

        # Resolve spread_id and ul_cost_basis from the matched open position BEFORE querying,
        # so the cost basis context is included in the prompt.
        # Strike is a float in the DB but a bare number string in the JS pos_key,
        # so compare as floats to avoid "5.0" != "5" mismatches.
        spread_id    = None
        matched_pos  = None
        ul_cost_basis = 0.0
        if pos_key:
            pk_parts = pos_key.split("|")  # symbol|option_type|strike|expiry
            pk_sym    = pk_parts[0].upper()  if len(pk_parts) > 0 else ""
            pk_type   = pk_parts[1].upper()  if len(pk_parts) > 1 else ""
            pk_expiry = pk_parts[3]          if len(pk_parts) > 3 else ""
            try:
                pk_strike = float(pk_parts[2]) if len(pk_parts) > 2 else None
            except ValueError:
                pk_strike = None
            for p in open_positions:
                try:
                    p_strike = float(p.get("strike", ""))
                except (TypeError, ValueError):
                    p_strike = None
                if (str(p.get("symbol","")).upper() == pk_sym
                        and str(p.get("option_type","")).upper() == pk_type
                        and p_strike == pk_strike
                        and str(p.get("expiry","")) == pk_expiry):
                    spread_id     = p.get("spread_id")
                    matched_pos   = p
                    try:
                        ul_cost_basis = float(p.get("ul_cost_basis") or 0)
                    except (TypeError, ValueError):
                        ul_cost_basis = 0.0
                    log.debug("Matched pos_key=%r → spread_id=%r ul_cost_basis=%.2f",
                              pos_key, spread_id, ul_cost_basis)
                    break
            else:
                log.warning("No position matched pos_key=%r in open_positions", pos_key)

        chain = get_chain_net_cash(spread_id, fallback_pos_id=matched_pos.get("id") if matched_pos else None)
        current_leg_price = float(matched_pos["current_price"]) if matched_pos and matched_pos.get("current_price") is not None else None

        # Claude is the primary advisor (see claude_advisor.py) — build the
        # same rich position context /api/openai-compare uses for the second
        # opinion (live Greeks, decay signals, ATR/IV, dte, reasons), rather
        # than the bare DB row in matched_pos, so the primary analysis has
        # full data to reason over.
        eval_data = get_eval_data(token, account_id, ticker=ticker, verbose=False)
        claude_pos = None
        for _p in eval_data.get("positions", []):
            try:
                if (str(_p.get("option_type", "")).upper() == pk_type
                        and float(_p.get("strike") or -1) == pk_strike
                        and str(_p.get("expiry")) == pk_expiry):
                    claude_pos = _p
                    break
            except (TypeError, ValueError):
                continue
        if claude_pos is None:
            return {"error": f"Position not found in live eval data for {ticker}", "recommendation": "HOLD", "text": "", "ticker": ticker}

        claude_context = claude_advisor.build_position_context(claude_pos, eval_data.get("vix"), key_dates)
        claude_result = claude_advisor.query_claude_advisor(claude_context, chain_candidates_text)
        if claude_result.get("error"):
            return {"error": claude_result["error"], "recommendation": "HOLD", "text": "", "ticker": ticker}
        text = claude_result["text"]
        tail_text = claude_result.get("tail_text")

        # Expected PnL: close current position at 40% of sale price (keep 60%)
        pos_avg_price = float(matched_pos["avg_price"]) if matched_pos and matched_pos.get("avg_price") is not None else None
        pos_qty       = abs(int(matched_pos["net_qty"])) if matched_pos and matched_pos.get("net_qty") is not None else None

        rec = claude_result.get("recommendation") or "HOLD"

        # Confirmed BTC/STO prices from the exact option chain snapshot uploaded to
        # NotebookLM — used below for the hard PnL guard, and returned so the
        # Execution Instructions can show verified prices next to Action 1/2
        # instead of whatever the LLM estimated (it sometimes pulls a stale-ish
        # number from PLAN/REVIEW prose rather than reading the uploaded chain).
        btc_chain_price = sto_chain_price = None
        sto_chain_desc  = None
        projected_roll_pnl = None
        roll_btc_cost = roll_sto_credit = roll_close_cost = roll_close_price = None
        if rec == "ROLL" and pos_qty and matched_pos:
            _adapted_rows = [
                {
                    "strike": r["strike_price"], "expiry": r["expiration_date"],
                    "option_type": r["option_type"], "symbol": ticker,
                    "mid_price": r.get("mid_price"), "last": r.get("last"),
                }
                for r in all_rows
            ]

            # Current (BTC) leg's price from the same chain snapshot.
            try:
                _cur_strike = float(matched_pos.get("strike"))
            except (TypeError, ValueError):
                _cur_strike = None
            _cur_type   = str(matched_pos.get("option_type", "")).upper()
            _cur_expiry = matched_pos.get("expiry")
            for _r in _adapted_rows:
                try:
                    _r_strike = float(_r.get("strike") or 0)
                except (TypeError, ValueError):
                    continue
                if (_r.get("option_type") == _cur_type and _r_strike == _cur_strike
                        and _r.get("expiry") == _cur_expiry):
                    try:
                        btc_chain_price = float(_r.get("mid_price") or _r.get("last") or 0) or None
                    except (TypeError, ValueError):
                        btc_chain_price = None
                    break

            # New (STO) leg's price — whatever NotebookLM recommended, matched
            # against the same chain snapshot.
            #
            # Execution Instructions consistently list "1. BTC ... 2. STO ..."
            # in that order, and the BTC line always restates the CURRENT leg's
            # exact strike/expiry verbatim (e.g. "BTC 4x LULU 115.00 Put exp
            # 2026-07-10") before the STO line ever states the actual new one.
            # A naive first-match search over the whole text reliably grabs the
            # BTC line's strike instead — not just when it happens to produce an
            # exact current-leg match (the old guard below only caught that
            # narrow case), but whenever chain-matching then resolves to some
            # OTHER type/expiry at that same wrong strike, which looks distinct
            # from the current leg without actually being the recommendation.
            # Scope the search to the STO/"sell to open" sentence first so this
            # can't happen. There can be more than one "STO" mention (e.g. a
            # summary line like "(simultaneous BTC/STO)" before the actual
            # detailed one) — try each candidate in turn and keep the first
            # that actually yields a strike, rather than just the first mention.
            _rec_opt = None
            _sto_snippet = None
            for _sto_m in re.finditer(r'(?:sell\s+to\s+open|\bSTO\b)[^\n]{0,150}', text, re.IGNORECASE):
                _candidate = _parse_recommended_option(_sto_m.group(0), _adapted_rows, trust_any_date=True)
                if _candidate and _candidate.get("strike") is not None:
                    _rec_opt = _candidate
                    _sto_snippet = _sto_m.group(0)
                    break

            if _rec_opt and _sto_snippet:
                # The snippet almost always states the type explicitly ("...105.00
                # Put exp..."); re-run matching with that as a hard preference so a
                # same-strike/same-expiry row of the OTHER type can't win the tie
                # (chain rows list calls before puts, so it otherwise would).
                # Rolls also essentially never change instrument type, so the
                # current leg's own type is a solid fallback if the snippet
                # doesn't spell it out.
                _type_m = re.search(r'\b(call|put)s?\b', _sto_snippet, re.IGNORECASE)
                _prefer_type = _type_m.group(1).upper() if _type_m else _cur_type
                _rec_opt = _parse_recommended_option(
                    _sto_snippet, _adapted_rows, prefer_type=_prefer_type, trust_any_date=True
                ) or _rec_opt

            if not _rec_opt:
                _rec_opt = _parse_recommended_option(text, _adapted_rows, prefer_type=_cur_type)
            # Still landed on the current leg's own strike (whole-text fallback
            # only) — strip those mentions and retry so it lands on the next
            # distinct strike/expiry actually being recommended.
            if (_rec_opt and not _sto_snippet and _cur_strike is not None
                    and str(_rec_opt.get("option_type", "")).upper() == _cur_type
                    and _rec_opt.get("expiry") == _cur_expiry):
                try:
                    _same_strike = float(_rec_opt.get("strike") or -1) == _cur_strike
                except (TypeError, ValueError):
                    _same_strike = False
                if _same_strike:
                    _masked_text = re.sub(
                        rf'{_cur_strike:g}(\.0+)?\s*(?:CALL|PUT)', '', text, flags=re.IGNORECASE
                    )
                    if _cur_expiry:
                        _masked_text = _masked_text.replace(_cur_expiry, '')
                    _rec_opt = _parse_recommended_option(_masked_text, _adapted_rows, prefer_type=_cur_type)
                    # Still landed on the current leg (or nothing distinct found) —
                    # there's no genuinely new leg to confirm a price for.
                    if (_rec_opt and str(_rec_opt.get("option_type", "")).upper() == _cur_type
                            and _rec_opt.get("expiry") == _cur_expiry):
                        try:
                            if float(_rec_opt.get("strike") or -1) == _cur_strike:
                                _rec_opt = None
                        except (TypeError, ValueError):
                            _rec_opt = None
            if _rec_opt:
                try:
                    sto_chain_price = float(
                        _rec_opt.get("mid_price") or _rec_opt.get("last")
                        or _rec_opt.get("opt_price") or 0
                    ) or None
                except (TypeError, ValueError):
                    sto_chain_price = None
                if sto_chain_price:
                    sto_chain_desc = (
                        f"{ticker} {_rec_opt.get('strike')} "
                        f"{str(_rec_opt.get('option_type','')).upper()} "
                        f"exp {_rec_opt.get('expiry')}"
                    )

            # Full projected roll-chain PnL: net cash to date, minus the real cost
            # to BTC the current leg, plus the new leg's STO credit, minus the cost
            # to close that new leg at 40% of its own premium (60% profit capture) —
            # i.e. "if I execute this exact roll and then eventually take my usual
            # profit on the new leg, is the WHOLE chain profitable." Computed from
            # the same verified chain-snapshot prices as the Confirmed Prices line,
            # not from NotebookLM's own (previously unreliable) arithmetic. This also
            # doubles as the hard PnL guard: a ROLL recommendation that fails this
            # check gets forced to HOLD.
            #
            # Commission/fees on the three hypothetical legs (BTC current, STO new,
            # eventual BTC of new at 40%) are estimated via the journal's own
            # Fidelity fee schedule (auto_comm/auto_fees) so this lines up with what
            # the journal will actually record once the trades are entered for real
            # — chain["net_cash"] already has the real leg's fees baked in, so only
            # these three projected legs need it added here.
            if sto_chain_price:
                _btc_price = btc_chain_price if btc_chain_price is not None else current_leg_price
                _btc_price = _btc_price or 0.0
                roll_btc_cost = round(
                    _btc_price * 100 * pos_qty
                    + auto_comm("buy", _btc_price, pos_qty, is_close=True)
                    + auto_fees("buy", _btc_price, pos_qty, ticker),
                    2,
                )
                _adj_net = round(chain["net_cash"] - roll_btc_cost, 2)

                roll_sto_credit = round(
                    sto_chain_price * 100 * pos_qty
                    - auto_comm("sell", sto_chain_price, pos_qty, is_close=False)
                    - auto_fees("sell", sto_chain_price, pos_qty, ticker),
                    2,
                )
                roll_close_price = round(sto_chain_price * 0.40, 4)
                roll_close_cost = round(
                    roll_close_price * 100 * pos_qty
                    + auto_comm("buy", roll_close_price, pos_qty, is_close=True)
                    + auto_fees("buy", roll_close_price, pos_qty, ticker),
                    2,
                )
                projected_roll_pnl = round(_adj_net + roll_sto_credit - roll_close_cost, 2)
                if projected_roll_pnl < 0:
                    rec = "HOLD"
                    log.warning(
                        "Overriding ROLL→HOLD for %s: deterministic projected chain PnL "
                        "%.2f < 0 (recommended premium ~$%.2f)",
                        ticker, projected_roll_pnl, sto_chain_price
                    )

        return {
            "recommendation": rec,
            "text": text,
            "tail_text": tail_text,  # so /api/ask can replay this turn for follow-ups
            "ticker": ticker,
            "chain_cash":      chain["net_cash"],       # for dashboard badge
            "chain_collected": chain["collected"],
            "chain_paid":      chain["paid"],
            "chain_positions": chain["num_positions"],
            "sim_chain_cash":  chain["sim_net_cash"],   # includes test trades
            "has_test_trade":  chain["has_test"],
            "pos_avg_price":   pos_avg_price,
            "pos_qty":         pos_qty,
            "ul_cost_basis":   ul_cost_basis,
            "btc_chain_price": btc_chain_price,
            "sto_chain_price": sto_chain_price,
            "sto_chain_desc":  sto_chain_desc,
            "projected_roll_pnl": projected_roll_pnl,
            "roll_btc_cost":    roll_btc_cost,     # incl. estimated commission/fees
            "roll_sto_credit":  roll_sto_credit,   # incl. estimated commission/fees
            "roll_close_cost":  roll_close_cost,   # incl. estimated commission/fees
            "roll_close_price": roll_close_price,  # 40% of sto_chain_price
            "chain_candidates_text": chain_candidates_text,  # for claude_advisor's reuse — same chain, no 2nd fetch
            "error": None,
        }

    except Exception as exc:
        return {"error": str(exc), "recommendation": "HOLD", "text": "", "ticker": ticker}


async def run_unborn_for_ticker(
    token: str,
    account_id: str,
    ticker: str,
    qty: int,
    strat: str,
    notebook_id: str,
    num_expirations: int = 20,
    ul_cost_basis: float = 0.0,
) -> dict:
    """
    CC/CSP analysis for a ticker with no existing open position ('unborn').
      1. Fetch & write option chain CSV
      2. Upload to NotebookLM
      3. Query NotebookLM with CC or CSP strategy
    Returns dict: {recommendation, text, ticker, strat, error}
    """
    _NO_OPT_ROW = {
        "symbol": ticker, "option_type": strat, "strike": None, "expiry": None,
        "side": "Short", "dte": None, "delta": None, "ul_price": None,
        "opt_price": None, "ideal_entry": "NO OPT AVAIL", "_no_opt": True,
    }
    try:
        try:
            all_expirations = get_expirations(token, account_id, ticker)
        except RuntimeError as _exp_err:
            _msg = str(_exp_err)
            if "does not support option" in _msg or "HTTP 400" in _msg:
                log.warning("[unborn] %s has no option support: %s", ticker, _msg)
                return {
                    "recommendation": "HOLD", "text": "", "ticker": ticker, "strat": strat,
                    "chain": [_NO_OPT_ROW], "ul_cost_basis": ul_cost_basis,
                    "_no_opt": True, "error": None,
                }
            raise
        if not all_expirations:
            return {
                "error": f"No option expirations found for {ticker} on Public.com.",
                "recommendation": "HOLD", "text": "", "ticker": ticker, "strat": strat,
            }
        # Filter out expirations within 7 days — too near-term to be useful
        _today = datetime.date.today()
        all_expirations = [
            e for e in all_expirations
            if (datetime.date.fromisoformat(e) - _today).days >= 7
        ]
        if not all_expirations:
            return {
                "error": f"No expirations with DTE ≥ 7 found for {ticker}.",
                "recommendation": "HOLD", "text": "", "ticker": ticker, "strat": strat,
            }
        expirations = all_expirations[:num_expirations]

        all_rows: list[dict] = []
        for exp_date in expirations:
            chain = get_option_chain(token, account_id, ticker, exp_date)
            if not chain:
                continue
            for contract in chain.get("calls", []):
                all_rows.append(contract_to_row(contract, "CALL", exp_date))
            for contract in chain.get("puts", []):
                all_rows.append(contract_to_row(contract, "PUT", exp_date))

        if not all_rows:
            return {
                "error": f"Option chain returned no contracts for {ticker}.",
                "recommendation": "HOLD", "text": "", "ticker": ticker, "strat": strat,
            }

        # Get underlying price for DTE and delta enrichment
        ul_price = get_underlying_price(ticker)
        today = datetime.date.today()

        # Build display rows: relevant type only, enriched with DTE
        opt_type_filter = "CALL" if strat == "CC" else "PUT"
        display_rows = []
        for row in all_rows:
            if row.get("option_type", "").upper() != opt_type_filter:
                continue
            try:
                expiry_dt = datetime.date.fromisoformat(row["expiration_date"])
                dte = (expiry_dt - today).days
            except (KeyError, ValueError):
                dte = None
            display_rows.append({
                "symbol":      ticker,
                "option_type": opt_type_filter,
                "strike":      row.get("strike_price"),
                "expiry":      row.get("expiration_date"),
                "side":        "Short",
                "dte":         dte,
                "delta":       row.get("delta"),
                "ul_price":    ul_price,
                "opt_price":   row.get("mid_price") or row.get("last"),
            })

        # Same chain fetch as above, reused for Claude's opinion (claude_advisor.py)
        # — no separate Public.com hit.
        chain_candidates_text = claude_advisor.build_chain_candidates_text(all_rows, opt_type_filter)

        key_dates = get_key_dates(ticker)
        vix = get_vix()
        atr = get_atr(ticker)

        # Claude is the primary advisor (see claude_advisor.py) — no chain CSV
        # upload/source needed since Claude reads the candidate list directly
        # from chain_candidates_text, not a full uploaded chain document.
        claude_context = claude_advisor.build_unborn_context(ticker, strat, ul_price, ul_cost_basis, vix, atr, key_dates)
        claude_result = claude_advisor.query_claude_unborn_advisor(claude_context, chain_candidates_text)
        if claude_result.get("error"):
            return {"error": claude_result["error"], "recommendation": "HOLD",
                    "text": "", "ticker": ticker, "strat": strat, "chain": display_rows}
        text = claude_result["text"]
        tail_text = claude_result.get("tail_text")

        log.info("[unborn] display_rows count: %d, first: %s", len(display_rows), display_rows[0] if display_rows else None)
        # Claude already returns a clean SELL/WAIT recommendation (parsed by
        # claude_advisor._call_claude) — no text-classification heuristics
        # needed. "HOLD" is this dict's established vocabulary for the
        # don't-act case (matching the existing-position return shape), even
        # though claude_advisor's own SELL/WAIT terminology differs.
        _do_nothing = (claude_result.get("recommendation") != "SELL")
        rec = "HOLD" if _do_nothing else "SELL"
        if _do_nothing:
            # Return a placeholder row — option details are blank, ideal_entry = DO NOTHING
            chosen = {
                "symbol":      ticker,
                "option_type": strat,
                "strike":      None,
                "expiry":      None,
                "side":        "Short",
                "dte":         None,
                "delta":       None,
                "ul_price":    display_rows[0].get("ul_price") if display_rows else None,
                "opt_price":   None,
                "ideal_entry": "DO NOTHING",
                "_do_nothing": True,
            }
            log.info("[unborn] DO NOTHING recommendation for %s", ticker)
        else:
            chosen = _parse_recommended_option(text, display_rows)
            log.info("[unborn] chosen: %s", chosen)
            if chosen is None and display_rows:
                # Fallback: pick option closest to 30 DTE and 0.30 delta
                target_delta = 0.30 if strat == "CC" else -0.30
                def _score(r):
                    try:
                        d_dist = abs(float(r.get("delta") or 0) - target_delta)
                    except (TypeError, ValueError):
                        d_dist = 1.0
                    try:
                        dte_dist = abs((r.get("dte") or 30) - 30)
                    except (TypeError, ValueError):
                        dte_dist = 30
                    return dte_dist * 0.5 + d_dist * 50
                chosen = min(display_rows, key=_score)
            if chosen is not None:
                chosen = dict(chosen)
                chosen["ideal_entry"] = _parse_ideal_entry(text)
        return {
            "recommendation": rec,
            "text": text,
            "tail_text": tail_text,  # so /api/ask-unborn can replay this turn for follow-ups
            "ticker": ticker,
            "strat": strat,
            "chain": [chosen] if chosen else [],
            "ul_cost_basis": ul_cost_basis,
            "ul_price": ul_price,
            "_do_nothing": _do_nothing,
            "chain_candidates_text": chain_candidates_text,  # for claude_advisor's reuse — same chain, no 2nd fetch
            "error": None,
        }

    except Exception as exc:
        return {"error": str(exc), "recommendation": "HOLD", "text": "", "ticker": ticker, "strat": strat}


# ── HTML template for --web dashboard ────────────────────────────────────────
_WEB_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Option Dashboard</title>
<script>(function(){try{if(localStorage.getItem('theme')==='light')document.documentElement.setAttribute('data-theme','light');}catch(e){}})();</script>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Ccircle cx='32' cy='32' r='30' fill='%23052e16' stroke='%2322c55e' stroke-width='3'/%3E%3Cpath d='M14 40 A20 20 0 1 1 50 40' fill='none' stroke='%23164b2a' stroke-width='6' stroke-linecap='round'/%3E%3Cpath d='M14 40 A20 20 0 0 1 44 18' fill='none' stroke='%2322c55e' stroke-width='6' stroke-linecap='round'/%3E%3Ccircle cx='32' cy='32' r='3' fill='%2322c55e'/%3E%3Cline x1='32' y1='32' x2='44' y2='20' stroke='%2386efac' stroke-width='2.5' stroke-linecap='round'/%3E%3Ctext x='32' y='50' text-anchor='middle' font-size='8' fill='%2322c55e' font-family='monospace'%3EOPTS%3C/text%3E%3C/svg%3E">
<style>
  :root {
    --bg: #0f1117; --surface: #1a1d27; --border: #2a2d3a;
    --text: #e2e8f0; --muted: #8892a4; --accent: #4f8ef7;
    --ok: #22c55e; --warn: #facc15; --danger: #ef4444;
    --ok-bg: #052e16; --warn-bg: #3a2e00; --danger-bg: #450a0a;
    --warn-row-bg: #1f1a0a; --danger-row-bg: #1f0a0a; --hold-bg: #0c1a2e;
    --tooltip-bg: #1e1e2e; --tooltip-fg: #f87171;
  }
  :root[data-theme="light"] {
    --bg: #f7f8fa; --surface: #ffffff; --border: #dde1e8;
    --text: #1a1d27; --muted: #667085; --accent: #2563eb;
    --ok: #16a34a; --warn: #b45309; --danger: #dc2626;
    --ok-bg: #dcfce7; --warn-bg: #fef3c7; --danger-bg: #fee2e2;
    --warn-row-bg: #fefaf0; --danger-row-bg: #fef5f5; --hold-bg: #dbeafe;
    --tooltip-bg: #1a1d27; --tooltip-fg: #fca5a5;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'SF Mono', 'Fira Code', monospace; font-size: 13px; }
  .theme-switch { position: relative; display: inline-block; width: 32px; height: 17px; flex-shrink: 0; }
  .theme-switch input { opacity: 0; width: 0; height: 0; }
  .theme-switch .slider { position: absolute; inset: 0; background: var(--border); border-radius: 17px; cursor: pointer; transition: background .15s; }
  .theme-switch .slider::before { content: ""; position: absolute; width: 13px; height: 13px; left: 2px; top: 2px; background: var(--surface); border-radius: 50%; transition: transform .15s; }
  .theme-switch input:checked + .slider { background: var(--accent); }
  .theme-switch input:checked + .slider::before { transform: translateX(15px); }
  header { background: var(--surface); border-bottom: 1px solid var(--border); padding: 14px 24px; display: flex; align-items: center; gap: 24px; flex-wrap: wrap; }
  header h1 { font-size: 16px; font-weight: 600; color: var(--accent); letter-spacing: 0.05em; }
  .stat { display: flex; flex-direction: column; gap: 2px; }
  .stat-label { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; }
  .stat-value { font-size: 15px; font-weight: 600; }
  .ok    { color: var(--ok); }
  .warn  { color: var(--warn); }
  .danger { color: var(--danger); }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0} }
  .blink { animation: blink 1s step-start infinite; }
  .muted { color: var(--muted); }
  .actions { margin-left: auto; display: flex; align-items: center; gap: 12px; }
  button { background: var(--accent); color: #fff; border: none; border-radius: 6px; padding: 6px 14px; cursor: pointer; font-size: 12px; font-family: inherit; }
  button:hover { opacity: 0.85; }
  #countdown { font-size: 11px; color: var(--muted); }
  main { padding: 20px 24px; }
  .section-title { font-size: 11px; text-transform: uppercase; letter-spacing: 0.1em; color: var(--muted); margin-bottom: 10px; margin-top: 20px; }
  table { width: 100%; border-collapse: collapse; }
  th { background: var(--surface); color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--border); cursor: pointer; user-select: none; white-space: nowrap; position: sticky; top: 0; z-index: 10; overflow: visible; }
  .col-rh { position:absolute; right:0; top:0; bottom:0; width:5px; cursor:col-resize; z-index:11; background:var(--border); opacity:.5; }
  .col-rh:hover, .col-rh.dragging { background:var(--accent); opacity:.9; }
  th:hover { color: var(--text); }
  th.sorted-asc::after  { content: " ▲"; }
  th.sorted-desc::after { content: " ▼"; }
  td { padding: 8px 10px; border-bottom: 1px solid var(--border); vertical-align: top; white-space: nowrap; transition: font-size 80ms ease, padding-top 80ms ease, padding-bottom 80ms ease; }
  tr.ok-row   td { background: transparent; }
  tr.warn-row td { background: var(--warn-row-bg); }
  tr.danger-row td { background: var(--danger-row-bg); }
  tr:hover td { filter: brightness(1.15); font-size: 15px; padding-top: 12px; padding-bottom: 12px; }
  #unborn-chain tbody tr:hover td, #former-chain tbody tr:hover td { padding-top: 9px; padding-bottom: 9px; }
  .badge { display: inline-block; border-radius: 4px; padding: 2px 7px; font-size: 10px; font-weight: 600; letter-spacing: 0.05em; }
  .badge-ok     { background: var(--ok-bg);   color: var(--ok);   }
  .badge-hold   { background: var(--hold-bg); color: var(--accent); border: 1px solid var(--accent); }
  .badge-warn   { background: var(--warn-bg); color: var(--warn); }
  .badge-danger { background: var(--danger-bg); color: var(--danger); }
  .reasons { font-size: 11px; color: var(--muted); white-space: normal; max-width: 360px; }
  .err-tip { position: relative; display: inline-block; cursor: default; }
  .err-tip .err-msg {
    display: none; position: absolute; z-index: 100; bottom: calc(100% + 6px); left: 50%;
    transform: translateX(-50%); background: var(--tooltip-bg); color: var(--tooltip-fg); border: 1px solid var(--tooltip-fg);
    border-radius: 6px; padding: 7px 10px; font-size: 11px; white-space: pre-wrap;
    max-width: 340px; min-width: 140px; word-break: break-word; box-shadow: 0 4px 16px #0008;
    pointer-events: none;
  }
  .err-tip:hover .err-msg { display: block; }
  .info-tip { cursor: help; display: inline-block; padding: 2px 4px; margin: -2px -4px; }
  .reasons li { margin-top: 3px; }
  .pnl-pos { color: var(--ok); }
  .pnl-neg { color: var(--danger); }
  .skipped-box { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px 16px; margin-top: 16px; }
  .skipped-box li { color: var(--muted); font-size: 12px; margin-top: 4px; }
  .unborn-bar { position:relative; display:flex; align-items:center; gap:10px; background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:10px 14px; margin-bottom:18px; flex-wrap:wrap; }
  .unborn-bar label { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:.05em; }
  .unborn-bar input[type=text], .unborn-bar input[type=number], .unborn-bar select {
    background:var(--bg); color:var(--text); border:1px solid var(--border); border-radius:4px;
    padding:4px 8px; font-size:12px; font-family:inherit; width:80px;
  }
  .unborn-bar input[type=text] { width:90px; }
  .unborn-bar select { width:80px; }
  #unborn-result { margin-left:6px; }
  #unborn-chain { margin-top:12px; overflow-x:auto; }
  #unborn-chain table { border-collapse:collapse; font-size:12px; width:100%; }
  #unborn-chain th { background:var(--surface); color:var(--muted); font-size:10px; text-transform:uppercase; letter-spacing:.05em; padding:5px 10px; text-align:left; border-bottom:1px solid var(--border); white-space:nowrap; }
  #unborn-chain td { padding:5px 10px; border-bottom:1px solid var(--border); white-space:nowrap; color:var(--text); }
  #former-chain { margin-top:18px; overflow-x:auto; }
  #former-chain table { border-collapse:collapse; font-size:12px; width:100%; }
  #former-chain th { background:var(--surface); color:var(--muted); font-size:10px; text-transform:uppercase; letter-spacing:.05em; padding:5px 10px; text-align:left; border-bottom:1px solid var(--border); white-space:nowrap; cursor:pointer; user-select:none; }
  #former-chain td { padding:5px 10px; border-bottom:1px solid var(--border); white-space:nowrap; color:var(--text); }
  #spinner { display: none; align-items: center; gap: 6px; font-size: 11px; color: var(--muted); margin-left: 10px; }
  #spinner.active { display: inline-flex; }
  #spinner .spin-ring { width: 13px; height: 13px; border: 2px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.7s linear infinite; flex-shrink: 0; }
  @keyframes spin { to { transform: rotate(360deg); } }
  #error-bar { display: none; background: var(--danger-bg); color: var(--danger); padding: 8px 16px; font-size: 12px; border-bottom: 1px solid var(--danger); }
</style>
</head>
<body>
<div id="error-bar"></div>
<header>
  <h1>&#9660; Option Dashboard</h1>
  <div class="stat"><span class="stat-label">Total</span><span class="stat-value" id="h-total">—</span></div>
  <div class="stat"><span class="stat-label">Flagged</span><span class="stat-value" id="h-flagged">—</span></div>
  <div class="stat"><span class="stat-label">VIX</span><span class="stat-value" id="h-vix">—</span></div>
  <div class="stat"><span class="stat-label">As of</span><span class="stat-value muted" id="h-time" style="font-size:12px">—</span></div>
  <div id="multiplier-files" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap"><!--MULTIPLIER_FILES--></div>
  <div class="actions">
    <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px;color:var(--muted)">
      <span class="theme-switch">
        <input type="checkbox" id="theme-toggle" onchange="toggleTheme()" checked>
        <span class="slider"></span>
      </span>
      Dark mode
    </label>
    <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px;color:var(--muted)">
      <input type="checkbox" id="collapse-ok" onchange="applyCollapseOk()" style="cursor:pointer;accent-color:var(--accent)">
      Collapse OK
    </label>
    <div style="display:flex;flex-direction:column;gap:3px">
      <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted)">
        Auto-refresh
        <select id="auto-refresh-sel" onchange="setAutoRefresh(this.value)"
          style="background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:3px 6px;font-size:12px;font-family:inherit;cursor:pointer">
          <option value="0">Off</option>
          <option value="300">5 min</option>
          <option value="600">10 min</option>
          <option value="900">15 min</option>
          <option value="1800">30 min</option>
          <option value="3600">60 min</option>
          <option value="7200">2 hours</option>
          <option value="14400">4 hours</option>
        </select>
      </label>
      <span id="last-run" style="font-size:10px;color:var(--muted);padding-left:2px">Last Run: —</span>
    </div>
    <span id="spinner"><span class="spin-ring"></span>refreshing…</span>
    <button onclick="fetchData()">&#8635; Refresh</button>
  </div>
</header>
<main>
  <div class="unborn-bar">
    <label for="ub-ticker">Ticker</label>
    <input type="text" id="ub-ticker" placeholder="IBM" maxlength="10" style="text-transform:uppercase" oninput="this.value=this.value.toUpperCase()" onblur="autoFillCcQty()">
    <label for="ub-qty">Qty</label>
    <input type="number" id="ub-qty" placeholder="1" min="1" value="1" style="width:60px">
    <label for="ub-strat">Strategy</label>
    <select id="ub-strat" onchange="autoFillCcQty()">
      <option value="CC" selected>CC</option>
      <option value="CSP">CSP</option>
    </select>
    <button onclick="findUnborn()">Find</button>
    <span id="unborn-result"></span>
    <div id="pos-search-wrap" style="display:flex;align-items:center;gap:6px">
      <label for="pos-search">Search</label>
      <input type="text" id="pos-search" placeholder="Filter by ticker…" maxlength="10"
        style="text-transform:uppercase;width:140px" oninput="onPosSearchInput(this.value)">
      <button id="pos-search-clear" onclick="clearPosSearch()" title="Clear"
        style="display:none;background:none;border:none;color:var(--muted);cursor:pointer;font-size:13px;padding:0 2px">&#10005;</button>
    </div>
  </div>
  <div id="unborn-chain"></div>
  <div class="section-title">Open Positions</div>
  <table id="pos-table">
    <thead>
      <tr>
        <th data-col="symbol" onclick="sortTable(0)">Symbol</th>
        <th data-col="type" onclick="sortTable(1)">Type</th>
        <th data-col="strike" onclick="sortTable(2)">Strike</th>
        <th data-col="expiry" onclick="sortTable(3)">Expiry</th>
        <th data-col="side" onclick="sortTable(4)">Side</th>
        <th data-col="qty" onclick="sortTable(5)">Qty</th>
        <th data-col="dte" onclick="sortTable(6)">DTE</th>
        <th data-col="delta" onclick="sortTable(7)">Δ</th>
        <th data-col="decay" onclick="sortTable(8)" title="Share of the price move since entry attributable to time decay vs. spot/vol movement — replaces the flat 60%-profit rule of thumb">Decay</th>
        <th data-col="ul_price" onclick="sortTable(9)">U/L Price</th>
        <th data-col="cost_basis" onclick="sortTable(10)">Cost Basis</th>
        <th data-col="opt_price" onclick="sortTable(11)">Opt Price</th>
        <th data-col="pnl" onclick="sortTable(12)">PnL</th>
        <th data-col="status">Status / Flags</th>
        <th data-col="dont_analyze" style="text-align:center" title="If checked, scheduled auto-analysis skips this position. Flags still light up, and pressing Analyze still works.">DON'T ANALYZE</th>
        <th data-col="action">Action<br><button onclick="resetRecommendations()" style="font-size:10px;padding:2px 7px;margin-top:4px;font-weight:normal">Reset</button></th>
        <th data-col="alert" style="text-align:center">Alert</th>
        <th data-col="hide" style="text-align:center;cursor:pointer" onclick="unhideAll()" title="Click to unhide all">Hide</th>
      </tr>
    </thead>
    <tbody id="pos-body"></tbody>
  </table>
  <div id="skipped-section"></div>
  <div id="former-chain"></div>
</main>

<!-- ── Alert Modal ──────────────────────────────────────────────────────── -->
<div id="alert-modal-overlay" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:9000;align-items:center;justify-content:center">
  <div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:22px 26px;min-width:340px;max-width:420px;box-shadow:0 8px 32px #0008">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <span id="alert-modal-title" style="font-weight:700;font-size:13px;color:var(--accent)"></span>
      <span onclick="closeAlertModal()" style="cursor:pointer;font-size:20px;line-height:1;color:var(--muted)">&times;</span>
    </div>
    <input type="hidden" id="alert-pos-key">
    <table style="width:100%;border-collapse:collapse;font-size:12px">
      <tr>
        <td style="padding:6px 8px 6px 0;color:var(--muted);white-space:nowrap">Underlying Price</td>
        <td style="padding:6px 4px">
          <select id="alert-price-dir" style="background:var(--surface2);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:3px 6px;font-size:12px">
            <option value="above">Above</option>
            <option value="below">Below</option>
          </select>
        </td>
        <td style="padding:6px 0">
          <input id="alert-price-val" type="number" step="any" placeholder="blank = ignore"
            style="width:130px;background:var(--surface2);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:3px 7px;font-size:12px">
        </td>
      </tr>
      <tr>
        <td style="padding:6px 8px 6px 0;color:var(--muted);white-space:nowrap">Delta</td>
        <td style="padding:6px 4px">
          <select id="alert-delta-dir" style="background:var(--surface2);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:3px 6px;font-size:12px">
            <option value="above">Above</option>
            <option value="below">Below</option>
          </select>
        </td>
        <td style="padding:6px 0">
          <input id="alert-delta-val" type="number" step="any" placeholder="blank = ignore"
            style="width:130px;background:var(--surface2);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:3px 7px;font-size:12px">
        </td>
      </tr>
    </table>
    <div style="margin:14px 0 18px;font-size:12px;color:var(--muted)">
      Trigger condition:&nbsp;&nbsp;
      <label style="cursor:pointer;margin-right:14px">
        <input type="radio" name="alert-cond" id="alert-cond-and" value="and" style="margin-right:4px">AND
      </label>
      <label style="cursor:pointer">
        <input type="radio" name="alert-cond" id="alert-cond-or" value="or" checked style="margin-right:4px">OR
      </label>
    </div>
    <div style="display:flex;gap:8px;justify-content:flex-end">
      <button onclick="clearAlert()" style="font-size:12px;padding:5px 12px;background:var(--danger);color:#fff;border:none;border-radius:5px;cursor:pointer">Clear</button>
      <button onclick="closeAlertModal()" style="font-size:12px;padding:5px 12px;background:var(--surface2);color:var(--fg);border:1px solid var(--border);border-radius:5px;cursor:pointer">Cancel</button>
      <button onclick="saveAlert()" style="font-size:12px;padding:5px 14px;background:var(--accent);color:#000;font-weight:700;border:none;border-radius:5px;cursor:pointer">Save</button>
    </div>
  </div>
</div>
<script>
const _SERVER_VERSION = "<!--SERVER_VERSION-->";
let _data = [];
const _LS_SORT_KEY = 'optionsSortState';
let _sortCol = (() => { try { return JSON.parse(localStorage.getItem(_LS_SORT_KEY) || '[0,1]')[0]; } catch { return 0; } })();
let _sortDir = (() => { try { return JSON.parse(localStorage.getItem(_LS_SORT_KEY) || '[0,1]')[1]; } catch { return 1; } })();
let _ubSortCol = 3, _ubSortDir = 1;
function _ubSort(col) {
  if (_ubSortCol === col) _ubSortDir *= -1; else { _ubSortCol = col; _ubSortDir = 1; }
  _renderUnbornTable();
}

// ── Position search (filters Open + Former Positions by ticker) ─────────
let _posSearch = '';
function onPosSearchInput(val) {
  _posSearch = val.trim().toUpperCase();
  document.getElementById('pos-search-clear').style.display = _posSearch ? '' : 'none';
  renderTable();
  _renderFormerTable();
}
function clearPosSearch() {
  document.getElementById('pos-search').value = '';
  onPosSearchInput('');
}
function _alignPosSearch() {
  const anchor = document.getElementById('last-run');
  const bar = document.querySelector('.unborn-bar');
  const wrap = document.getElementById('pos-search-wrap');
  if (!anchor || !bar || !wrap) return;
  const left = anchor.getBoundingClientRect().left - bar.getBoundingClientRect().left;
  wrap.style.position = 'absolute';
  wrap.style.left = Math.max(0, left) + 'px';
}
window.addEventListener('load', _alignPosSearch);
window.addEventListener('resize', _alignPosSearch);

async function fetchData() {
  document.getElementById('spinner').classList.add('active');
  try {
    const r = await fetch('/api/eval');
    if (!r.ok) throw new Error(await r.text());
    const d = await r.json();
    document.getElementById('error-bar').style.display = 'none';
    applyData(d);
    _loadFormerPositions();
    fetch('/api/multiplier-status-html', {cache: 'no-store'}).then(r => {
      if (!r.ok) return;
      return r.text();
    }).then(html => {
      if (!html) return;
      const el = document.getElementById('multiplier-files');
      if (el) el.innerHTML = html;
      _alignPosSearch();
    }).catch(() => {});
    const ts = new Date().toLocaleTimeString('en-US', {hour12: false});
    document.getElementById('last-run').textContent = 'Last Run: ' + ts;
    localStorage.setItem(_LS_AR_LAST, String(Date.now()));
    _alignPosSearch();
  } catch(e) {
    const bar = document.getElementById('error-bar');
    bar.textContent = 'Refresh failed: ' + e.message;
    bar.style.display = 'block';
  } finally {
    document.getElementById('spinner').classList.remove('active');
  }
}

function applyData(d) {
  // Server restarted (code deploy) since this tab loaded — a stale tab keeps
  // polling data-only endpoints forever on old JS otherwise, silently missing
  // any fix. Force a real reload so the tab always picks up current code.
  if (d.server_version && _SERVER_VERSION && d.server_version !== _SERVER_VERSION) {
    console.warn('[version] server restarted (', _SERVER_VERSION, '→', d.server_version, ') — reloading');
    location.reload();
    return;
  }
  _data = d.positions || [];
  // Seed _prevUlPrices from server data so the first price poll can show ticks
  for (const p of _data) {
    const sym = (p.symbol || '').toUpperCase();
    if (_prevUlPrices[sym] == null && p.underlying != null) {
      _prevUlPrices[sym] = p.underlying;
    }
  }
  // Restore any server-cached analysis results so badges survive refresh
  const serverRecs = d.cached_recommendations || {};
  let changed = false;
  for (const [key, val] of Object.entries(serverRecs)) {
    if (val.recommendation) {
      const existing = _recommendations[key];
      const incoming = {rec: val.recommendation, chainCash: val.chain_cash ?? null, runAt: val.run_at ?? null};
      if (!existing || existing.rec !== incoming.rec || existing.runAt !== incoming.runAt) {
        _recommendations[key] = Object.assign(existing || {}, incoming);
        changed = true;
      }
    }
  }
  if (changed) _saveRecs(_recommendations);
  const fc = d.flagged_count || 0;
  document.getElementById('h-total').textContent = d.total_count ?? _data.length;
  const fEl = document.getElementById('h-flagged');
  fEl.textContent = fc;
  fEl.className = 'stat-value ' + (fc === 0 ? 'ok' : fc <= 3 ? 'warn' : 'danger');
  const vix = d.vix;
  const vEl = document.getElementById('h-vix');
  if (vix != null) {
    vEl.textContent = vix.toFixed(2);
    vEl.className = 'stat-value ' + (vix < 20 ? 'ok' : vix < 30 ? 'warn' : 'danger');
  } else {
    vEl.textContent = 'N/A'; vEl.className = 'stat-value muted';
  }
  document.getElementById('h-time').textContent = (d.fetched_at || '').replace('T', ' ');
  renderTable();
  renderSkipped(d.skipped || []);

  // Pick up any externally-triggered in-flight analyses (e.g. scheduled_refresh.py)
  _inflight       = new Set(d.inflight || []);
  _unbornInflight = new Set(d.unborn_inflight || []);
  for (const key of _inflight) {
    if (_recommendations[key]) continue;  // badge already showing
    const cell = _actionCell(key);
    if (!cell) continue;
    const btn = cell.querySelector('button');
    if (!btn || btn.disabled) continue;   // already spinning
    analyzePosition(key, btn);
  }
  if (_unbornInflight.size) _renderUnbornTableInner();
}

function fmt(v, digits, prefix='') {
  return v == null ? '—' : prefix + v.toFixed(digits);
}

// Greeks-based decay-quality cell (replaces the flat 60%-profit rule of thumb).
// decay_quality: share of the price move since entry attributable to time decay
// vs. a spot/vol move that could reverse. '—' whenever entry-snapshot data is
// missing (older positions, or the capture call failed) — never a guess.
function _opinionColor(opinion) {
  if (opinion === 'Close') return 'var(--danger)';
  if (opinion === 'Close/Roll' || opinion === 'Consider closing' || opinion === 'Hold (fragile)') return 'var(--warn)';
  return 'var(--ok)';
}

function _decayCellHtml(d, dte, delta, symbol) {
  if (!d) return '—';
  const parts = [];
  if (d.opinion) parts.push(`${d.opinion}${d.opinion_reason ? ' \\u2014 ' + d.opinion_reason : ''}`);
  if (d.spot_component != null) parts.push(`Spot: ${d.spot_component>=0?'+':''}$${d.spot_component.toFixed(3)}`);
  if (d.time_component != null) parts.push(`Time: ${d.time_component>=0?'+':''}$${d.time_component.toFixed(3)}`);
  if (d.vega_component != null) parts.push(`Vol: ${d.vega_component>=0?'+':''}$${d.vega_component.toFixed(3)}`);
  if (d.residual != null)       parts.push(`Residual: ${d.residual>=0?'+':''}$${d.residual.toFixed(3)}`);
  if (d.theta_per_day != null)  parts.push(`Theta: $${d.theta_per_day.toFixed(2)}/day`);
  if (d.capital_efficiency != null) parts.push(`Capital eff: ${(d.capital_efficiency*100).toFixed(3)}%/day`);
  if (d.dollar_gamma != null)   parts.push(`$Gamma: ${d.dollar_gamma.toFixed(0)}`);
  if (d.vanna != null)          parts.push(`Vanna: ${d.vanna.toFixed(4)}`);
  if (d.charm != null)          parts.push(`Charm: ${d.charm.toFixed(4)}/day`);
  if (d.note && d.decay_quality == null) parts.push(`(${d.note})`);
  const msg = esc(parts.join('\\n'));
  const opinionBadge = d.opinion
    ? `<span class="info-tip" data-tip="${msg}" style="color:${_opinionColor(d.opinion)};font-size:10px;margin-left:3px">${esc(d.opinion)}</span>`
    : '';

  let gammaBadge = '';
  if (d.gamma_risk) {
    const sym    = symbol || 'the stock';
    const dgStr  = d.dollar_gamma != null ? `$${d.dollar_gamma.toFixed(0)}` : 'unknown';
    const dteStr = dte != null ? dte : '?';
    const absD   = delta != null ? Math.abs(delta).toFixed(3) : '?';
    const gammaMsg = esc([
      'Gamma = how fast delta (your directional exposure) changes as the stock moves \\u2014 the "convexity" of the position.',
      'High gamma means small stock moves cause outsized swings in P&L, because your effective exposure is changing under you as the stock moves, not staying fixed like it would for a plain shares position.',
      '',
      `This position's $Gamma: ${dgStr} \\u2014 a 1% move in ${sym} would shift your delta exposure by roughly that many dollars.`,
      '',
      `Flagged because DTE \\u2264 14 AND |delta| \\u2265 0.30 (this position: ${dteStr} DTE, |delta| ${absD}).`,
      'That combination matters because gamma accelerates sharply as expiry nears, and is largest for strikes near the money (moderate-to-high delta) \\u2014 so small stock moves swing this position\\u2019s value disproportionately to the theta reward left on the table.',
    ].join('\\n'));
    gammaBadge = `<span class="info-tip" data-tip="${gammaMsg}" style="color:var(--danger);margin-left:3px">&#9650;</span>`;
  }

  if (d.decay_quality == null) {
    return `<span class="info-tip" data-tip="${msg}" style="color:var(--muted)">—</span>${gammaBadge}${opinionBadge}`;
  }
  const pct = d.decay_quality * 100;
  const color = pct >= 70 ? 'var(--ok)' : pct >= 35 ? 'var(--warn)' : 'var(--danger)';
  return `<span class="info-tip" data-tip="${msg}" style="color:${color}">${pct.toFixed(0)}%</span>${gammaBadge}${opinionBadge}`;
}

// Shared, JS-positioned tooltip for .info-tip elements. Deliberately NOT a
// CSS-only nested-absolute child (that was the original approach): the table's
// sticky header has its own z-index'd layer, and a tooltip nested inside a
// table row can end up painted underneath it regardless of a nominally higher
// z-index, since stacking-context comparison doesn't just compare raw numbers
// across arbitrarily nested ancestors. A single `position:fixed` element
// appended directly to <body> sits outside the table's DOM/stacking context
// entirely, so this can't happen — and it lets us clamp/flip the position to
// stay inside the viewport on any edge, not just the top.
let _tipEl = null;
function _showInfoTip(target) {
  const text = target.getAttribute('data-tip');
  if (!text) return;
  if (!_tipEl) {
    _tipEl = document.createElement('div');
    _tipEl.id = '_infoTooltip';
    _tipEl.style.cssText = 'display:none;position:fixed;z-index:99999;background:var(--surface);' +
      'color:var(--text);border:1px solid var(--border);border-radius:6px;padding:9px 12px;' +
      'font-size:11px;line-height:1.5;white-space:pre-wrap;max-width:380px;min-width:200px;' +
      'word-break:break-word;box-shadow:0 4px 16px #0008;pointer-events:none;text-align:left';
    document.body.appendChild(_tipEl);
  }
  _tipEl.textContent = text;
  _tipEl.style.display = 'block';
  const r = target.getBoundingClientRect();
  const tw = _tipEl.offsetWidth, th = _tipEl.offsetHeight;
  let left = r.right + 6;
  if (left + tw > window.innerWidth - 8) left = r.left - tw - 6;   // flip left if it'd overflow the right edge
  if (left < 8) left = 8;
  let top = r.top + r.height / 2 - th / 2;
  top = Math.max(8, Math.min(top, window.innerHeight - th - 8));   // clamp within the viewport vertically
  _tipEl.style.left = left + 'px';
  _tipEl.style.top  = top + 'px';
}
function _hideInfoTip() {
  if (_tipEl) _tipEl.style.display = 'none';
}
document.addEventListener('mouseover', e => {
  const t = e.target.closest('.info-tip');
  if (t) _showInfoTip(t);
});
document.addEventListener('mouseout', e => {
  const t = e.target.closest('.info-tip');
  if (t) _hideInfoTip();
});

function _applySortHeader() {
  const ths = document.querySelectorAll('th');
  ths.forEach(th => th.classList.remove('sorted-asc','sorted-desc'));
  if (ths[_sortCol]) ths[_sortCol].classList.add(_sortDir === 1 ? 'sorted-asc' : 'sorted-desc');
}

function renderTable() {
  _applySortHeader();
  const rows = _posSearch ? _data.filter(p => (p.symbol||'').toUpperCase().includes(_posSearch)) : [..._data];
  rows.sort((a, b) => {
    const cols = [
      r => r.symbol, r => r.option_type, r => parseFloat(r.strike||0),
      r => r.expiry, r => (r.net_qty > 0 ? 'Short' : 'Long'),
      r => Math.abs(r.net_qty||0), r => r.dte??999, r => r.delta??-99,
      r => (r.decay && r.decay.decay_quality != null) ? r.decay.decay_quality : -1,
      r => r.underlying??0,
      r => (r.option_type||'').toLowerCase() === 'put' ? -1 : (r.ul_cost_basis??0),
      r => r.current_price??0,
      r => { if (r.has_test_trade && r.sim_chain_cash != null) { return r.chain_cash !== 0 && r.chain_cash != null ? (r.sim_chain_cash / Math.abs(r.chain_cash)) * 100 : r.sim_chain_cash; } if (r.chain_cash != null && r.current_price != null) { const abs = r.chain_cash - r.current_price * 100 * Math.abs(r.net_qty||0); return r.chain_cash !== 0 ? (abs / Math.abs(r.chain_cash)) * 100 : abs; } return r.pct_pnl ?? -999999; },
    ];
    const fn = cols[_sortCol] || (r => 0);
    const av = fn(a), bv = fn(b);
    return _sortDir * (av < bv ? -1 : av > bv ? 1 : 0);
  });

  // ── Collar detection ─────────────────────────────────────────────────────
  // A collar = spread_id group that has both a call leg and a put leg.
  const _collarMap = new Map(); // spread_id → {call, put}
  for (const p of rows) {
    if (!p.spread_id) continue;
    const g = _collarMap.get(p.spread_id) || {call: null, put: null};
    if ((p.option_type||'').toLowerCase() === 'call') g.call = p;
    else if ((p.option_type||'').toLowerCase() === 'put') g.put = p;
    _collarMap.set(p.spread_id, g);
  }
  const _collars = new Map(); // spread_id → {call, put} for confirmed collars only
  for (const [sid, g] of _collarMap) {
    if (g.call && g.put) _collars.set(sid, g);
  }

  // Regroup: move second collar leg immediately after the first so sort order
  // never splits them (e.g. sorting by Side puts Long before Short).
  if (_collars.size) {
    for (const [sid] of _collars) {
      const firstIdx  = rows.findIndex(p => p.spread_id === sid);
      const secondIdx = rows.findIndex((p, i) => p.spread_id === sid && i !== firstIdx);
      if (firstIdx >= 0 && secondIdx >= 0 && secondIdx !== firstIdx + 1) {
        const [leg] = rows.splice(secondIdx, 1);
        rows.splice(firstIdx + 1, 0, leg);
      }
    }
  }

  const _collarHeaderRendered = new Set();

  const tbody = document.getElementById('pos-body');
  tbody.innerHTML = '';
  for (const p of rows) {
    // ── Collar group header (rendered once, before the first leg) ──────────
    if (p.spread_id && _collars.has(p.spread_id) && !_collarHeaderRendered.has(p.spread_id)) {
      _collarHeaderRendered.add(p.spread_id);
      const cg  = _collars.get(p.spread_id);
      const sym = (cg.call.symbol || cg.put.symbol || '').toUpperCase();
      const callStrike = parseFloat(cg.call.strike || 0).toFixed(2);
      const putStrike  = parseFloat(cg.put.strike  || 0).toFixed(2);
      const expiry     = cg.call.expiry || cg.put.expiry || '';
      const qty        = Math.abs(cg.call.net_qty || 0);
      const ulPrice    = cg.call.underlying;
      const ulTick     = _ulPriceTick[sym];
      const ulHtml     = ulPrice == null ? '—'
        : ulTick === 'up'   ? `<span style="color:var(--ok);font-weight:700" title="Price up since last poll">&#9650; $${ulPrice.toFixed(2)}</span>`
        : ulTick === 'down' ? `<span style="color:var(--danger);font-weight:700" title="Price down since last poll">&#9660; $${ulPrice.toFixed(2)}</span>`
        : `$${ulPrice.toFixed(2)}`;

      // Combined PnL: chain_cash (net entry for both legs) + current put value - current cc cost
      // chain_cash on the call leg already includes both legs since they share spread_id
      let collarPnlHtml = '—';
      const chainCash = cg.call.chain_cash;
      const ccPrice   = cg.call.current_price;
      const putPrice  = cg.put.current_price;
      if (chainCash != null && ccPrice != null && putPrice != null && qty > 0) {
        const collarPnl = chainCash - (ccPrice - putPrice) * 100 * qty;
        collarPnlHtml = `<span class="${collarPnl >= 0 ? 'pnl-pos' : 'pnl-neg'}">${collarPnl >= 0 ? '+' : ''}$${collarPnl.toFixed(2).replace(/\\B(?=(\\d{3})+(?!\\d))/g,',')}</span>`;
      }

      // Net position delta: short call contributes -call_delta; long put contributes put_delta (already negative)
      const netDelta = (cg.call.delta != null && cg.put.delta != null)
        ? (-cg.call.delta + cg.put.delta) : null;
      const netDeltaHtml = netDelta != null
        ? `<span style="color:${Math.abs(netDelta)<0.40?'var(--ok)':Math.abs(netDelta)<=0.60?'var(--warn)':'var(--danger)'}">${(netDelta>=0?'+':'')+netDelta.toFixed(3)}</span>`
        : '—';
      const dte = cg.call.dte ?? cg.put.dte ?? null;
      const dteHtml = dte == null ? '—'
        : `<span style="color:${dte>21?'var(--ok)':dte>=10?'var(--warn)':'var(--danger)'}">${dte}</span>`;

      const isCollapsed = _collarCollapsed.has(p.spread_id);
      const safeId = p.spread_id.replace(/'/g, "\\'");

      // Moneyness color: danger if either leg ITM, warn if either within 3%, else ok
      let collarSymColor = '';
      if (ulPrice != null) {
        const callStrikeV = parseFloat(cg.call.strike || 0);
        const putStrikeV  = parseFloat(cg.put.strike  || 0);
        const callItm = callStrikeV > 0 && ulPrice > callStrikeV;
        const putItm  = putStrikeV  > 0 && ulPrice < putStrikeV;
        if (callItm || putItm) {
          collarSymColor = 'color:var(--danger)';
        } else {
          const callAway = callStrikeV > 0 ? Math.abs(ulPrice - callStrikeV) / ulPrice * 100 : 999;
          const putAway  = putStrikeV  > 0 ? Math.abs(ulPrice - putStrikeV)  / ulPrice * 100 : 999;
          collarSymColor = Math.min(callAway, putAway) <= 3.0 ? 'color:var(--warn)' : 'color:var(--ok)';
        }
      }

      // Flag state: worst across both legs
      const maxFlags = Math.max((cg.call.reasons||[]).length, (cg.put.reasons||[]).length);
      const hdrRowCls = maxFlags >= 3 ? 'danger-row' : maxFlags >= 1 ? 'warn-row' : 'ok-row';
      const hdrBadge = maxFlags >= 3
        ? `<span class="badge badge-danger">&#9888; ${maxFlags} flags</span>`
        : maxFlags >= 1
          ? `<span class="badge badge-warn">&#9888; ${maxFlags} flag${maxFlags>1?'s':''}</span>`
          : '<span class="badge badge-ok">&#10003; OK</span>';

      const hdrTr = document.createElement('tr');
      hdrTr.className = hdrRowCls;
      hdrTr.style.cssText = 'border-top:2px solid rgba(168,85,247,.35);cursor:pointer';
      hdrTr.onclick = () => _toggleCollar(p.spread_id);
      hdrTr.innerHTML = `
        <td>
          <span data-collar-arrow="${esc(p.spread_id)}" style="display:inline-block;font-size:9px;color:var(--muted);transition:transform .18s;transform:${isCollapsed?'rotate(-90deg)':'rotate(0deg)'};margin-right:5px">▼</span>
          <strong style="font-size:13px;${collarSymColor}">${esc(sym)}</strong>
        </td>
        <td colspan="2" style="white-space:nowrap">
          <span class="badge b-call" style="font-size:10px;margin-right:2px">C</span><strong>$${callStrike}</strong>
          <span style="color:var(--muted);margin:0 4px">/</span>
          <span class="badge b-put" style="font-size:10px;margin-right:2px">P</span><strong>$${putStrike}</strong>
        </td>
        <td>${esc(expiry)}</td>
        <td>Long</td>
        <td>${qty}</td>
        <td>${dteHtml}</td>
        <td>${netDeltaHtml}</td>
        <td>—</td>
        <td>${ulHtml}</td>
        <td>${cg.call.ul_cost_basis > 0 ? '$' + parseFloat(cg.call.ul_cost_basis).toFixed(2) : '—'}</td>
        <td>—</td>
        <td>${collarPnlHtml}</td>
        <td>${hdrBadge} <span class="badge" style="background:rgba(168,85,247,.18);color:#a855f7;font-size:10px">COLLAR</span></td>
        <td colspan="3"></td>`;
      tbody.appendChild(hdrTr);
    }

    const flagged = p.flagged;
    const nFlags  = (p.reasons||[]).length;
    const rowCls  = flagged ? (nFlags >= 3 ? 'danger-row' : 'warn-row') : 'ok-row';
    const badge   = flagged
      ? (nFlags >= 3
          ? '<span class="badge badge-danger">&#9888; ' + nFlags + ' flags</span>'
          : '<span class="badge badge-warn">&#9888; ' + nFlags + ' flag' + (nFlags>1?'s':'') + '</span>')
      : '<span class="badge badge-ok">&#10003; OK</span>';

    const side = (p.net_qty||0) > 0 ? 'Short' : 'Long';
    const qty  = Math.abs(p.net_qty||0);
    // Net chain cash PnL: if a test trade exists, sim_chain_cash is the full-chain answer
    // with the test buyback baked in — the honest number across all rolls.
    let pnlAbs = null, pnlPct = null, legPct = null;
    const _isCollarLegRow = p.spread_id && _collars.has(p.spread_id);
    if (_isCollarLegRow) {
      // Collar legs share one chain_cash across both legs (correct for the combined
      // total shown on the collar header row above) — it can't be attributed to a
      // single leg without double-counting, so show this leg's own mark-to-market
      // PnL instead.
      pnlAbs = p.abs_pnl;
      pnlPct = p.pct_pnl;
    } else if (p.has_test_trade && p.sim_chain_cash != null) {
      pnlAbs = p.sim_chain_cash;
      pnlPct = p.chain_cash !== 0 && p.chain_cash != null
        ? (pnlAbs / Math.abs(p.chain_cash)) * 100 : null;
      legPct = p.pct_pnl ?? null;
    } else if (p.chain_cash != null && p.current_price != null && qty > 0) {
      const buybackCost = p.current_price * 100 * qty;
      pnlAbs = p.chain_cash - buybackCost;
      pnlPct = p.chain_cash !== 0 ? (pnlAbs / Math.abs(p.chain_cash)) * 100 : null;
    } else {
      pnlAbs = p.abs_pnl;
      pnlPct = p.pct_pnl;
    }
    const pnlAbsStr = pnlAbs == null ? '—'
      : '<span class="' + (pnlAbs>=0?'pnl-pos':'pnl-neg') + '">'
        + (pnlAbs>=0?'+':'') + '$' + pnlAbs.toFixed(2).replace(/\\B(?=(\\d{3})+(?!\\d))/g,',') + '</span>';
    const pnlPctStr = pnlPct == null ? '—'
      : (() => {
          const chainSpan = '<span class="' + (pnlPct>=0?'pnl-pos':'pnl-neg') + '">'
            + (pnlPct>=0?'+':'') + pnlPct.toFixed(1) + '%</span>';
          if (legPct == null) return chainSpan;
          const legSpan = '<span class="' + (legPct>=0?'pnl-pos':'pnl-neg') + '">'
            + (legPct>=0?'+':'') + legPct.toFixed(1) + '%</span>';
          return legSpan + ' <span style="opacity:.6;font-size:10px">(leg)</span>'
            + ' / ' + chainSpan + ' <span style="opacity:.6;font-size:10px">(chain)</span>';
        })();

    // ── Server reasons: strip ATR breach + dividend/extrinsic (we re-evaluate both client-side) ──
    const serverReasons = (p.reasons||[]).filter(r => !r.includes('ATR buffer') && !r.includes('early assignment'));
    const serverReasonItems = serverReasons.map(r => '<li>' + esc(r) + '</li>').join('');

    const posKey = [p.symbol, p.option_type, String(p.strike||''), p.expiry||''].join('|');
    const fingerprint = _flagFingerprint(p);
    const flagChanged = posKey in _prevFlags && _prevFlags[posKey] !== fingerprint;
    const cached = _recommendations[posKey];
    const actionCell = cached
      ? recBadge(cached.rec, posKey, cached.chainCash, cached.text, cached.runAt)
      : `<button onclick="analyzePosition('${posKey.replace(/'/g,"\\'")}', this)" style="font-size:11px;padding:4px 10px">Analyze</button>`;
    const actionTdAttr = ` data-poskey="${posKey.replace(/"/g,'&quot;')}"`;

    // ── Moneyness color for symbol ──
    const underlying = p.underlying;
    const strike = parseFloat(p.strike || 0);
    const optType = (p.option_type || '').toUpperCase();
    let symColor = '';
    if (underlying != null && strike > 0) {
      const isITM = (optType === 'CALL' && underlying > strike) || (optType === 'PUT' && underlying < strike);
      if (isITM) {
        symColor = 'color:var(--danger)';
      } else {
        const pctAway = Math.abs(underlying - strike) / underlying * 100;
        symColor = pctAway <= 3.0 ? 'color:var(--warn)' : 'color:var(--ok)';
      }
    }

    // ── Client-side ATR breach (re-evaluated on every live price update) ──
    let clientAtrBreach = false;
    if (underlying != null && p.buffer != null && strike > 0) {
      const gap = optType === 'PUT' ? underlying - strike : strike - underlying;
      clientAtrBreach = gap < p.buffer;
    }
    const prevAtrState = _prevClientAtr[posKey]; // undefined = never seen
    // Only highlight as changed/new after at least one price poll (avoids noise on initial load)
    const atrHighlight = _pricePollCount > 0 && clientAtrBreach
      && (prevAtrState === undefined || prevAtrState !== clientAtrBreach);
    _prevClientAtr[posKey] = clientAtrBreach;

    const atrBadgeInner = clientAtrBreach && p.atr != null
      ? `ATR breach (gap &lt; 1.5×ATR $${(p.buffer||0).toFixed(2)})`
      : null;
    const atrBadgeHtml = atrBadgeInner
      ? (atrHighlight
          ? `<li><span title="ATR breach status just changed" style="outline:2px solid var(--warn);border-radius:3px;padding:1px 4px;font-weight:700">&#9650; ${atrBadgeInner}</span></li>`
          : `<li>${atrBadgeInner}</li>`)
      : '';

    // ── Dividend ≥ extrinsic (client-side, re-evaluated on live price) ──
    const _isItm = (optType === 'CALL' && underlying > strike) || (optType === 'PUT' && underlying < strike);
    let divWarn = false, _extrinsic = null;
    if (p.dividend_amount != null && _isItm && underlying != null && strike > 0 && p.current_price != null) {
      const _intrinsic = optType === 'CALL'
        ? Math.max(0, underlying - strike)
        : Math.max(0, strike - underlying);
      _extrinsic = Math.max(0, p.current_price - _intrinsic);
      divWarn = p.dividend_amount >= _extrinsic;
    }
    const divWarnBadgeHtml = divWarn
      ? `<li><span style="color:var(--danger);font-weight:700" title="Early assignment risk">dividend ($${p.dividend_amount.toFixed(2)}) ≥ extrinsic ($${_extrinsic.toFixed(2)}) — early assignment risk</span></li>`
      : '';

    // ── U/L price tick direction (from live price poller) ──
    const sym = (p.symbol||'').toUpperCase();
    const tick = _ulPriceTick[sym];
    const ulPriceHtml = underlying == null ? '—'
      : tick === 'up'   ? `<span style="color:var(--ok);font-weight:700"  title="Price up since last poll">&#9650; $${underlying.toFixed(2)}</span>`
      : tick === 'down' ? `<span style="color:var(--danger);font-weight:700" title="Price down since last poll">&#9660; $${underlying.toFixed(2)}</span>`
      : `$${underlying.toFixed(2)}`;

    // ── Hide row logic ──
    const snapshot = _rowSnapshot(p);
    const hideEntry = _hiddenRows[posKey];
    let isHidden = false;
    if (hideEntry) {
      const prevSnap = hideEntry.snapshot || '';
      if (prevSnap === snapshot) {
        isHidden = true; // data unchanged — keep hidden
      } else {
        // data changed — unhide and update snapshot
        delete _hiddenRows[posKey];
        _saveHidden();
      }
    }

    const safeKey = posKey.replace(/'/g, "\\'");
    const checkboxCell = `<td style="text-align:center">
      <input type="checkbox" ${isHidden ? 'checked' : ''}
        onchange="_toggleHide('${safeKey}', this.checked, '${snapshot.replace(/'/g,"\\'")}', this.closest('tr'))"
        style="cursor:pointer;accent-color:var(--accent);width:14px;height:14px">
    </td>`;

    const tr = document.createElement('tr');
    tr.className = rowCls;
    if (isHidden) tr.style.display = 'none';
    const isCollarLeg = p.spread_id && _collars.has(p.spread_id);
    if (isCollarLeg) {
      tr.dataset.collarId = p.spread_id;
      if (_collarCollapsed.has(p.spread_id)) tr.style.display = 'none';
    }
    tr.innerHTML = `
      <td style="${isCollarLeg ? 'padding-left:22px' : ''}"><b style="${symColor}">${esc(p.symbol)}</b>${isCollarLeg ? '<br><span style="font-size:10px;color:#a855f7">collar</span>' : ''}</td>
      <td>${esc((p.option_type||'').toUpperCase())}</td>
      <td>$${parseFloat(p.strike||0).toFixed(2)}</td>
      <td>${esc(p.expiry||'')}</td>
      <td>${side}</td>
      <td>${qty}</td>
      <td>${p.dte==null ? '—' : `<span style="color:${p.dte>21?'var(--ok)':p.dte>=10?'var(--warn)':'var(--danger)'}">${p.dte}</span>`}</td>
      <td>${p.delta!=null ? `<span style="color:${Math.abs(p.delta)<0.40?'var(--ok)':Math.abs(p.delta)<=0.60?'var(--warn)':'var(--danger)'}">${(p.delta>=0?'+':'')+p.delta.toFixed(3)}</span>` : '—'}</td>
      <td>${_decayCellHtml(p.decay, p.dte, p.delta, p.symbol)}</td>
      <td>${ulPriceHtml}</td>
      <td>${(p.option_type||'').toLowerCase() === 'put' ? '—' : (p.ul_cost_basis > 0 ? '$' + parseFloat(p.ul_cost_basis).toFixed(2) : '—')}</td>
      <td>${(() => {
        if (p.current_price == null) return '—';
        const priceStr = '$' + p.current_price.toFixed(2);
        return divWarn
          ? `<span style="color:var(--danger);font-weight:700" title="Dividend ($${p.dividend_amount.toFixed(2)}) ≥ extrinsic value ($${_extrinsic.toFixed(2)}). Early assignment risk: the holder may exercise to capture the dividend before ex-div.">${priceStr} ⚠</span>`
          : priceStr;
      })()}</td>
      <td style="white-space:nowrap;line-height:1.4">${pnlPctStr !== '—' ? pnlPctStr : '—'}${pnlAbsStr !== '—' ? '<br><span style="font-size:10px;opacity:.85">' + pnlAbsStr + '</span>' : ''}</td>
      <td>${badge}${(() => {
        const allItems = serverReasonItems + atrBadgeHtml + divWarnBadgeHtml;
        if (!allItems) return '';
        const ulStyle = flagChanged
          ? 'outline:1px solid var(--warn);border-radius:3px;padding:2px 4px;margin-top:2px'
          : '';
        const ulTitle = flagChanged ? 'title="Flags changed since last refresh"' : '';
        return `<ul class="reasons" style="${ulStyle}" ${ulTitle}>${allItems}</ul>`;
      })()}</td>
      <td style="text-align:center"><input type="checkbox" ${p.dont_analyze ? 'checked' : ''}
        onchange="_toggleDontAnalyze('${posKey.replace(/'/g,"\\'")}', this.checked)"
        style="cursor:pointer;accent-color:var(--accent);width:14px;height:14px"></td>
      <td${actionTdAttr}>${actionCell}</td>
      <td style="text-align:center">${(() => {
        const hasAlert = !!_alerts[posKey];
        const a = _alerts[posKey] || {};
        const btnStyle = hasAlert
          ? 'background:var(--accent);color:#000;font-weight:700'
          : 'background:var(--surface2);color:var(--fg)';
        const btnTitle = hasAlert ? 'Alert set — click to edit' : 'Set price/delta alert';
        const bell = `<button onclick="openAlertModal('${safeKey}')" style="font-size:11px;padding:2px 8px;border:none;border-radius:4px;cursor:pointer;${btnStyle}" title="${btnTitle}">${hasAlert ? '🔔' : '🔕'}</button>`;
        const condBox = hasAlert
          ? `<label style="display:block;margin-top:3px;font-size:10px;color:var(--muted);cursor:pointer;white-space:nowrap" title="Checked = AND, unchecked = OR">
               <input type="checkbox" ${a.condition === 'and' ? 'checked' : ''}
                 onchange="_toggleAlertCond('${safeKey}', this.checked)"
                 style="margin-right:2px;accent-color:var(--accent);cursor:pointer">AND
             </label>`
          : '';
        return bell + condBox;
      })()}</td>
      ${checkboxCell}`;
    tbody.appendChild(tr);
    _prevFlags[posKey] = fingerprint;
  }
  applyCollapseOk();

  // Auto-analyze flagged positions that don't have a cached recommendation yet
  for (const p of rows) {
    if (!p.flagged) continue;
    if (p.dont_analyze) continue;
    const posKey = [p.symbol, p.option_type, String(p.strike||''), p.expiry||''].join('|');
    if (_recommendations[posKey]) continue;
    if (_autoQueued.has(posKey)) continue;
    const cell = _actionCell(posKey);
    if (!cell) continue;
    const btn = cell.querySelector('button');
    if (!btn || btn.disabled) continue;
    _autoQueued.add(posKey);
    analyzePosition(posKey, btn);
  }

  // Auto-rerun analysis when flag tier, delta (±0.05), or underlying (±$0.50) changes
  for (const p of rows) {
    if (p.dont_analyze) continue;
    const posKey = [p.symbol, p.option_type, String(p.strike||''), p.expiry||''].join('|');
    const newFP  = _analysisFP(p);
    const oldFP  = _prevAnalysisFP[posKey];
    _prevAnalysisFP[posKey] = newFP;
    if (oldFP === undefined || oldFP === newFP) continue;
    if (!_recommendations[posKey]) continue;
    const now = Date.now();
    if (_autoRerunAt[posKey] && now - _autoRerunAt[posKey] < _AUTO_RERUN_COOLDOWN_MS) continue;
    _autoRerunAt[posKey] = now;
    console.log('[auto-rerun]', posKey, ':', oldFP, '→', newFP);
    // Force a fresh server-side recompute without clearing the existing cache
    // entry first — the old (still-correct-enough) analysis stays visible on
    // the page until the new one is ready, instead of a blank "not found" gap
    // if this fetch never completes (backgrounded/closed tab, network drop).
    analyzePosition(posKey, null, true);
  }
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function renderSkipped(skipped) {
  const el = document.getElementById('skipped-section');
  if (!skipped.length) { el.innerHTML = ''; return; }
  el.innerHTML = '<div class="section-title" style="margin-top:24px">Skipped</div>'
    + '<div class="skipped-box"><ul>'
    + skipped.map(s => '<li>' + esc(s) + '</li>').join('')
    + '</ul></div>';
}

function applyCollapseOk() {
  const hide = document.getElementById('collapse-ok').checked;
  document.querySelectorAll('#pos-body tr.ok-row').forEach(tr => {
    tr.style.display = hide ? 'none' : '';
  });
}

function toggleTheme() {
  const dark = document.getElementById('theme-toggle').checked;
  const theme = dark ? 'dark' : 'light';
  document.documentElement.setAttribute('data-theme', theme);
  try { localStorage.setItem('theme', theme); } catch {}
}
document.getElementById('theme-toggle').checked = document.documentElement.getAttribute('data-theme') !== 'light';

// Persist recommendations across browser refreshes via localStorage
const _LS_KEY = 'optionsRecs';
function _loadRecs() {
  try { return JSON.parse(localStorage.getItem(_LS_KEY) || '{}'); } catch { return {}; }
}
function _saveRecs(recs) {
  try { localStorage.setItem(_LS_KEY, JSON.stringify(recs)); } catch {}
}
const _recommendations = _loadRecs(); // posKey -> {rec}
let _inflight       = new Set(); // pos_keys currently being analyzed server-side
let _unbornInflight = new Set(); // "TICKER|STRAT" row keys with unborn analysis running
let _autoQueued     = new Set(); // pos_keys auto-triggered for analysis this session
let _unbornAutoRecalcDone = false;

// ── Flag-change detection ────────────────────────────────────────────────────
// Stores the flag fingerprint from the previous render so we can highlight
// anything that changed on the next fetch.
let _prevFlags      = {}; // posKey → "flagged|nFlags|reason0;reason1;..."
let _prevAnalysisFP = {}; // posKey → meaningful-state fingerprint from last render
let _autoRerunAt    = {}; // posKey → Date.now() of last auto-rerun
const _AUTO_RERUN_COOLDOWN_MS = 10 * 60 * 1000; // 10 minutes

// ── Live price polling state ─────────────────────────────────────────────────
let _prevUlPrices  = (() => { try { return JSON.parse(localStorage.getItem(_LS_PR_PRICES) || '{}'); } catch { return {}; } })();
let _ulPriceTick   = {};  // {ticker: 'up'|'down'|null} — direction since last poll
let _prevClientAtr = {};  // {posKey: bool} — ATR breach state at last render
let _pricePollCount = 0;  // increments each time fetchPrices() completes
let _prevVix       = null; // VIX at last poll

function _flagFingerprint(p) {
  return [(p.flagged ? '1' : '0'), (p.reasons||[]).length, (p.reasons||[]).join(';')].join('|');
}

function _analysisFP(p) {
  // Buckets: flag tier (ok/warn/danger), delta ±0.05, underlying ±$0.50
  const tier = !p.flagged ? 0 : (p.reasons||[]).length >= 3 ? 2 : 1;
  const dBkt = (Math.round((p.delta||0) * 20) / 20).toFixed(2);
  const uBkt = (Math.round((p.underlying||0) * 2) / 2).toFixed(1);
  return tier + '|' + dBkt + '|' + uBkt;
}
// ─────────────────────────────────────────────────────────────────────────────

// ── Hidden rows persistence ──────────────────────────────────────────────────
const _LS_HIDE_KEY = 'optionsHiddenRows';
function _loadHidden() {
  try { return JSON.parse(localStorage.getItem(_LS_HIDE_KEY) || '{}'); } catch { return {}; }
}
function _saveHidden() {
  try { localStorage.setItem(_LS_HIDE_KEY, JSON.stringify(_hiddenRows)); } catch {}
}
const _hiddenRows = _loadHidden(); // posKey -> {snapshot}

function _rowSnapshot(p) {
  // A compact fingerprint of the fields that matter for change detection
  return [
    p.strike, p.expiry,
    p.underlying != null ? p.underlying.toFixed(2) : '',
    p.delta     != null ? p.delta.toFixed(3)      : '',
    p.abs_pnl   != null ? p.abs_pnl.toFixed(2)    : '',
    p.flagged ? '1' : '0',
    (p.reasons||[]).length
  ].join('|');
}

function unhideAll() {
  for (const key of Object.keys(_hiddenRows)) delete _hiddenRows[key];
  _saveHidden();
  document.querySelectorAll('#pos-body tr').forEach(tr => {
    tr.style.display = '';
    const cb = tr.querySelector('input[type=checkbox]');
    if (cb) cb.checked = false;
  });
}

function _toggleHide(posKey, checked, snapshot, trEl) {
  if (checked) {
    _hiddenRows[posKey] = {snapshot};
    if (trEl) trEl.style.display = 'none';
  } else {
    delete _hiddenRows[posKey];
    if (trEl) trEl.style.display = '';
  }
  _saveHidden();
}
// ────────────────────────────────────────────────────────────────────────────

// ── Don't-analyze toggle (server-persisted — skips scheduled auto-analysis
// for this position; flags still light up, manual Analyze still works) ──────
async function _toggleDontAnalyze(posKey, checked) {
  const p = _data.find(row => [row.symbol, row.option_type, String(row.strike||''), row.expiry||''].join('|') === posKey);
  if (p) p.dont_analyze = checked;
  try {
    await fetch('/api/dont-analyze', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({position_key: posKey, checked})
    });
  } catch (e) { console.error('dont-analyze toggle failed', e); }
}
// ────────────────────────────────────────────────────────────────────────────

// ── Collar expand/collapse ────────────────────────────────────────────────────
const _collarCollapsed = new Set(); // spread_ids that are currently collapsed

function _toggleCollar(sid) {
  const collapsing = !_collarCollapsed.has(sid);
  if (collapsing) _collarCollapsed.add(sid); else _collarCollapsed.delete(sid);
  document.querySelectorAll(`tr[data-collar-id]`).forEach(tr => {
    if (tr.dataset.collarId === sid) tr.style.display = collapsing ? 'none' : '';
  });
  const arrow = document.querySelector(`[data-collar-arrow="${sid}"]`);
  if (arrow) arrow.style.transform = collapsing ? 'rotate(-90deg)' : 'rotate(0deg)';
}
// ── Alert system ─────────────────────────────────────────────────────────────
const _LS_ALERT_KEY = 'optionAlerts';
let _alerts = {};          // posKey -> {priceDir, priceVal, deltaDir, deltaVal, condition}
let _alertFired = {};      // posKey -> last-fired epoch ms (cooldown)
let _divAlertFired = {};   // posKey -> last-fired epoch ms for dividend/extrinsic auto-alerts
const _ALERT_COOLDOWN_MS = 5 * 60 * 1000; // 5 min between repeat alerts

function _loadAlerts() {
  try { return JSON.parse(localStorage.getItem(_LS_ALERT_KEY) || '{}'); } catch { return {}; }
}
function _saveAlerts() {
  try { localStorage.setItem(_LS_ALERT_KEY, JSON.stringify(_alerts)); } catch {}
}
_alerts = _loadAlerts();

function openAlertModal(posKey) {
  const a = _alerts[posKey] || {};
  document.getElementById('alert-modal-title').textContent =
    'Set Alert: ' + posKey.replace(/\\|/g, ' · ');
  document.getElementById('alert-pos-key').value = posKey;
  document.getElementById('alert-price-dir').value = a.priceDir || 'above';
  document.getElementById('alert-price-val').value = a.priceVal != null ? a.priceVal : '';
  document.getElementById('alert-delta-dir').value = a.deltaDir || 'above';
  document.getElementById('alert-delta-val').value = a.deltaVal != null ? a.deltaVal : '';
  const cond = a.condition || 'or';
  document.getElementById('alert-cond-' + cond).checked = true;
  document.getElementById('alert-modal-overlay').style.display = 'flex';
  document.getElementById('alert-price-val').focus();
}

function closeAlertModal() {
  document.getElementById('alert-modal-overlay').style.display = 'none';
}

function saveAlert() {
  const posKey  = document.getElementById('alert-pos-key').value;
  const priceRaw = document.getElementById('alert-price-val').value.trim();
  const deltaRaw = document.getElementById('alert-delta-val').value.trim();
  if (!priceRaw && !deltaRaw) {
    delete _alerts[posKey];
    _saveAlerts();
    closeAlertModal();
    renderTable();
    return;
  }
  _alerts[posKey] = {
    priceDir:  document.getElementById('alert-price-dir').value,
    priceVal:  priceRaw ? parseFloat(priceRaw) : null,
    deltaDir:  document.getElementById('alert-delta-dir').value,
    deltaVal:  deltaRaw ? parseFloat(deltaRaw) : null,
    condition: document.querySelector('input[name="alert-cond"]:checked').value,
  };
  _saveAlerts();
  closeAlertModal();
  renderTable();
}

function clearAlert() {
  const posKey = document.getElementById('alert-pos-key').value;
  delete _alerts[posKey];
  delete _alertFired[posKey];
  _saveAlerts();
  closeAlertModal();
  renderTable();
}

function _toggleAlertCond(posKey, checked) {
  if (!_alerts[posKey]) return;
  _alerts[posKey].condition = checked ? 'and' : 'or';
  _saveAlerts();
}

function _checkAlerts() {
  if (!_data || !_data.length) return;
  const now = Date.now();
  for (const p of _data) {
    const posKey = [p.symbol, p.option_type, p.strike, p.expiry].join('|');

    // ── Dividend/extrinsic auto-alert (fires regardless of configured alert) ──
    if ((now - (_divAlertFired[posKey] || 0)) >= _ALERT_COOLDOWN_MS) {
      const _ul    = p.underlying;
      const _str   = parseFloat(p.strike || 0);
      const _ot    = (p.option_type || '').toUpperCase();
      const _isItm = (_ot === 'CALL' && _ul > _str) || (_ot === 'PUT' && _ul < _str);
      if (_isItm && p.dividend_amount != null && _ul != null && _str > 0 && p.current_price != null) {
        const _intr = _ot === 'CALL' ? Math.max(0, _ul - _str) : Math.max(0, _str - _ul);
        const _extr = Math.max(0, p.current_price - _intr);
        if (p.dividend_amount >= _extr) {
          _divAlertFired[posKey] = now;
          const sym = (p.symbol||'').toUpperCase();
          const msg = `${sym} ${_ot} $${p.strike} ${p.expiry}: `
            + `dividend ($${p.dividend_amount.toFixed(2)}) ≥ extrinsic ($${_extr.toFixed(2)}) — early assignment risk`;
          _sendPushoverAlert(msg);
        }
      }
    }

    // ── User-configured price/delta alerts ───────────────────────────────────
    const a = _alerts[posKey];
    if (!a) continue;
    if ((now - (_alertFired[posKey] || 0)) < _ALERT_COOLDOWN_MS) continue;

    const ulPrice = p.underlying;
    const delta   = p.delta;

    const _test = (dir, threshold, value) => {
      if (threshold == null || value == null) return null;
      return dir === 'above' ? value > threshold : value < threshold;
    };

    const priceTrip = _test(a.priceDir, a.priceVal, ulPrice);
    const deltaTrip = _test(a.deltaDir, a.deltaVal, delta);

    let triggered = false;
    if (a.condition === 'and') {
      const checks = [];
      if (a.priceVal != null) checks.push(priceTrip);
      if (a.deltaVal != null) checks.push(deltaTrip);
      triggered = checks.length > 0 && checks.every(Boolean);
    } else {
      const checks = [];
      if (a.priceVal != null) checks.push(priceTrip);
      if (a.deltaVal != null) checks.push(deltaTrip);
      triggered = checks.some(Boolean);
    }

    if (triggered) {
      _alertFired[posKey] = now;
      const parts = [];
      if (a.priceVal != null && priceTrip)
        parts.push(`underlying ${a.priceDir} $${a.priceVal} (now $${(ulPrice||0).toFixed(2)})`);
      if (a.deltaVal != null && deltaTrip)
        parts.push(`delta ${a.deltaDir} ${a.deltaVal} (now ${(delta||0).toFixed(3)})`);
      const sym = (p.symbol||'').toUpperCase();
      const msg = `${sym} ${(p.option_type||'').toUpperCase()} $${p.strike} ${p.expiry}: ` + parts.join(', ');
      _sendPushoverAlert(msg);
    }
  }
}

async function _sendPushoverAlert(message) {
  try {
    const r = await fetch('/api/send-alert', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message}),
    });
    if (!r.ok) console.warn('Pushover alert failed:', await r.text());
  } catch(e) { console.warn('Alert send error:', e); }
}
// ─────────────────────────────────────────────────────────────────────────────

// ── Auto-refresh scheduler ───────────────────────────────────────────────────
// Uses a 30-second heartbeat rather than long setInterval timers, which
// browsers throttle heavily (especially in background tabs).
const _LS_AR_KEY  = 'optionsAutoRefresh';
const _LS_AR_LAST = 'optionsAutoRefreshLastRun'; // epoch ms of last successful fetch
let _arHeartbeat  = null;
let _arIntervalSecs = 0;

function _isMarketHours() {
  const now = new Date();
  const day = now.getDay();
  if (day === 0 || day === 6) return false;
  const mins = now.getHours() * 60 + now.getMinutes();
  return mins >= 9 * 60 + 30 && mins < 16 * 60;
}

function _isMarketOpen() {
  // Exactly at 9:30 or 16:00 Mon-Fri (within the current heartbeat window)
  const now = new Date();
  if (now.getDay() === 0 || now.getDay() === 6) return false;
  const mins = now.getHours() * 60 + now.getMinutes();
  return mins === 9 * 60 + 30 || mins === 16 * 60;
}

function _arTick() {
  if (_arIntervalSecs <= 0) return;
  const now = Date.now();

  // Always fire at open/close bell (checked every 30s, fires once per minute)
  if (_isMarketOpen()) {
    const bellKey = 'arBellFired_' + new Date().toISOString().slice(0,16); // per-minute key
    if (!sessionStorage.getItem(bellKey)) {
      sessionStorage.setItem(bellKey, '1');
      fetchData().then(() => localStorage.setItem(_LS_AR_LAST, String(Date.now())));
      return;
    }
  }

  if (!_isMarketHours()) return;

  const last = parseInt(localStorage.getItem(_LS_AR_LAST) || '0', 10);
  const elapsed = (now - last) / 1000; // seconds since last fetch
  if (elapsed >= _arIntervalSecs) {
    fetchData().then(() => localStorage.setItem(_LS_AR_LAST, String(Date.now())));
  }
}

function setAutoRefresh(val) {
  localStorage.setItem(_LS_AR_KEY, val);
  clearInterval(_arHeartbeat);
  _arHeartbeat = null;
  _arIntervalSecs = parseInt(val, 10) || 0;

  if (_arIntervalSecs > 0) {
    _arHeartbeat = setInterval(_arTick, 30_000); // heartbeat every 30s
  }
}

function _initAutoRefresh() {
  const saved = localStorage.getItem(_LS_AR_KEY) || '0';
  const sel = document.getElementById('auto-refresh-sel');
  if (sel) {
    const opt = [...sel.options].find(o => o.value === saved);
    sel.value = opt ? saved : '0';
  }
  setAutoRefresh(saved);
}
// ─────────────────────────────────────────────────────────────────────────────

// Proposed trades — server-side persistence via /api/unborn-rows
const _unbornRows = {};          // populated on load from server
const _deletedUnbornKeys = new Set(); // rows explicitly deleted this session

async function _loadUnbornFromServer() {
  try {
    const r = await fetch('/api/unborn-rows');
    if (!r.ok) return;
    const d = await r.json();
    Object.assign(_unbornRows, d);
    // Seed _prevUlPrices from stored ul_price so first poll can show ticks
    for (const row of Object.values(_unbornRows)) {
      const sym = (row.symbol || '').toUpperCase();
      if (_prevUlPrices[sym] == null && row.ul_price != null) {
        _prevUlPrices[sym] = row.ul_price;
      }
    }
    _renderUnbornTable();
    _renderFormerTable();
    if (!_unbornAutoRecalcDone) {
      _unbornAutoRecalcDone = true;
      setTimeout(() => {
        document.querySelectorAll('#unborn-chain button[data-key]').forEach(btn => {
          if (btn.textContent.trim() === 'Recalc' && !btn.disabled) {
            const row = _unbornRows[btn.dataset.key];
            if (!row || !row._run_at) btn.click();
          }
        });
      }, 0);
    }
  } catch(e) { /* silently ignore */ }
}

function _parseRunAt(s) {
  if (!s) return 0;
  const m = s.match(/(\d+)\/(\d+)\s+(\d+):(\d+)\s+(AM|PM)/i);
  if (!m) return 0;
  let [, mon, day, hr, min, ampm] = m;
  hr = parseInt(hr);
  if (ampm.toUpperCase() === 'PM' && hr !== 12) hr += 12;
  if (ampm.toUpperCase() === 'AM' && hr === 12) hr = 0;
  return new Date(new Date().getFullYear(), parseInt(mon) - 1, parseInt(day), hr, parseInt(min)).getTime();
}

async function _saveUnbornToServer() {
  // Merge with server state before saving:
  // - Rows the browser doesn't have: keep server's version (unless explicitly deleted)
  // - Rows both have: keep whichever has the newer _run_at, preserving browser's _ul_cost_basis
  let payload = {..._unbornRows};
  try {
    const sr = await fetch('/api/unborn-rows');
    if (sr.ok) {
      const serverRows = await sr.json();
      for (const [k, v] of Object.entries(serverRows)) {
        if (_deletedUnbornKeys.has(k)) continue;
        if (!(k in payload)) {
          payload[k] = v;
        } else if (_parseRunAt(v._run_at) > _parseRunAt(payload[k]._run_at)) {
          payload[k] = Object.assign({}, v, {_ul_cost_basis: payload[k]._ul_cost_basis});
          _unbornRows[k] = payload[k]; // keep browser in sync
        }
      }
    }
  } catch (_) {}
  try {
    const r = await fetch('/api/unborn-rows', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    if (!r.ok) {
      const txt = await r.text().catch(() => r.status);
      console.error('[unborn-save] Server error:', txt);
      const bar = document.getElementById('error-bar');
      if (bar) { bar.textContent = 'Failed to save unborn rows: ' + txt; bar.style.display = 'block'; }
    }
  } catch(e) {
    console.error('[unborn-save] Network error:', e.message);
    const bar = document.getElementById('error-bar');
    if (bar) { bar.textContent = 'Failed to save unborn rows: ' + e.message; bar.style.display = 'block'; }
  }
}

function _renderUnbornTable() {
  try { _renderUnbornTableInner(); } catch(e) {
    const el = document.getElementById('unborn-chain');
    if (el) el.innerHTML = `<p style="color:var(--danger)">Render error: ${e.message}</p>`;
    console.error('[unborn render]', e);
  }
}
function _renderUnbornTableInner() {
  const el = document.getElementById('unborn-chain');
  const today = new Date(); today.setHours(0,0,0,0);
  // Filter out rows whose option has already expired
  const keys = Object.keys(_unbornRows).filter(k => {
    const exp = _unbornRows[k].expiry;
    if (!exp) return true;
    return new Date(exp + 'T00:00:00') >= today;
  });
  if (!keys.length) { el.innerHTML = ''; return; }
  const fv = (v, d, p='') => v == null ? '—' : p + parseFloat(v).toFixed(d);

  // Sort
  const _ubCols = [
    k => (_unbornRows[k].symbol||''),
    k => ((_unbornRows[k].option_type||'').toUpperCase()),
    k => parseFloat(_unbornRows[k].strike||0),
    k => (_unbornRows[k].expiry||''),
    k => (_unbornRows[k].side||''),
    k => (_unbornRows[k]._qty||1),
    k => (_unbornRows[k].dte??999),
    k => (_unbornRows[k].delta??-99),
    k => (_unbornRows[k].ul_price??0),
    k => (_unbornRows[k].opt_price??0),
    k => (_unbornRows[k]._ul_cost_basis??0),
    k => (_unbornRows[k].ideal_entry||''),
  ];
  if (_ubSortCol < _ubCols.length) {
    const fn = _ubCols[_ubSortCol];
    keys.sort((a,b) => { const av=fn(a),bv=fn(b); return _ubSortDir*(av<bv?-1:av>bv?1:0); });
  }

  const thStyle = 'cursor:pointer;user-select:none;';
  const thHdr = (label, i) => {
    const arrow = _ubSortCol===i ? (_ubSortDir===1?' ▲':' ▼') : '';
    return `<th style="${thStyle}" onclick="_ubSort(${i})">${label}${arrow}</th>`;
  };

  const rows = keys.map(k => {
    const c = _unbornRows[k];
    const qty = c._qty || 1;
    const detailHref = c._ubKey ? `/unborn/${c._ubKey}` : null;
    const symCell = detailHref
      ? `<a href="${detailHref}" target="_blank" style="color:var(--accent);text-decoration:none"><b>${esc(c.symbol)}</b></a>`
      : `<b>${esc(c.symbol)}</b>`;
    const _blank = c._do_nothing || c._no_opt;
    return `<tr>
      <td>${symCell}</td>
      <td>${esc((c.option_type||'').toUpperCase())}</td>
      <td>${_blank ? '—' : fv(c.strike,2,'$')}</td>
      <td>${_blank ? '—' : esc(c.expiry||'—')}</td>
      <td>${_blank ? '—' : esc(c.side||'Short')}</td>
      <td>${_blank ? '—' : qty}</td>
      <td>${_blank ? '—' : (c.dte??'—')}</td>
      <td>${_blank ? '—' : (c.delta!=null?(c.delta>=0?'+':'')+parseFloat(c.delta).toFixed(3):'—')}</td>
      <td>${(() => {
        if (c.ul_price == null) return '—';
        const sym  = (c.symbol||'').toUpperCase();
        const tick = _ulPriceTick[sym];
        const priceStr = '$' + parseFloat(c.ul_price).toFixed(2);
        return tick === 'up'
          ? '<span style="color:var(--ok);font-weight:700" title="Price up since last poll">&#9650; ' + priceStr + '</span>'
          : tick === 'down'
          ? '<span style="color:var(--danger);font-weight:700" title="Price down since last poll">&#9660; ' + priceStr + '</span>'
          : priceStr;
      })()}</td>
      <td>${_blank ? '—' : fv(c.opt_price,2,'$')}</td>
      <td>${c._ul_cost_basis > 0 ? fv(c._ul_cost_basis,2,'$') : '—'}</td>
      <td style="font-size:11px;white-space:normal;max-width:130px;${c._no_opt?'color:var(--muted);font-style:italic':c._do_nothing?'color:var(--warn);font-weight:700':'color:var(--muted)'}">${esc(c.ideal_entry||'—')}</td>
      <td style="white-space:nowrap">
        ${_blank ? '' : '<button data-key="' + esc(k) + '" onclick="placeTrade(this.dataset.key,this)" style="font-size:11px;padding:3px 8px;background:var(--accent);color:#fff;border:none;border-radius:4px;cursor:pointer">Trade</button>'}
        <button data-key="${esc(k)}" onclick="deleteUnbornRow(this.dataset.key)" style="font-size:11px;padding:3px 6px;background:var(--danger-bg);color:var(--danger);border:1px solid var(--danger);border-radius:4px;cursor:pointer;${_blank?'':'margin-left:4px'}">&times;</button>
        ${c._no_opt ? '' : (() => { const recalcRunning = _unbornInflight.has(k); return '<button data-key="' + esc(k) + '" ' + (recalcRunning ? 'disabled ' : '') + 'onclick="recalcUnbornRow(this.dataset.key,this)" title="Re-run analysis" style="font-size:11px;padding:3px 8px;background:transparent;color:var(--muted);border:1px solid var(--border);border-radius:4px;cursor:pointer;margin-left:4px;display:inline-flex;align-items:center;gap:4px">' + (recalcRunning ? '<span style="width:10px;height:10px;border:2px solid currentColor;border-top-color:transparent;border-radius:50%;animation:spin 0.7s linear infinite;display:inline-block;flex-shrink:0"></span>Recalculating…' : 'Recalc') + '</button>'; })()}
        ${c._run_at ? '<span style="font-size:10px;color:var(--muted);margin-left:6px;white-space:nowrap">' + esc(c._run_at) + '</span>' : ''}
      </td>
    </tr>`;
  }).join('');
  el.innerHTML = `<table>
    <thead><tr>
      ${thHdr('Symbol',0)}${thHdr('Type',1)}${thHdr('Strike',2)}${thHdr('Expiry',3)}
      ${thHdr('Side',4)}${thHdr('Qty',5)}${thHdr('DTE',6)}${thHdr('&Delta;',7)}
      ${thHdr('U/L Price',8)}${thHdr('Opt Price',9)}${thHdr('Cost Basis',10)}${thHdr('Ideal Entry',11)}<th></th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

async function retryAnalysis(key, btn) {
  // Clear server-side cache entry so the analysis re-runs fresh
  await fetch('/api/reset-cache-entry', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({position_key: key})
  });
  delete _recommendations[key];
  _saveRecs(_recommendations);
  analyzePosition(key, btn);
}

async function _setUnbornCostBasis(key, val) {
  const v = parseFloat(val);
  if (!_unbornRows[key] || isNaN(v) || v <= 0) return;
  _unbornRows[key]._ul_cost_basis = v;
  await _saveUnbornToServer();
  _renderUnbornTable();
}

// ── Former Positions ──────────────────────────────────────────────────────────
let _formerPositions = [];
let _formerSortCol = 0, _formerSortDir = 1;

const _LS_FMR_HIDE_KEY = 'optionsHiddenFormerRows';
function _loadHiddenFormer() {
  try { return new Set(JSON.parse(localStorage.getItem(_LS_FMR_HIDE_KEY) || '[]')); } catch { return new Set(); }
}
function _saveHiddenFormer() {
  try { localStorage.setItem(_LS_FMR_HIDE_KEY, JSON.stringify([..._hiddenFormerRows])); } catch {}
}
const _hiddenFormerRows = _loadHiddenFormer();
function _toggleHideFormer(key, checked) {
  if (checked) _hiddenFormerRows.add(key);
  else _hiddenFormerRows.delete(key);
  _saveHiddenFormer();
  _renderFormerTable();
}

function unhideAllFormer() {
  _hiddenFormerRows.clear();
  _saveHiddenFormer();
  _renderFormerTable();
}

async function _loadFormerPositions() {
  try {
    const r = await fetch('/api/former-positions');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    _formerPositions = await r.json();
    // Seed _prevUlPrices so the price-poll tick logic works for former tickers
    for (const p of _formerPositions) {
      const sym = (p.symbol || '').toUpperCase();
      if (p.ul_price != null && _prevUlPrices[sym] == null) _prevUlPrices[sym] = p.ul_price;
    }
    _renderFormerTable();
  } catch(e) {
    const el = document.getElementById('former-chain');
    if (el) el.innerHTML = `<p style="color:var(--danger);font-size:12px">Former positions load error: ${esc(e.message)}</p>`;
    console.error('[former-positions]', e);
  }
}

function _renderFormerTable() {
  const el = document.getElementById('former-chain');
  if (!el) return;
  try {
    _renderFormerTableInner(el);
  } catch(e) {
    el.innerHTML = `<p style="color:var(--danger);font-size:12px">Former render error: ${esc(e.message)}</p>`;
    console.error('[former render]', e);
  }
}

function _renderFormerTableInner(el) {
  const unbornTickers = new Set(Object.values(_unbornRows).map(r => (r.symbol||'').toUpperCase()));
  const base = _formerPositions.filter(p => !unbornTickers.has(p.symbol.toUpperCase()));
  const rows = _posSearch ? base.filter(p => p.symbol.toUpperCase().includes(_posSearch)) : base;
  if (!base.length) { el.innerHTML = ''; return; }
  if (!rows.length) {
    el.innerHTML = '<div class="section-title" style="margin-top:0;margin-bottom:8px">Former Positions</div>';
    return;
  }

  const fmtCb = v => (v > 0) ? '$' + parseFloat(v).toFixed(2) : '—';
  const sortFns = [
    p => p.symbol, p => p.strat, p => 0, p => '', p => '',
    p => p.qty, p => 999, p => -99, p => 0, p => 0,
    p => p.ul_cost_basis ?? 0, p => '', p => p.earnings_date || '',
  ];
  const fn = sortFns[_formerSortCol] || (p => 0);
  rows.sort((a, b) => {
    const av = fn(a), bv = fn(b);
    return _formerSortDir * (av < bv ? -1 : av > bv ? 1 : 0);
  });

  const arrow = (i) => _formerSortCol === i ? (_formerSortDir === 1 ? ' ▲' : ' ▼') : '';
  const th = (label, i) => `<th onclick="_fmrSort(${i})">${label}${arrow(i)}</th>`;

  const hdrs = [th('Symbol',0),th('Type',1),th('Strike',2),th('Expiry',3),th('Side',4),
                th('Qty',5),th('DTE',6),th('Delta',7),th('U/L Price',8),th('Opt Price',9),
                th('Cost Basis',10),th('Ideal Entry',11),th('Earnings',12),
                '<th>Actions</th>',
                '<th style="text-align:center;cursor:pointer" onclick="unhideAllFormer()" title="Click to unhide all">Hide</th>'].join('');

  const trs = rows.map(p => {
    const sym = esc(p.symbol);
    const rawKey = p.symbol + '|' + p.strat;
    const key = esc(rawKey);
    const hidden = _hiddenFormerRows.has(rawKey);
    const earningsCell = (!p.earnings_date || p.earnings_date === 'Unknown')
      ? '—'
      : `${esc(p.earnings_date)}${p.earnings_source === 'estimated' ? ' <span style="color:var(--muted);font-size:10px" title="No confirmed date from the exchange yet — estimated from the last report + one quarter">(est.)</span>' : ''}`;
    return `<tr${hidden ? ' style="display:none"' : ''}>
      <td><b>${sym}</b></td>
      <td>${esc(p.strat)}</td>
      <td>—</td><td>—</td><td>—</td>
      <td>${p.qty}</td>
      <td>—</td><td>—</td>
      <td>${(() => { if (p.ul_price == null) return '—'; const t = _ulPriceTick[(p.symbol||'').toUpperCase()]; const pr = '$' + parseFloat(p.ul_price).toFixed(2); return t === 'up' ? `<span style="color:var(--ok);font-weight:700" title="Price up since last poll">&#9650; ${pr}</span>` : t === 'down' ? `<span style="color:var(--danger);font-weight:700" title="Price down since last poll">&#9660; ${pr}</span>` : pr; })()}</td>
      <td>—</td>
      <td>${fmtCb(p.ul_cost_basis)}</td>
      <td>—</td>
      <td>${earningsCell}</td>
      <td><button data-key="${key}" onclick="analyzeFormerPosition(this.dataset.key,this)"
          style="font-size:11px;padding:3px 8px;background:var(--accent);color:#fff;border:none;border-radius:4px;cursor:pointer">Analyze</button></td>
      <td style="text-align:center">
        <input type="checkbox" ${hidden ? 'checked' : ''}
          onchange="_toggleHideFormer('${rawKey.replace(/'/g,"\\'")}',this.checked)"
          style="cursor:pointer;accent-color:var(--accent);width:14px;height:14px">
      </td>
    </tr>`;
  }).join('');

  el.innerHTML = `<div class="section-title" style="margin-top:0;margin-bottom:8px">Former Positions</div>
    <table><thead><tr>${hdrs}</tr></thead><tbody>${trs}</tbody></table>`;
}

function _fmrSort(col) {
  if (_formerSortCol === col) _formerSortDir = -_formerSortDir;
  else { _formerSortCol = col; _formerSortDir = 1; }
  _renderFormerTable();
}

async function analyzeFormerPosition(key, btn) {
  const parts = key.split('|');
  const ticker = parts[0], strat = parts[1] || 'CC';
  const pos = _formerPositions.find(p => p.symbol === ticker && p.strat === strat);
  const qty = pos ? pos.qty : 1;
  const cb  = pos ? (pos.ul_cost_basis || 0) : 0;

  btn.disabled = true;
  btn.innerHTML = '<span style="display:inline-block;width:11px;height:11px;border:2px solid currentColor;border-top-color:transparent;border-radius:50%;animation:spin 0.7s linear infinite;vertical-align:middle"></span>';

  try {
    let d;
    let forceNow = true;
    while (true) {
      const r = await fetch('/api/unborn', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ticker, qty, strat, cost_basis: cb, force: forceNow})
      });
      // Only force the very first request in this sequence — re-sending
      // force:true on every retry bypasses the server's cache/in-flight
      // check every time, so a fast-completing analysis (e.g. "no option
      // support") never gets a chance to be returned: each poll finds
      // nothing in-flight (the prior one already finished) and force
      // skips the cache hit too, so it just restarts forever. Confirmed
      // live on PPIH — polled every ~5s in a genuine infinite loop.
      forceNow = false;
      let raw;
      try { raw = await r.text(); d = JSON.parse(raw); }
      catch { throw new Error('Non-JSON: ' + (raw||'').slice(0,200)); }
      if (r.status === 202 && d.status === 'in_progress') {
        await new Promise(res => setTimeout(res, (d.retry_after || 5) * 1000));
        continue;
      }
      break;
    }
    if (d.error) throw new Error(d.error);
    const chain = d.chain || [];
    if (chain.length) {
      const ubKey = encodeURIComponent(ticker + '|' + strat + '|' + qty);
      const _runAt = new Date().toLocaleString('en-US', {timeZone:'America/New_York',month:'numeric',day:'numeric',hour:'numeric',minute:'2-digit',hour12:true}).replace(',','') + ' ET';
      _unbornRows[key] = Object.assign({}, chain[0], {_qty: qty, _ubKey: ubKey, _ul_cost_basis: (d.ul_cost_basis ?? cb) || null, _run_at: _runAt});
      _deletedUnbornKeys.delete(key);
    }
    await _saveUnbornToServer();
    _renderUnbornTable();
    _renderFormerTable();
  } catch(e) {
    btn.disabled = false;
    btn.textContent = 'Analyze';
    alert('Analysis failed for ' + ticker + ': ' + e.message);
  }
}
// ─────────────────────────────────────────────────────────────────────────────

async function deleteUnbornRow(key) {
  _deletedUnbornKeys.add(key);
  delete _unbornRows[key];
  await _saveUnbornToServer();
  await fetch('/api/unborn-cache-delete', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({row_key: key})
  });
  _renderUnbornTable();
  _renderFormerTable();
}

async function recalcUnbornRow(key, btn) {
  // key = "TICKER|STRAT"
  const parts = key.split('|');
  const ticker = parts[0];
  const strat  = parts[1] || 'CC';
  const row    = _unbornRows[key] || {};
  const qty    = row._qty || 1;
  const oldRow = Object.assign({}, row);  // keep a copy in case we need to restore

  // Disable buttons and blank stale market data while running; row stays visible
  const tr = btn.closest('tr');
  const btns = tr ? tr.querySelectorAll('button') : [btn];
  btns.forEach(b => { b.disabled = true; });
  btn.innerHTML = '<span style="display:inline-block;width:11px;height:11px;border:2px solid currentColor;border-top-color:transparent;border-radius:50%;animation:spin 0.7s linear infinite;vertical-align:middle"></span>';
  // Columns: 0=Symbol,1=Type,2=Strike,3=Expiry,4=Side,5=Qty,6=DTE,7=Delta,8=UL,9=OptPrice,10=Basis,11=Ideal,12=Actions
  if (tr) [2, 3, 6, 7, 9].forEach(i => { if (tr.cells[i]) tr.cells[i].textContent = '—'; });

  try {
    let d;
    let forceNow = true;
    while (true) {
      const r = await fetch('/api/unborn', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ticker, qty, strat, force: forceNow})
      });
      forceNow = false;  // see analyzeFormerPosition's comment on the PPIH infinite-loop bug
      let raw;
      try { raw = await r.text(); d = JSON.parse(raw); }
      catch { throw new Error('Non-JSON: ' + (raw||'').slice(0,200)); }
      if (r.status === 202 && d.status === 'in_progress') {
        await new Promise(res => setTimeout(res, (d.retry_after || 5) * 1000));
        continue;
      }
      break;
    }
    if (d.error) throw new Error(d.error);
    const chain = d.chain || [];
    if (chain.length) {
      const ubKey = encodeURIComponent(ticker + '|' + strat + '|' + qty);
      const _runAt = new Date().toLocaleString('en-US', {timeZone:'America/New_York',month:'numeric',day:'numeric',hour:'numeric',minute:'2-digit',hour12:true}).replace(',','') + ' ET';
      _unbornRows[key] = Object.assign({}, chain[0], {_qty: qty, _ubKey: ubKey, _ul_cost_basis: d.ul_cost_basis ?? null, _run_at: _runAt});
    } else {
      // Analysis ran but returned no chain — restore old row
      _unbornRows[key] = oldRow;
    }
    await _saveUnbornToServer();
    _renderUnbornTable();
  } catch(e) {
    // Restore old row on error
    _unbornRows[key] = oldRow;
    _renderUnbornTable();
    alert('Recalc failed for ' + ticker + ': ' + e.message);
  }
}

function recBadge(rec, key, chainCash, text, runAt) {
  const cls = rec === 'ROLL' ? 'warn' : rec === 'ASSIGNMENT' ? 'danger'
    : rec === 'HOLD' ? 'hold' : rec === 'LET EXPIRE' ? 'muted' : 'ok';
  const label = rec;
  const tipAttr = (rec === 'LET EXPIRE' && text) ? ` title="${esc(text)}"` : '';
  const cashLine = (rec === 'ROLL' && chainCash != null)
    ? `<div style="font-size:10px;margin-top:3px;color:var(--${chainCash >= 0 ? 'ok' : 'danger'})">`
      + `Net chain: ${chainCash >= 0 ? '+' : ''}$${chainCash.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2})}</div>`
    : '';
  const safeKey = key.replace(/'/g,"\\'");
  const dateSpan = runAt ? `<span style="font-size:10px;color:var(--muted);white-space:nowrap">${esc(runAt)}</span>` : '';
  return `<div>
    <div style="display:inline-flex;align-items:center;gap:4px">
      <a href="/analyze/${encodeURIComponent(key)}" target="_blank"
        class="badge badge-${cls}" style="text-decoration:none;cursor:pointer"${tipAttr}>${label}</a>
      <button onclick="resetAnalysis('${safeKey}', this)" title="Reset to Analyze"
        style="font-size:10px;padding:0 4px;line-height:14px;background:none;border:1px solid var(--muted);border-radius:3px;color:var(--muted);cursor:pointer">x</button>
      ${dateSpan}
    </div>
    ${cashLine}
  </div>`;
}

async function resetAnalysis(key, btn) {
  await fetch('/api/reset-cache-entry', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({position_key: key})
  });
  delete _recommendations[key];
  _saveRecs(_recommendations);
  const cell = _actionCell(key);
  if (cell) cell.innerHTML = `<button onclick="analyzePosition('${key.replace(/'/g,"\\'")}', this)" style="font-size:11px;padding:4px 10px">Analyze</button>`;
}

function _actionCell(key) {
  return document.querySelector(`[data-poskey]`) &&
    [...document.querySelectorAll('[data-poskey]')].find(el => el.dataset.poskey === key);
}

async function analyzePosition(key, btn, force) {
  if (btn) {
    btn.disabled = true;
    btn.style.background = 'orange';
    btn.style.color = '#000';
    btn.style.borderColor = 'orange';
    btn.style.display = 'inline-flex';
    btn.style.alignItems = 'center';
    btn.style.gap = '5px';
    btn.innerHTML = '<span style="width:11px;height:11px;border:2px solid rgba(0,0,0,0.25);border-top-color:#000;border-radius:50%;animation:spin 0.7s linear infinite;display:inline-block;flex-shrink:0"></span>Analyzing…';
  } else {
    const _c = _actionCell(key);
    if (_c) _c.innerHTML = '<span style="color:var(--muted);font-size:11px;display:inline-flex;align-items:center;gap:5px"><span style="width:10px;height:10px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin 0.7s linear infinite;display:inline-block;flex-shrink:0"></span>Re-analyzing…</span>';
  }
  console.log('[analyze] starting:', key);
  try {
    let d;
    let forceNow = !!force;
    // Poll until the server finishes (handles concurrent in-progress queries)
    while (true) {
      const r = await fetch('/api/analyze', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({position_key: key, force: forceNow})
      });
      forceNow = false;  // see analyzeFormerPosition's comment on the PPIH infinite-loop bug
      let raw;
      try {
        raw = await r.text();
        d = JSON.parse(raw);
      } catch(parseErr) {
        throw new Error('Server returned non-JSON: ' + raw.slice(0, 200));
      }
      if (r.status === 202 && d.status === 'in_progress') {
        await new Promise(res => setTimeout(res, (d.retry_after || 5) * 1000));
        continue;
      }
      break;
    }
    console.log('[analyze] result:', d);
    if (d.error) throw new Error(d.error);
    const displayRec = (d.auto && d.recommendation === 'HOLD') ? 'LET EXPIRE' : d.recommendation;
    const runAt = new Date().toLocaleString('en-US', {timeZone:'America/New_York',month:'numeric',day:'numeric',hour:'numeric',minute:'2-digit',hour12:true}).replace(',','') + ' ET';
    _recommendations[key] = {rec: displayRec, chainCash: d.chain_cash ?? null, text: d.text ?? null, runAt};
    _saveRecs(_recommendations);
    const cell = _actionCell(key);
    if (cell) cell.innerHTML = recBadge(displayRec, key, d.chain_cash ?? null, d.text ?? null, runAt);
  } catch(e) {
    console.error('[analyze] error for', key, ':', e.message);
    const cell = _actionCell(key);
    if (cell) {
      cell.innerHTML = `<span class="err-tip" style="color:var(--danger);font-size:11px">&#9888; Error<span class="err-msg">${esc(e.message)}</span></span>
        <button onclick="retryAnalysis('${key.replace(/'/g,"\\'")}', this)" style="font-size:10px;padding:2px 6px;margin-left:4px">Retry</button>`;
    } else if (btn) {
      btn.disabled = false;
      btn.textContent = 'Retry';
      btn.title = e.message;
      btn.style.background = '';
      btn.style.borderColor = '';
      btn.style.color = 'var(--danger)';
    }
  }
}

async function autoFillCcQty() {
  const ticker = document.getElementById('ub-ticker').value.trim().toUpperCase();
  const strat  = document.getElementById('ub-strat').value;
  if (strat !== 'CC' || !ticker) return;
  try {
    const r = await fetch('/api/cc-qty/' + encodeURIComponent(ticker));
    const d = await r.json();
    if (d.qty != null && d.qty > 0) document.getElementById('ub-qty').value = d.qty;
  } catch(e) { /* silently ignore */ }
}

async function findUnborn() {
  const ticker    = document.getElementById('ub-ticker').value.trim().toUpperCase();
  const qty       = parseInt(document.getElementById('ub-qty').value) || 1;
  const strat     = document.getElementById('ub-strat').value;
  const resultEl = document.getElementById('unborn-result');
  if (!ticker) { resultEl.innerHTML = '<span style="color:var(--danger);font-size:12px">Enter a ticker.</span>'; return; }

  resultEl.innerHTML = '<span style="display:inline-flex;align-items:center;gap:6px;color:var(--muted);font-size:12px"><span style="width:13px;height:13px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin 0.7s linear infinite;display:inline-block;flex-shrink:0"></span>Analyzing…</span>';
  try {
    // Always run fresh
    let d;
    let forceNow = true;
    while (true) {
      const r = await fetch('/api/unborn', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ticker, qty, strat, force: forceNow})
      });
      forceNow = false;  // see analyzeFormerPosition's comment on the PPIH infinite-loop bug
      let raw;
      try { raw = await r.text(); d = JSON.parse(raw); }
      catch { throw new Error('Server returned non-JSON: ' + (raw||'').slice(0,200)); }
      if (r.status === 202 && d.status === 'in_progress') {
        await new Promise(res => setTimeout(res, (d.retry_after || 5) * 1000));
        continue;
      }
      break;
    }
    if (d.error) throw new Error(d.error);

    // Recommendation badge
    const cls   = d.recommendation === 'ROLL' ? 'warn' : d.recommendation === 'ASSIGNMENT' ? 'danger' : 'ok';
    const ubKey = encodeURIComponent(ticker + '|' + strat + '|' + qty);
    resultEl.innerHTML = `<a href="/unborn/${ubKey}" target="_blank"
      class="badge badge-${cls}" style="text-decoration:none;cursor:pointer;font-size:12px;padding:4px 10px">
      Details</a>`;

    // Accumulate proposed trades — always save the result, including DO NOTHING rows
    const chain = d.chain || [];
    const _runAt2 = new Date().toLocaleString('en-US', {timeZone:'America/New_York',month:'numeric',day:'numeric',hour:'numeric',minute:'2-digit',hour12:true}).replace(',','') + ' ET';
    const rowBase = chain.length ? chain[0] : {
      symbol: ticker, option_type: strat,
      _do_nothing: !!d._do_nothing, ideal_entry: d._do_nothing ? 'DO NOTHING' : null,
    };
    const row = Object.assign({}, rowBase, {_qty: qty, _ubKey: ubKey, _ul_cost_basis: d.ul_cost_basis ?? null, _run_at: _runAt2});
    _unbornRows[ticker + '|' + strat] = row;
    await _saveUnbornToServer();
    _renderUnbornTable();
    document.getElementById('unborn-chain').scrollIntoView({behavior:'smooth', block:'nearest'});
    // Clear inputs after successful display
    document.getElementById('ub-ticker').value = '';
    document.getElementById('ub-qty').value = '1';
    document.getElementById('ub-strat').value = 'CC';
    resultEl.innerHTML = '';
  } catch(e) {
    resultEl.innerHTML = `<span class="err-tip" style="color:var(--danger);font-size:12px">&#9888; Error<span class="err-msg">${esc(e.message)}</span></span>`;
  }
}

async function placeTrade(rowKey, btn) {
  const c = _unbornRows[rowKey];
  if (!c) { alert('Trade data not found for: ' + rowKey); return; }
  btn.disabled = true;
  btn.textContent = '⧗';
  const trade = {
    symbol: c.symbol, option_type: c.option_type, strike: c.strike,
    expiry: c.expiry, qty: c._qty || 1, opt_price: c.opt_price
  };
  try {
    const r = await fetch('/api/trade', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(trade)
    });
    const d = await r.json();
    if (d.status === 'launched') {
      btn.textContent = '✓';
      btn.style.background = 'var(--ok)';
      btn.disabled = false;
      btn.title = 'Click to retry';
      btn.onclick = () => _resetTradeBtn(btn, rowKey);
      _showTradeModal(d.trade);
    } else {
      throw new Error(d.error || 'Unknown error');
    }
  } catch(e) {
    btn.textContent = 'Err';
    btn.style.background = 'var(--danger)';
    btn.title = e.message + ' — click to retry';
    btn.disabled = false;
    btn.onclick = () => _resetTradeBtn(btn, rowKey);
  }
}

function _resetTradeBtn(btn, rowKey) {
  btn.textContent = 'Trade';
  btn.style.background = 'var(--accent)';
  btn.title = '';
  btn.onclick = () => placeTrade(rowKey, btn);
}

function _cpBtn(val) {
  return `<button onclick="navigator.clipboard.writeText('${val}');this.textContent='✓';setTimeout(()=>this.textContent='copy',1500)"
    style="margin-left:6px;font-size:10px;padding:1px 6px;border-radius:3px;cursor:pointer;border:1px solid var(--muted);background:transparent;color:var(--fg)">copy</button>`;
}
function _showTradeModal(t) {
  const existing = document.getElementById('trade-modal');
  if (existing) existing.remove();
  const cp     = (t.option_type||'').toUpperCase() === 'CALL' ? 'Call' : 'Put';
  const strike = t.strike ? parseFloat(t.strike).toFixed(2) : '—';
  const price  = t.opt_price ? parseFloat(t.opt_price).toFixed(2) : '—';
  const expiry = t.expiry || '—';
  const qty    = t.qty || 1;

  // Make draggable
  const div = document.createElement('div');
  div.id = 'trade-modal';
  div.style.cssText = 'position:fixed;top:70px;right:24px;z-index:9999;background:var(--surface);border:2px solid var(--accent);border-radius:10px;padding:0;min-width:310px;box-shadow:0 6px 32px #0008;font-size:13px;user-select:none';
  div.innerHTML = `
    <div id="trade-modal-hdr" style="display:flex;justify-content:space-between;align-items:center;padding:12px 16px 10px;background:var(--accent);border-radius:8px 8px 0 0;cursor:grab">
      <b style="color:#fff;font-size:13px">&#128203; Fidelity Trade Ticket</b>
      <span onclick="document.getElementById('trade-modal').remove()" onmousedown="event.stopPropagation()" style="cursor:pointer;color:#fff;font-size:20px;line-height:1;padding:0 2px">&times;</span>
    </div>
    <div style="padding:14px 16px">
      <table style="border-collapse:collapse;width:100%">
        <tr><td style="color:var(--muted);padding:5px 14px 5px 0;white-space:nowrap">1. Action</td>
            <td><b style="color:var(--ok)">Sell to Open</b>${t._run_at ? `<div style="font-size:10px;color:var(--muted);margin-top:2px">${esc(t._run_at)}</div>` : ''}</td></tr>
        <tr><td style="color:var(--muted);padding:5px 14px 5px 0">2. Symbol</td>
            <td><b>${esc(t.symbol)}</b>${_cpBtn(t.symbol)}</td></tr>
        <tr><td style="color:var(--muted);padding:5px 14px 5px 0">3. Type</td>
            <td>${cp}${_cpBtn(cp)}</td></tr>
        <tr><td style="color:var(--muted);padding:5px 14px 5px 0">4. Expiry</td>
            <td>${esc(expiry)}${_cpBtn(expiry)}</td></tr>
        <tr><td style="color:var(--muted);padding:5px 14px 5px 0">5. Strike</td>
            <td>$${strike}${_cpBtn(strike)}</td></tr>
        <tr><td style="color:var(--muted);padding:5px 14px 5px 0">6. Qty</td>
            <td>${qty}${_cpBtn(String(qty))}</td></tr>
        <tr><td style="color:var(--muted);padding:5px 14px 5px 0">7. Order type</td>
            <td>Limit / Day</td></tr>
        <tr style="background:rgba(var(--accent-rgb,99,102,241),0.08);border-radius:4px">
            <td style="color:var(--muted);padding:7px 14px 7px 0"><b>8. Limit $</b></td>
            <td><b style="color:var(--accent);font-size:15px">$${price}</b>${_cpBtn(price)}</td></tr>
      </table>
      <div style="margin-top:10px;font-size:11px;color:var(--muted);border-top:1px solid var(--border);padding-top:8px">
        Fill each field in order → Preview → Place Order
      </div>
    </div>`;

  // Drag support
  const hdr = div.querySelector('#trade-modal-hdr');
  let ox=0,oy=0,mx=0,my=0;
  hdr.addEventListener('mousedown', e => {
    ox = div.offsetLeft; oy = div.offsetTop;
    mx = e.clientX;      my = e.clientY;
    div.style.right = 'auto';
    function onMove(e2) {
      div.style.left = (ox + e2.clientX - mx) + 'px';
      div.style.top  = (oy + e2.clientY - my) + 'px';
    }
    function onUp() { document.removeEventListener('mousemove', onMove); document.removeEventListener('mouseup', onUp); }
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });

  document.body.appendChild(div);
}

async function resetRecommendations() {
  Object.keys(_recommendations).forEach(k => delete _recommendations[k]);
  _saveRecs({});
  await fetch('/api/reset-cache', {method:'POST'});
  renderTable();
}

function sortTable(col) {
  const ths = document.querySelectorAll('th');
  ths.forEach(th => th.classList.remove('sorted-asc','sorted-desc'));
  if (_sortCol === col) { _sortDir *= -1; }
  else { _sortCol = col; _sortDir = 1; }
  ths[col].classList.add(_sortDir === 1 ? 'sorted-asc' : 'sorted-desc');
  localStorage.setItem(_LS_SORT_KEY, JSON.stringify([_sortCol, _sortDir]));
  renderTable();
}

// ── Live underlying price poller (every 5 min, independent of main refresh) ──
const _LS_PR_LAST   = 'optionsPriceLastRun';  // epoch ms of last price poll
const _LS_PR_PRICES = 'optionsPrevUlPrices';  // {ticker: price} — persisted across refresh
const _PRICE_INTERVAL_SECS = 300;           // 5 minutes
let _priceHeartbeat = null;

async function fetchPrices() {
  try {
    const r = await fetch('/api/prices');
    if (!r.ok) return;
    const prices = await r.json();
    const _seenSyms = new Set();
    for (const p of _data) {
      const sym = (p.symbol || '').toUpperCase();
      const info = prices[sym];
      if (!info) continue;
      const newPrice = info.price;
      if (newPrice != null) {
        if (!_seenSyms.has(sym)) {
          const prev = _prevUlPrices[sym];
          _ulPriceTick[sym] = (prev == null) ? null : (newPrice > prev ? 'up' : newPrice < prev ? 'down' : null);
          _prevUlPrices[sym] = newPrice;
          _seenSyms.add(sym);
        }
        p.underlying = newPrice;
      }
      if (info.atr  != null) p.atr    = info.atr;
      if (info.buffer != null) p.buffer = info.buffer;
    }
    // ── VIX ──
    const vixInfo = prices['__vix__'];
    if (vixInfo && vixInfo.price != null) {
      const newVix = vixInfo.price;
      const vEl = document.getElementById('h-vix');
      if (vEl) {
        const tick = _prevVix != null ? (newVix > _prevVix ? 'up' : newVix < _prevVix ? 'down' : null) : null;
        const arrow = tick === 'up' ? ' ▲' : tick === 'down' ? ' ▼' : '';
        const lvlCls = newVix < 20 ? 'ok' : newVix < 30 ? 'warn' : 'danger';
        const tickStyle = tick === 'up' ? ';color:var(--danger)' : tick === 'down' ? ';color:var(--ok)' : '';
        vEl.innerHTML = `<span style="color:var(--${lvlCls})${tickStyle}">${newVix.toFixed(2)}${arrow}</span>`;
        vEl.className = 'stat-value';
      }
      _prevVix = newVix;
    }

    _pricePollCount++;
    localStorage.setItem(_LS_PR_LAST,   String(Date.now()));
    localStorage.setItem(_LS_PR_PRICES, JSON.stringify(_prevUlPrices));
    // ── Update unborn ul_price + tick direction ────────────────────────────
    for (const k of Object.keys(_unbornRows)) {
      const sym = (_unbornRows[k].symbol || '').toUpperCase();
      const info = prices[sym];
      if (!info || info.price == null) continue;
      const newPrice = info.price;
      // Only compute tick if this symbol isn't already in _data (avoid double-setting)
      if (!_data.some(p => (p.symbol||'').toUpperCase() === sym)) {
        const prev = _prevUlPrices[sym];
        _ulPriceTick[sym] = (prev == null) ? null : (newPrice > prev ? 'up' : newPrice < prev ? 'down' : null);
        _prevUlPrices[sym] = newPrice;
      }
      _unbornRows[k].ul_price = newPrice;
    }
    // ── Update former position ul_price + tick ────────────────────────────
    let fmrUpdated = false;
    for (const p of _formerPositions) {
      const sym = (p.symbol || '').toUpperCase();
      const info = prices[sym];
      if (!info || info.price == null) continue;
      const newPrice = info.price;
      if (!_seenSyms.has(sym)) {
        const prev = _prevUlPrices[sym];
        _ulPriceTick[sym] = (prev == null) ? null : (newPrice > prev ? 'up' : newPrice < prev ? 'down' : null);
        _prevUlPrices[sym] = newPrice;
        _seenSyms.add(sym);
      }
      p.ul_price = newPrice;
      fmrUpdated = true;
    }
    renderTable();
    _renderUnbornTable();
    if (fmrUpdated) _renderFormerTable();
    _checkAlerts();
  } catch(e) { /* silent — don't disrupt the UI */ }
}

function _priceTick() {
  if (!_isMarketHours()) return;
  const last = parseInt(localStorage.getItem(_LS_PR_LAST) || '0', 10);
  if ((Date.now() - last) / 1000 >= _PRICE_INTERVAL_SECS) fetchPrices();
}

// Share the existing 30-second heartbeat — just add _priceTick to the interval
function _initPriceRefresh() {
  // Fire once on load (after a short delay so _data is populated) — only during market hours
  setTimeout(() => { if (_isMarketHours()) fetchPrices(); }, 3000);
  _priceHeartbeat = setInterval(_priceTick, 30_000);
}
// ─────────────────────────────────────────────────────────────────────────────

// ── Analysis-updates poller ───────────────────────────────────────────────────
// Polls /api/analysis-updates to pick up scheduled-refresh results without a
// full page reload. Runs every 60s normally; speeds up to 12s when inflight
// analyses are in progress or new results just appeared.
const _AU_SLOW_MS = 60_000;
const _AU_FAST_MS = 12_000;
let   _auPollTimer = null;

async function _pollAnalysisUpdates() {
  let nextDelay = _AU_SLOW_MS;
  try {
    // Open-position analysis cache
    const r = await fetch('/api/analysis-updates');
    if (r.ok) {
      const data = await r.json();
      let anyNew = false;
      for (const [posKey, val] of Object.entries(data.updates || {})) {
        const existing = _recommendations[posKey];
        if (!existing || existing.runAt !== val.run_at) {
          _recommendations[posKey] = {
            rec: val.rec,
            chainCash: val.chain_cash ?? null,
            text: val.text ?? null,
            runAt: val.run_at,
          };
          const cell = _actionCell(posKey);
          if (cell) cell.innerHTML = recBadge(val.rec, posKey, val.chain_cash ?? null, val.text ?? null, val.run_at);
          anyNew = true;
        }
      }
      if (anyNew || (data.inflight || []).length > 0 || (data.unborn_inflight || []).length > 0) nextDelay = _AU_FAST_MS;
    }
    // Unborn rows — merge in any rows whose _run_at changed, AND pick up rows
    // the server persisted directly (e.g. a background analysis that finished
    // after this tab stopped polling it) that this tab never had locally.
    const ru = await fetch('/api/unborn-rows');
    if (ru.ok) {
      const serverRows = await ru.json();
      let unbornUpdated = false;
      for (const [key, row] of Object.entries(serverRows)) {
        if (_deletedUnbornKeys.has(key)) continue;
        const existing = _unbornRows[key];
        if (!existing || (row._run_at && existing._run_at !== row._run_at)) {
          _unbornRows[key] = row;
          unbornUpdated = true;
        }
      }
      if (unbornUpdated) _renderUnbornTable();
    }
  } catch (_) {}
  _auPollTimer = setTimeout(_pollAnalysisUpdates, nextDelay);
}

function _initAnalysisUpdatePoller() {
  _auPollTimer = setTimeout(_pollAnalysisUpdates, _AU_SLOW_MS);
}
// ─────────────────────────────────────────────────────────────────────────────

// ── Resizable columns (pos-table only) ───────────────────────────────────────
const _COL_WIDTHS_KEY = 'pos-col-widths';

function _saveColWidths() {
  const ths = document.querySelectorAll('#pos-table thead th');
  const widths = {};
  ths.forEach(t => { if (t.dataset.col) widths[t.dataset.col] = t.offsetWidth; });
  localStorage.setItem(_COL_WIDTHS_KEY, JSON.stringify(widths));
}

function initResizableCols() {
  const table = document.getElementById('pos-table');
  if (!table) return;
  const ths = table.querySelectorAll('thead th');
  const saved = JSON.parse(localStorage.getItem(_COL_WIDTHS_KEY) || 'null');
  if (saved) {
    ths.forEach(th => {
      const w = saved[th.dataset.col];
      if (w) { th.style.minWidth = w + 'px'; th.style.width = w + 'px'; }
    });
  }
  ths.forEach((th, i) => {
    if (th.querySelector('.col-rh')) return; // already wired
    const handle = document.createElement('div');
    handle.className = 'col-rh';
    handle.addEventListener('mousedown', e => {
      e.stopPropagation();
      e.preventDefault();
      handle.classList.add('dragging');
      const startX = e.pageX;
      const startW = th.offsetWidth;
      const onMove = e => {
        const w = Math.max(40, startW + e.pageX - startX);
        th.style.minWidth = w + 'px';
        th.style.width = w + 'px';
      };
      const onUp = () => {
        handle.classList.remove('dragging');
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        _saveColWidths();
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
    th.appendChild(handle);
  });
}
// ─────────────────────────────────────────────────────────────────────────────

// Sort by expiry by default (col 5, ascending)
document.querySelectorAll('th')[3].classList.add('sorted-asc');
_loadUnbornFromServer();
_loadFormerPositions();
fetchData().then(() => initResizableCols());
_initAutoRefresh();
_initPriceRefresh();
_initAnalysisUpdatePoller();

</script>
</body>
</html>"""


# ── Fidelity browser automation ──────────────────────────────────────────────
_FIDELITY_SESSION_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".fidelity_session"
)



async def _fidelity_trade_async(trade: dict) -> None:  # kept for compat
    """
    Open Fidelity's options trade ticket in the user's existing Chrome session
    (via CDP) or fall back to a persistent Playwright context.
    Pre-fills all fields; user reviews and submits manually.
    """
    from playwright.async_api import async_playwright

    symbol      = str(trade.get("symbol", "")).upper()
    option_type = str(trade.get("option_type", "CALL")).upper()
    strike      = trade.get("strike")
    expiry      = trade.get("expiry", "")
    qty         = int(trade.get("qty", 1))
    opt_price   = trade.get("opt_price")

    try:
        exp_dt      = datetime.datetime.strptime(expiry, "%Y-%m-%d")
        exp_display = exp_dt.strftime("%b %d, %Y")
    except (ValueError, TypeError):
        exp_display = expiry

    log.info("[fidelity] Trade request: %s %s %s %s x%d", symbol, option_type, strike, expiry, qty)

    _FIDELITY_TRADE_URL = "https://digital.fidelity.com/ftgw/digital/easy/investmentoptions"

    async with async_playwright() as pw:
        use_cdp = _cdp_available()
        if use_cdp:
            log.info("[fidelity] CDP available — opening tab in existing Chrome session")
            # Open the Fidelity page via AppleScript so it uses the existing Chrome session
            import subprocess as _sp
            _sp.Popen([
                "osascript", "-e",
                f'tell application "Google Chrome" to open location "{_FIDELITY_TRADE_URL}"'
            ])
            await asyncio.sleep(4)   # let the tab open and load

            # Connect via CDP and find the new Fidelity tab
            browser = await pw.chromium.connect_over_cdp("http://localhost:9222")
            page = None
            for _ in range(15):
                for ctx in browser.contexts:
                    for p in ctx.pages:
                        if "fidelity.com" in p.url and "login" not in p.url:
                            page = p
                            break
                    if page:
                        break
                if page:
                    break
                await asyncio.sleep(1)
            if page is None:
                # fallback: grab most recently opened page
                all_pages = [p for ctx in browser.contexts for p in ctx.pages]
                page = all_pages[-1] if all_pages else None
            if page is None:
                raise RuntimeError("Could not find Fidelity tab in Chrome")
            log.info("[fidelity] Found Fidelity tab: %s", page.url)
        else:
            log.warning("[fidelity] Chrome not running with --remote-debugging-port=9222. "
                        "Restart Chrome with: open -a 'Google Chrome' --args --remote-debugging-port=9222")
            os.makedirs(_FIDELITY_SESSION_DIR, exist_ok=True)
            ctx  = await pw.chromium.launch_persistent_context(
                _FIDELITY_SESSION_DIR, headless=False, channel="chrome",
                viewport={"width": 1280, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto("https://digital.fidelity.com/ftgw/digital/portfolio/summary",
                            wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(2000)
            if any(x in page.url for x in ["login", "saa.fidelity", "nb.fidelity", "www.fidelity.com/"]):
                log.info("[fidelity] Not logged in — waiting up to 3 min for user…")
                await page.goto("https://digital.fidelity.com/ftgw/digital/login/full-page",
                                wait_until="domcontentloaded")
                try:
                    await page.wait_for_url(
                        re.compile(r"digital\.fidelity\.com/ftgw/digital/(?!login)"),
                        timeout=180_000)
                    await page.wait_for_timeout(2000)
                except Exception:
                    log.warning("[fidelity] Login wait timed out; continuing anyway…")
            await page.goto(_FIDELITY_TRADE_URL, wait_until="domcontentloaded", timeout=30_000)

        await page.wait_for_timeout(3000)

        # ── 3. Fill Symbol ───────────────────────────────────────────────────
        sym_selectors = [
            'input[id*="symbol" i]',
            'input[name*="symbol" i]',
            'input[placeholder*="symbol" i]',
            'input[aria-label*="symbol" i]',
            '#eq-ticket-dest-symbol',
        ]
        for sel in sym_selectors:
            try:
                await page.fill(sel, symbol, timeout=3000)
                await page.press(sel, "Tab")
                await page.wait_for_timeout(1500)
                log.info("[fidelity] Filled symbol with selector: %s", sel)
                break
            except Exception:
                continue

        # ── 4. Select Options tab if present ─────────────────────────────────
        for label in ["Options", "Option"]:
            try:
                await page.click(f'[role="tab"]:has-text("{label}")', timeout=3000)
                await page.wait_for_timeout(1000)
                break
            except Exception:
                pass

        # ── 5. Action = Sell to Open ─────────────────────────────────────────
        action_texts = ["Sell to Open", "Sell To Open", "SELL TO OPEN"]
        for txt in action_texts:
            try:
                # Try select element first
                await page.select_option('select[id*="action" i], select[name*="action" i]', label=txt, timeout=2000)
                log.info("[fidelity] Set action via select")
                break
            except Exception:
                pass
            try:
                await page.click(f'option:has-text("{txt}")', timeout=1000)
                break
            except Exception:
                pass
        await page.wait_for_timeout(500)

        # ── 6. Expiration date ───────────────────────────────────────────────
        exp_selectors = [
            'select[id*="expir" i]', 'select[name*="expir" i]',
            'select[aria-label*="expir" i]',
        ]
        for sel in exp_selectors:
            try:
                await page.select_option(sel, label=re.compile(exp_display[:6], re.IGNORECASE), timeout=3000)
                log.info("[fidelity] Set expiry: %s", exp_display)
                await page.wait_for_timeout(1000)
                break
            except Exception:
                continue

        # ── 7. Call / Put ────────────────────────────────────────────────────
        cp_label = "Call" if option_type == "CALL" else "Put"
        cp_selectors = [
            f'input[type="radio"][value*="{cp_label}" i]',
            f'label:has-text("{cp_label}")',
            f'[role="radio"]:has-text("{cp_label}")',
            f'select[id*="type" i], select[name*="type" i]',
        ]
        for sel in cp_selectors:
            try:
                if "select" in sel:
                    await page.select_option(sel, label=cp_label, timeout=2000)
                else:
                    await page.click(sel, timeout=2000)
                log.info("[fidelity] Set call/put: %s", cp_label)
                await page.wait_for_timeout(800)
                break
            except Exception:
                continue

        # ── 8. Strike ────────────────────────────────────────────────────────
        strike_str = f"{float(strike):.2f}" if strike is not None else ""
        strike_selectors = [
            'select[id*="strike" i]', 'select[name*="strike" i]',
            'select[aria-label*="strike" i]',
        ]
        for sel in strike_selectors:
            try:
                await page.select_option(sel, label=re.compile(r'\b' + re.escape(strike_str.lstrip('0') or strike_str)), timeout=3000)
                log.info("[fidelity] Set strike: %s", strike_str)
                await page.wait_for_timeout(800)
                break
            except Exception:
                continue

        # ── 9. Quantity ───────────────────────────────────────────────────────
        qty_selectors = [
            'input[id*="quant" i]', 'input[name*="quant" i]',
            'input[aria-label*="quant" i]', 'input[id*="qty" i]',
            'input[placeholder*="quant" i]',
        ]
        for sel in qty_selectors:
            try:
                await page.fill(sel, str(qty), timeout=2000)
                log.info("[fidelity] Set qty: %d", qty)
                await page.wait_for_timeout(500)
                break
            except Exception:
                continue

        # ── 10. Order type = Limit ────────────────────────────────────────────
        for sel in ['select[id*="order" i]', 'select[name*="order" i]', 'select[aria-label*="order type" i]']:
            try:
                await page.select_option(sel, label=re.compile("limit", re.IGNORECASE), timeout=2000)
                log.info("[fidelity] Set order type: Limit")
                await page.wait_for_timeout(500)
                break
            except Exception:
                continue

        # ── 11. Limit price ───────────────────────────────────────────────────
        if opt_price is not None:
            price_str = f"{float(opt_price):.2f}"
            price_selectors = [
                'input[id*="limit" i]', 'input[name*="limit" i]',
                'input[aria-label*="limit price" i]', 'input[id*="price" i]',
            ]
            for sel in price_selectors:
                try:
                    await page.fill(sel, price_str, timeout=2000)
                    log.info("[fidelity] Set limit price: %s", price_str)
                    await page.wait_for_timeout(500)
                    break
                except Exception:
                    continue

        # ── 12. Time in force = Day ───────────────────────────────────────────
        for sel in ['select[id*="duration" i]', 'select[name*="duration" i]',
                    'select[aria-label*="time in force" i]', 'select[id*="tif" i]']:
            try:
                await page.select_option(sel, label=re.compile(r'^day$', re.IGNORECASE), timeout=2000)
                log.info("[fidelity] Set time in force: Day")
                await page.wait_for_timeout(500)
                break
            except Exception:
                continue

        log.info("[fidelity] Form fill complete — browser left open for user review")
        # Keep the page open; if CDP, leave Chrome running; if fallback, wait up to 10 min
        if not use_cdp:
            try:
                await page.wait_for_timeout(600_000)
            except Exception:
                pass
            try:
                await ctx.close()
            except Exception:
                pass


def _angular_set(field_id: str, value: str) -> str:
    """JS: set an Angular-bound <input> value and trigger change detection."""
    v = value.replace("\\", "\\\\").replace("'", "\\'")
    return (
        f"(function(){{"
        f"var el=document.getElementById('{field_id}');"
        f"if(!el)return 'not found:{field_id}';"
        f"var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
        f"s.call(el,'{v}');"
        f"el.dispatchEvent(new Event('input',{{bubbles:true}}));"
        f"el.dispatchEvent(new Event('change',{{bubbles:true}}));"
        f"return 'ok:{field_id}={v}';"
        f"}})()"
    )


def _click_id(el_id: str) -> str:
    """JS: click an element by id."""
    return (
        f"(function(){{"
        f"var el=document.getElementById('{el_id}');"
        f"if(!el)return 'not found:{el_id}';"
        f"el.click();"
        f"return 'clicked:{el_id}';"
        f"}})()"
    )


def _click_dropdown_option(text: str) -> str:
    """JS: click the first visible button/li/option whose text matches (case-insensitive)."""
    t = text.replace("\\", "\\\\").replace("'", "\\'")
    return (
        f"(function(){{"
        f"var needle='{t}'.toLowerCase();"
        f"var els=[...document.querySelectorAll('button,li,[role=option],[role=menuitem]')];"
        f"var el=els.find(e=>e.offsetParent!==null && e.textContent.trim().toLowerCase().includes(needle));"
        f"if(!el)return 'not found:'+needle;"
        f"el.click();"
        f"return 'clicked option:'+el.textContent.trim().substring(0,60);"
        f"}})()"
    )


def _click_nth_account(n: int) -> str:
    """JS: open account dropdown then click the nth account item (1-based)."""
    return (
        f"(function(){{"
        f"var btn=document.getElementById('account');"
        f"if(!btn)return 'no #account btn';"
        f"btn.click();"
        f"return 'account-dropdown-opened';"
        f"}})()"
    )


def _select_nth_account_item(n: int) -> str:
    """JS: after account dropdown is open, click the item whose text contains '8097' (fallback: nth item)."""
    idx = n - 1
    return (
        f"(function(){{"
        f"var container=document.querySelector('ott-account-dropdown');"
        f"if(!container)return 'no ott-account-dropdown';"
        f"var items=[...container.querySelectorAll('li,button,[role=option],[role=menuitem],[class*=item]')]"
        f".filter(e=>e.offsetParent!==null);"
        f"var target=items.find(e=>/8097/i.test(e.textContent));"
        f"if(!target){{"
        f"  if(items.length<={idx})return 'only '+items.length+' items, no 8097 match — available: '+items.map(e=>e.textContent.trim().substring(0,30)).join(' | ');"
        f"  target=items[{idx}];"
        f"}}"
        f"target.click();"
        f"return 'selected: '+target.textContent.trim().substring(0,60);"
        f"}})()"
    )


_FIDELITY_URL_FRAGMENT = "fidelity.com/ftgw/digital/trade-options"

def _osascript_js(js: str) -> str:
    """Wrap JS for Chrome's current active tab (simple, no tab iteration)."""
    safe = js.replace("\\", "\\\\").replace('"', '\\"')
    return f'tell application "Google Chrome" to execute front window\'s active tab javascript "{safe}"'


def _focus_fidelity_tab() -> str:
    """Find the trade-options tab by URL, bring it to front, activate Chrome.
    Uses multi-line AppleScript passed via stdin to avoid -e quoting issues."""
    import subprocess as _sp
    script = (
        'tell application "Google Chrome"\n'
        '  repeat with w in windows\n'
        '    set i to 0\n'
        '    repeat with t in tabs of w\n'
        '      set i to i + 1\n'
        '      if URL of t contains "trade-options" then\n'
        '        set active tab index of w to i\n'
        '        set index of w to 1\n'
        '        activate\n'
        '        return "focused:tab-" & i\n'
        '      end if\n'
        '    end repeat\n'
        '  end repeat\n'
        '  activate\n'
        '  return "trade-options-tab-not-found"\n'
        'end tell'
    )
    try:
        r = _sp.run(["osascript"], input=script,
                    capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or r.stderr.strip()
    except Exception as exc:
        return str(exc)


def _run_js(js: str, label: str) -> str:
    import subprocess as _sp
    try:
        r = _sp.run(["osascript", "-e", _osascript_js(js)],
                    capture_output=True, text=True, timeout=12)
        out = r.stdout.strip() or r.stderr.strip()
        log.info("[fidelity] %s → %s", label, out)
        return out
    except Exception as exc:
        log.warning("[fidelity] %s failed: %s", label, exc)
        return str(exc)


def _launch_fidelity_trade(trade: dict) -> None:
    """Open Fidelity options trade page in Chrome and auto-fill via osascript JS injection."""
    import subprocess as _sp
    import time as _time
    from datetime import datetime as _dt

    symbol      = str(trade.get("symbol", ""))
    option_type = str(trade.get("option_type", "call")).lower()
    qty         = str(trade.get("qty", 1))
    opt_price   = str(trade.get("opt_price", ""))
    expiry_raw  = str(trade.get("expiry", ""))   # e.g. "2026-07-10"
    strike_raw  = str(trade.get("strike", ""))   # e.g. "74.0"
    limit_price = f"{float(opt_price):.2f}" if opt_price else ""
    radio_id    = "call-put-0-call" if option_type == "call" else "call-put-0-put"

    # Format expiry for Fidelity dropdown text, e.g. "Jul 10, 2026"
    expiry_label = ""
    if expiry_raw:
        try:
            expiry_label = _dt.strptime(expiry_raw, "%Y-%m-%d").strftime("%b %d, %Y").replace(" 0", " ")
        except Exception:
            expiry_label = expiry_raw

    # Format strike for dropdown text match, e.g. "74" or "74.00"
    strike_label = ""
    if strike_raw:
        try:
            sv = float(strike_raw)
            strike_label = str(int(sv)) if sv == int(sv) else f"{sv:.2f}"
        except Exception:
            strike_label = strike_raw

    url = (
        "https://digital.fidelity.com/ftgw/digital/trade-options"
        "?&FULL_BANNER=Y&TIME_IN_FORCE=D&ORDER_TYPE=O&CURRENT_PAGE=TradeOption&DEST_TRADE=Y"
    )
    log.info("[fidelity] Opening trade page for %s %s %s %s", symbol, option_type, strike_label, expiry_label)
    try:
        _sp.Popen(["open", "-a", "Google Chrome", url])
    except Exception as exc:
        log.warning("[fidelity] open -a Chrome failed: %s — trying default", exc)
        _sp.Popen(["open", url])

    _time.sleep(5)   # wait for Angular to render
    focus_result = _focus_fidelity_tab()
    log.info("[fidelity] focus-tab → %s", focus_result)

    # ── 1. Select account: click button#account, wait, pick 3rd item (Babs IRA) ──
    _run_js(_click_nth_account(3), "open-account-dropdown")
    _time.sleep(1.2)
    _run_js(_select_nth_account_item(3), "select-account-3")
    _time.sleep(1.5)

    # ── 2. Symbol — set value then pick from autocomplete dropdown ──
    _run_js(_angular_set("symbol_search", symbol), "set-symbol")
    _time.sleep(1.5)

    # Poll for autocomplete suggestion list, then click the item matching our symbol
    _sym_upper = symbol.upper()
    _ac_poll = (
        f"(function(){{"
        f"var items=[...document.querySelectorAll("
        f"  'ul li, [role=option], [role=listitem], [class*=suggestion], [class*=autocomplete], [class*=result]'"
        f")].filter(e=>e.offsetParent!==null);"
        f"var match=items.find(e=>e.textContent.trim().toUpperCase().startsWith('{_sym_upper}'));"
        f"if(match){{match.click();return 'ac-clicked:'+match.textContent.trim().substring(0,40);}}"
        f"if(items.length)return 'ac-no-match items:'+items.slice(0,3).map(e=>e.textContent.trim().substring(0,20)).join('|');"
        f"return 'ac-no-items';"
        f"}})()"
    )
    for _ac_attempt in range(10):   # up to 5s
        _ac_result = _run_js(_ac_poll, f"pick-symbol-ac-{_ac_attempt}")
        if _ac_result.startswith("ac-clicked:"):
            log.info("[fidelity] symbol autocomplete picked: %s", _ac_result)
            break
        _time.sleep(0.5)
    else:
        # Fallback: press Enter on the symbol field to confirm
        log.warning("[fidelity] no autocomplete found — pressing Enter on symbol field")
        _run_js(
            f"(function(){{"
            f"var f=document.getElementById('symbol_search');"
            f"if(f)f.dispatchEvent(new KeyboardEvent('keydown',{{key:'Enter',keyCode:13,bubbles:true}}));"
            f"return 'enter-sent';"
            f"}})()",
            "symbol-enter"
        )

    # Poll until the expiry dropdown button is enabled (Angular populates it after symbol load).
    # Replaces the old fixed 3-second sleep — more robust across machines with different load times.
    _exp_ready_poll = (
        "(function(){"
        "var btn=document.getElementById('exp_dropdown-0');"
        "if(!btn) return 'no-btn';"
        "if(btn.disabled) return 'disabled';"
        "return 'ready';"
        "})()"
    )
    for _wi in range(20):   # up to 10 s
        _ws = _run_js(_exp_ready_poll, f"wait-exp-dropdown-{_wi}")
        if _ws.strip() == "ready":
            log.info("[fidelity] expiry dropdown ready after ~%.1fs", _wi * 0.5)
            break
        _time.sleep(0.5)
    else:
        log.warning("[fidelity] expiry dropdown never became ready — proceeding anyway")

    # ── 3. Qty ──
    _run_js(_angular_set("quantity-0", qty), "set-qty")
    _time.sleep(0.5)

    # ── 4. Action → Sell to Open ──
    _run_js(_click_id("action_dropdown-0"), "open-action-dropdown")
    _time.sleep(0.8)
    _run_js(_click_dropdown_option("Sell to Open"), "select-sell-to-open")
    _time.sleep(0.5)

    # ── 5. Call / Put ──
    _run_js(
        f"(function(){{var r=document.getElementById('{radio_id}');if(!r)return 'not found:{radio_id}';"
        f"r.click();r.dispatchEvent(new Event('change',{{bubbles:true}}));return 'ok:{radio_id}';}})() ",
        "set-call-put"
    )
    # Poll for expiry dropdown to re-populate after call/put selection changes the chain
    for _cpi in range(14):   # up to 7 s
        _cps = _run_js(_exp_ready_poll, f"wait-exp-after-cp-{_cpi}")
        if _cps.strip() == "ready":
            log.info("[fidelity] expiry dropdown ready after call/put ~%.1fs", _cpi * 0.5)
            break
        _time.sleep(0.5)

    # ── 6. Expiry dropdown ──
    if expiry_label:
        log.info("[fidelity] refocus → %s", _focus_fidelity_tab())
        _run_js(_click_id("exp_dropdown-0"), "open-expiry-dropdown")
        _time.sleep(1.5)
        result_exp = _run_js(_click_dropdown_option(expiry_label), "select-expiry")
        # Fallback: try full month name (e.g. "July 17, 2026" vs "Jul 17, 2026")
        if "not found" in result_exp:
            import calendar as _cal
            month_num = int(expiry_raw.split("-")[1]) if expiry_raw else 0
            full_month = _cal.month_name[month_num] if month_num else ""
            day = str(int(expiry_raw.split("-")[2])) if expiry_raw else ""
            year = expiry_raw.split("-")[0] if expiry_raw else ""
            alt_label = f"{full_month} {day}, {year}"
            log.info("[fidelity] expiry not found as %r — trying %r", expiry_label, alt_label)
            _run_js(_click_dropdown_option(alt_label), "select-expiry-alt")
        _time.sleep(0.5)

    # ── 7. Strike dropdown ──
    if strike_label:
        log.info("[fidelity] refocus → %s", _focus_fidelity_tab())
        # Poll for strike dropdown to be enabled after expiry selection
        _strike_ready_poll = (
            "(function(){"
            "var btn=document.getElementById('strike_dropdown-0');"
            "if(!btn) return 'no-btn';"
            "if(btn.disabled) return 'disabled';"
            "return 'ready';"
            "})()"
        )
        for _sri in range(14):   # up to 7 s
            _srs = _run_js(_strike_ready_poll, f"wait-strike-{_sri}")
            if _srs.strip() == "ready":
                log.info("[fidelity] strike dropdown ready after ~%.1fs", _sri * 0.5)
                break
            _time.sleep(0.5)
        _run_js(_click_id("strike_dropdown-0"), "open-strike-dropdown")
        _time.sleep(1.0)
        result = _run_js(_click_dropdown_option(strike_label), "select-strike")
        # Fallback: try alternate format (e.g. "72.5" vs "72.50" vs "72")
        if "not found" in result:
            try:
                sv = float(strike_raw)
                alts = {f"{sv}", f"{sv:.1f}", f"{int(sv)}", f"{sv:.2f}"}
                alts.discard(strike_label)
                for alt in alts:
                    result2 = _run_js(_click_dropdown_option(alt), f"select-strike-alt-{alt}")
                    if "not found" not in result2:
                        break
            except Exception:
                pass
        _time.sleep(0.5)

    # ── 8. Order type → Limit ──
    _run_js(_click_id("ordertype-dropdown"), "open-ordertype-dropdown")
    _time.sleep(0.8)
    _run_js(_click_dropdown_option("Limit"), "select-limit-order")
    _time.sleep(0.5)

    # ── 9. Limit price ──
    # Poll until the field is enabled (Angular enables it once Limit order type is confirmed)
    if limit_price:
        _time.sleep(0.5)
        _price_poll = (
            "(function(){"
            "var f=document.getElementById('dest-limitPrice');"
            "if(!f)return 'no-field';"
            "if(f.disabled)return 'disabled';"
            "return 'enabled';"
            "})()"
        )
        for _pi in range(14):   # up to 7s
            _ps = _run_js(_price_poll, f"poll-price-{_pi}")
            if _ps.strip() == "enabled":
                break
            _time.sleep(0.5)
        else:
            log.warning("[fidelity] price field never became enabled — forcing it")
            _run_js(
                "(function(){var f=document.getElementById('dest-limitPrice');"
                "if(f){f.removeAttribute('disabled');f.removeAttribute('readonly');"
                "f.dispatchEvent(new Event('focus',{bubbles:true}));} return 'forced';} )()",
                "force-enable-price"
            )
            _time.sleep(0.3)

        # Focus the field and insert value via execCommand (creates trusted input mutation)
        _run_js(
            f"(function(){{"
            f"var f=document.getElementById('dest-limitPrice');"
            f"if(!f)return 'no-field';"
            f"f.focus();"
            f"f.select();"
            f"document.execCommand('selectAll');"
            f"document.execCommand('insertText',false,'{limit_price}');"
            f"return 'price-set:'+f.value;"
            f"}})()",
            "set-limit-price"
        )
        _time.sleep(0.3)
        # Move focus to qty field — commits the price value and closes the dropdown panel
        _run_js(
            "(function(){"
            "var q=document.getElementById('quantity-0');"
            "if(q){q.focus();return 'focus-moved-to-qty';}"
            "return 'qty-not-found';"
            "})()",
            "commit-price"
        )
        _time.sleep(0.3)

    # TIF is pre-set to Day via the URL parameter TIME_IN_FORCE=D — no click needed.

    log.info("[fidelity] Form fill complete — review and place order in Fidelity")


def _migrate_db_ul_cost_basis() -> None:
    """Add ul_cost_basis and entry-snapshot columns to positions table if not
    already present. Defensive mirror of option_trade_journal.py's init_db()
    migration — either process may start up first against the shared DB."""
    import sqlite3
    db_path = os.path.normpath(JOURNAL_DB)
    if not os.path.exists(db_path):
        return
    try:
        con = sqlite3.connect(db_path)
        cols = [row[1] for row in con.execute("PRAGMA table_info(positions)").fetchall()]
        if "ul_cost_basis" not in cols:
            con.execute("ALTER TABLE positions ADD COLUMN ul_cost_basis REAL DEFAULT 0")
            con.commit()
            log.info("DB migrated: added ul_cost_basis column to positions")
        for col, decl in [
            ("entry_iv",               "REAL DEFAULT NULL"),
            ("entry_underlying_price", "REAL DEFAULT NULL"),
            ("entry_delta",            "REAL DEFAULT NULL"),
            ("entry_gamma",            "REAL DEFAULT NULL"),
            ("entry_theta",            "REAL DEFAULT NULL"),
            ("entry_vega",             "REAL DEFAULT NULL"),
            ("entry_snapshot_at",      "TEXT DEFAULT NULL"),
        ]:
            if col not in cols:
                con.execute(f"ALTER TABLE positions ADD COLUMN {col} {decl}")
                con.commit()
                log.info("DB migrated: added %s column to positions", col)
        con.close()
    except Exception as exc:
        log.warning("DB migration (ul_cost_basis/entry-snapshot) failed: %s", exc)


def run_web_dashboard(token: str, account_id: str) -> None:
    """Start a Flask web dashboard showing all open positions with live data."""
    setup_logging()
    _migrate_db_ul_cost_basis()

    try:
        from flask import Flask, Response, request as flask_request
    except ImportError:
        log.error("Flask is required for --web mode. Install with: pip install flask")
        sys.exit(1)

    import base64
    import html as html_mod
    import json
    import urllib.parse
    import webbrowser
    import threading
    import time as _time

    # --- Token refresh logic ---
    _token_state = {
        "token": token,
        "expires_at": _time.time() + 55 * 60,  # treat initial token as ~55 min valid
    }
    _token_lock = threading.Lock()

    def _get_valid_token() -> str:
        """Return a fresh access token, refreshing if within 5 minutes of expiry."""
        with _token_lock:
            if _time.time() >= _token_state["expires_at"]:
                secret = os.environ.get("PUBLIC_API_SECRET", "")
                log.info("Access token expired — refreshing.")
                _token_state["token"] = get_access_token(secret)
                _token_state["expires_at"] = _time.time() + 55 * 60
            return _token_state["token"]

    app = Flask(__name__)
    app.config["JSON_SORT_KEYS"] = False

    # Process-start timestamp, embedded in every served page and every /api/eval
    # response — client-side JS compares its own baked-in copy against the live
    # value on every poll and force-reloads on mismatch. Without this, a tab left
    # open across a server restart/deploy just keeps polling data-only endpoints
    # forever on stale JS, silently ignoring fixes (bit us three times running).
    _SERVER_VERSION = str(int(datetime.datetime.now().timestamp()))

    # ── Cloudflare Access auth ──────────────────────────────────────────────
    # No credential check here by design (matches dogpile/stocks/synopticon):
    # this process binds to 127.0.0.1 only, so the only traffic that can ever
    # reach it is (a) same-machine processes — the journal's entry-snapshot
    # call, or a local curl, already trusted at the same boundary as
    # filesystem access — or (b) cloudflared relaying traffic AFTER
    # Cloudflare Access has already enforced its login policy at the edge; an
    # unauthenticated external request never reaches the tunnel's local
    # forward step at all. The header below is identity for logging, not a
    # pass/fail gate.
    def _cf_email() -> str:
        return flask_request.headers.get("CF-Access-Authenticated-User-Email", "").lower().strip()

    def require_auth(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            email = _cf_email()
            if email:
                log.info("AUTH  %s %s  user=%s", flask_request.method, flask_request.path, email)
            return f(*args, **kwargs)
        return decorated

    @app.errorhandler(Exception)
    def _handle_exception(exc):
        from werkzeug.exceptions import HTTPException
        if isinstance(exc, HTTPException):
            return exc
        log.exception("[flask] unhandled exception in request: %s", exc)
        return Response("", status=500, mimetype="text/html")

    # Persistent cache: posKey -> {recommendation, text, ticker, error, qa_thread}
    _CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".analysis_cache.json")
    _analysis_inflight: set[str] = set()
    _cache_lock = threading.Lock()

    def _load_cache() -> dict:
        try:
            with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_cache(cache: dict) -> None:
        try:
            with open(_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f, default=str)
        except Exception as exc:
            log.warning("Could not save analysis cache: %s", exc)

    _analysis_cache: dict[str, dict] = _load_cache()

    # Persistent "don't analyze" set: posKey -> excluded from scheduled_refresh's
    # flagged-position auto-analysis pass. Manual Analyze-button clicks and the
    # flagging/badge logic itself are untouched by this — it only gates the cron.
    _DONT_ANALYZE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".dont_analyze.json")

    def _load_dont_analyze() -> set[str]:
        try:
            with open(_DONT_ANALYZE_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            return set()

    def _save_dont_analyze(keys: set[str]) -> None:
        try:
            with open(_DONT_ANALYZE_FILE, "w", encoding="utf-8") as f:
                json.dump(sorted(keys), f)
        except Exception as exc:
            log.warning("Could not save dont-analyze set: %s", exc)

    _dont_analyze: set[str] = _load_dont_analyze()

    def _pos_key_str(p: dict) -> str:
        strike = p.get("strike")
        try:
            f = float(strike)
            strike_str = str(int(f)) if f == int(f) else str(f)
        except (TypeError, ValueError):
            strike_str = str(strike or "")
        return "|".join([str(p.get("symbol", "")), str(p.get("option_type", "")), strike_str, str(p.get("expiry") or "")])

    iv_history.ensure_vix_backfilled()

    def _startup_ensure_fed_source():
        notebook_id = os.environ.get("NOTEBOOKLM_NOTEBOOK_ID")
        if not notebook_id:
            return
        try:
            from notebooklm import NotebookLMClient
            async def _run():
                async with NotebookLMClient.from_storage() as client:
                    await _ensure_fed_calendar_source(client, notebook_id)
            asyncio.run(_run())
        except Exception as exc:
            log.warning("[fed-calendar] startup ensure-source failed: %s", exc)
    threading.Thread(target=_startup_ensure_fed_source, daemon=True).start()

    # Re-evaluate recommendations on load so any previously mis-classified
    # entries (e.g. ROLL when primary was HOLD) get corrected immediately
    # without requiring a fresh NotebookLM call.
    _cache_dirty = False
    for _ck, _cv in _analysis_cache.items():
        if _cv.get("text") and not _cv.get("error"):
            _fresh_rec = _detect_recommendation(_cv["text"])
            if _fresh_rec != _cv.get("recommendation"):
                log.info(
                    "Cache correction: %s %s → %s (re-detected from text)",
                    _ck, _cv.get("recommendation"), _fresh_rec,
                )
                _cv["recommendation"] = _fresh_rec
                _cache_dirty = True
    if _cache_dirty:
        _save_cache(_analysis_cache)

    def _serial(obj):
        if obj is None:
            return None
        raise TypeError(f"Object of type {type(obj)} is not JSON serialisable")

    def _sanitize(obj):
        """Recursively replace float NaN/Inf with None so json.dumps produces valid JSON."""
        import math
        if isinstance(obj, float):
            return None if (math.isnan(obj) or math.isinf(obj)) else obj
        if isinstance(obj, dict):
            return {k: _sanitize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_sanitize(v) for v in obj]
        return obj

    @app.route("/")
    @require_auth
    def index():
        html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Options</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#0d1117;color:#e6edf3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:100vh;gap:40px}
  h1{font-size:18px;font-weight:500;color:#8892a4;letter-spacing:.04em;text-transform:uppercase}
  .cards{display:flex;gap:24px}
  a.card{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;
         width:200px;height:140px;background:#161b22;border:1px solid #30363d;border-radius:12px;
         text-decoration:none;color:#e6edf3;font-size:15px;font-weight:500;transition:border-color .15s,background .15s}
  a.card:hover{border-color:#58a6ff;background:#1c2230}
  a.card svg{opacity:.6}
</style>
</head>
<body>
<h1>Options</h1>
<div class="cards">
  <a class="card" href="/dashboard">
    <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/>
      <rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/>
    </svg>
    Dashboard
  </a>
  <a class="card" href="/journal">
    <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
    </svg>
    Trade Journal
  </a>
</div>
</body>
</html>"""
        return Response(html, mimetype="text/html")

    _JOURNAL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    @app.route("/journal", defaults={"path": ""}, methods=_JOURNAL_METHODS)
    @app.route("/journal/<path:path>", methods=_JOURNAL_METHODS)
    @require_auth
    def journal_proxy(path):
        import requests as _req
        url = f"http://localhost:5001/{path}" if path else "http://localhost:5001/journal"
        qs = flask_request.query_string.decode()
        if qs:
            url += f"?{qs}"
        try:
            resp = _req.request(
                method=flask_request.method,
                url=url,
                headers={k: v for k, v in flask_request.headers if k.lower() not in ("host", "content-length", "transfer-encoding")},
                data=flask_request.get_data(),
                allow_redirects=False,
                timeout=30,
            )
            excluded = {"content-encoding", "transfer-encoding", "connection"}
            headers = {k: v for k, v in resp.headers.items() if k.lower() not in excluded}
            return Response(resp.content, status=resp.status_code, headers=headers)
        except _req.exceptions.ConnectionError:
            return Response("Journal offline", status=503, mimetype="text/plain")

    @app.route("/dashboard")
    @require_auth
    def dashboard():
        try:
            mf_html = _build_multiplier_files_html()
        except BaseException:
            log.exception("[multiplier] build failed — skipping header status")
            mf_html = ""
        return Response(
            _WEB_DASHBOARD_HTML
            .replace("<!--MULTIPLIER_FILES-->", mf_html)
            .replace("<!--SERVER_VERSION-->", _SERVER_VERSION),
            mimetype="text/html",
            headers={"Cache-Control": "no-store"},
        )

    @app.route("/api/multiplier-status-html")
    @require_auth
    def api_multiplier_status_html():
        try:
            resp = Response(_build_multiplier_files_html(), mimetype="text/html")
            resp.headers["Cache-Control"] = "no-store"
            return resp
        except BaseException:
            log.exception("[multiplier] build failed on status-html endpoint")
            return Response("", mimetype="text/html", status=500)

    # Cache notebook titles so a single timeout doesn't blank the status display.
    # Tuple of (fetched_at: datetime, titles: set[str]) or None if never succeeded.
    _nb_titles_cache: list = [None]   # mutable container so closure can write to it
    _NB_CACHE_TTL_S  = 300            # serve cached result for up to 5 min
    _NB_CACHE_STALE_S = 900           # show amber only after 15 min without a good fetch

    def _fetch_notebook_titles() -> set[str] | None:
        """Query NotebookLM source titles via subprocess.
        Returns a set on success, a cached set if the fetch fails but cache is fresh,
        or None (amber) only when the cache is older than _NB_CACHE_STALE_S."""
        import subprocess, json as _json
        notebook_id = os.environ.get("NOTEBOOKLM_NOTEBOOK_ID", "")
        if not notebook_id:
            return set()

        # Serve from cache if it's still within TTL
        cached = _nb_titles_cache[0]
        now = datetime.datetime.now()
        if cached is not None:
            age = (now - cached[0]).total_seconds()
            if age < _NB_CACHE_TTL_S:
                return cached[1]

        script = (
            "import asyncio, json, sys\n"
            "async def _main():\n"
            "    from notebooklm import NotebookLMClient\n"
            f"    async with NotebookLMClient.from_storage() as c:\n"
            f"        titles = [s.title for s in await c.sources.list({_json.dumps(notebook_id)}) if s.title]\n"
            "    print(json.dumps(titles))\n"
            "asyncio.run(_main())\n"
        )
        try:
            r = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True, text=True, timeout=60,
                env=os.environ.copy(),
            )
            if r.returncode == 0 and r.stdout.strip():
                titles = set(_json.loads(r.stdout.strip()))
                _nb_titles_cache[0] = (now, titles)
                return titles
            if r.stderr:
                log.warning("[multiplier] notebook subprocess stderr: %s", r.stderr[-500:])
        except BaseException as exc:
            log.warning("[multiplier] notebook subprocess failed: %s: %s", type(exc).__name__, exc)

        # Fetch failed — return stale cache if it's not too old, else None (amber)
        if cached is not None:
            age = (now - cached[0]).total_seconds()
            if age < _NB_CACHE_STALE_S:
                log.info("[multiplier] serving stale notebook cache (%.0fs old)", age)
                return cached[1]
        return None

    def _build_multiplier_files_html() -> str:
        MDIR = Path("/Users/joeandbabs/work/retirement/options")
        today = datetime.date.today()
        weekday = today.weekday()  # 0=Mon … 6=Sun

        # this_fri = most recent Friday (today if today is Friday)
        # next_fri = the Friday after this_fri
        this_fri_date = today - datetime.timedelta(days=(weekday - 4) % 7)
        next_fri_date = this_fri_date + datetime.timedelta(days=7)

        # PLAN files have a grace period: csv due Sunday, pdf due Monday.
        # next_fri - 5 = Sunday, next_fri - 4 = Monday.
        plan_csv_due = next_fri_date - datetime.timedelta(days=5)  # Sunday
        plan_pdf_due = next_fri_date - datetime.timedelta(days=4)  # Monday

        # PLAN shows this week's files until Sunday, then flips to next week
        plan_fri_date = next_fri_date if today >= plan_csv_due else this_fri_date
        plan_fri_str  = plan_fri_date.strftime("%Y_%m_%d")
        plan_fri_week = plan_fri_date.isocalendar()[1]

        # REVIEW shows last week until Sunday, then flips to this week
        review_fri_date = this_fri_date if today >= plan_csv_due else this_fri_date - datetime.timedelta(days=7)
        last_fri_str    = review_fri_date.strftime("%Y_%m_%d")
        last_fri_week   = review_fri_date.isocalendar()[1]

        def _latest_name(pattern):
            hits = []
            for f in MDIR.glob(pattern):
                m = re.search(r"Week_(\d+)", f.name)
                if m:
                    hits.append((int(m.group(1)), f.name))
            return max(hits)[1] if hits else None

        def _disk_name(prefix, date_str, ext):
            hits = list(MDIR.glob(f"{prefix}_{date_str}_Week_*.{ext}"))
            return hits[0].name if hits else None

        # Always query the notebook on every explicit refresh (browser or button).
        nb = _fetch_notebook_titles()  # None = unreachable, set() = reachable but empty

        # Find current-week titles in notebook by expected date prefix
        def _nb_name(prefix, date_str, ext):
            if nb is None:
                return None
            pfx = f"{prefix}_{date_str}_Week_"
            return next((t for t in nb if t.startswith(pfx) and t.endswith(f".{ext}")), None)

        nb_plan_pdf   = _nb_name("PLAN",   plan_fri_str, "pdf")
        nb_plan_csv   = _nb_name("PLAN",   plan_fri_str, "csv")
        nb_review_pdf = _nb_name("REVIEW", last_fri_str, "pdf")

        def _label(nb_match, prefix, date_str, week_num, ext, due_date=None):
            # Returns (label, status) where status is:
            #   "ok"      — in notebook (green)
            #   "unknown" — notebook unreachable (amber)
            #   "pending" — not in notebook, not yet due (gray)
            #   "overdue" — not in notebook, past due (red)
            if nb_match:
                return nb_match, "ok"
            disk = _disk_name(prefix, date_str, ext)
            label = disk or f"{prefix}_{date_str}_Week_{week_num:02d}.{ext}"
            if nb is None:
                return label, "unknown"
            past_due = due_date is None or today >= due_date
            return label, "overdue" if past_due else "pending"

        items = [
            _label(nb_plan_pdf,   "PLAN",   plan_fri_str, plan_fri_week, "pdf", plan_pdf_due),
            _label(nb_plan_csv,   "PLAN",   plan_fri_str, plan_fri_week, "csv", plan_csv_due),
            _label(nb_review_pdf, "REVIEW", last_fri_str, last_fri_week, "pdf", plan_csv_due),
        ]

        parts = []
        for label, status in items:
            if not label:
                continue
            if status == "ok":
                color, title, cls = "#22c55e", "In notebook", ""
            elif status == "unknown":
                color, title, cls = "#f59e0b", "NotebookLM unreachable", ""
            elif status == "pending":
                color, title, cls = "#6b7280", "Not yet due", ""
            else:
                color, title, cls = "#ef4444", "Overdue — not in notebook", ' class="blink"'
            parts.append(
                f'<span{cls} style="font-size:13px;color:{color};white-space:nowrap" title="{title}">'
                f"{label}</span>"
            )
        sep = '<span style="color:#8892a4"> | </span>'
        return sep.join(parts)

    @app.route("/api/prices")
    @require_auth
    def api_prices():
        """Return live underlying prices + ATR values for all open + unborn + former positions (yfinance)."""
        positions = get_all_open_positions()
        tickers = {p["symbol"].upper() for p in positions}
        # Also include tickers from unborn rows so the poller updates them too
        try:
            if os.path.exists(_UNBORN_ROWS_FILE):
                with open(_UNBORN_ROWS_FILE, "r", encoding="utf-8") as f:
                    unborn = json.load(f)
                for row in unborn.values():
                    sym = (row.get("symbol") or "").upper()
                    if sym:
                        tickers.add(sym)
        except Exception:
            pass
        # Also include former position tickers (closed positions not in open/unborn)
        try:
            import sqlite3 as _sq
            _db = os.path.normpath(JOURNAL_DB)
            con = _sq.connect(f"file:{_db}?mode=ro", uri=True)
            open_syms = {r[0].upper() for r in con.execute(
                "SELECT DISTINCT symbol FROM positions WHERE status='open'"
            ).fetchall()}
            for (sym,) in con.execute(
                "SELECT DISTINCT symbol FROM positions WHERE status != 'open'"
            ).fetchall():
                sym = sym.upper()
                if sym not in open_syms:
                    tickers.add(sym)
            con.close()
        except Exception:
            pass
        result = {}
        for sym in tickers:
            price = get_underlying_price_fresh(sym)
            atr   = get_atr(sym, period=14)
            result[sym] = {
                "price":  price,
                "atr":    atr,
                "buffer": round(atr * 1.5, 4) if atr else None,
            }
        result["__vix__"] = {"price": get_vix()}
        return Response(json.dumps(_sanitize(result), default=_serial), mimetype="application/json")

    @app.route("/api/eval")
    @require_auth
    def api_eval():
        import datetime as _dt
        log.info("[DASHBOARD-REFRESH] eval requested at %s", _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        _price_cache.clear()  # force fresh underlying prices on every manual refresh
        data = get_eval_data(_get_valid_token(), account_id, ticker=None, verbose=False)
        data["server_version"] = _SERVER_VERSION
        # Include server-side analysis cache so the client can restore
        # recommendation badges after a manual refresh
        with _cache_lock:
            for _p in data.get("positions", []):
                _p["dont_analyze"] = _pos_key_str(_p) in _dont_analyze
            data["cached_recommendations"] = {
                k: {
                    "recommendation": v.get("recommendation"),
                    "chain_cash": v.get("chain_cash"),
                    "run_at": v.get("_run_at"),
                    "error": v.get("error"),
                }
                for k, v in _analysis_cache.items()
                if not v.get("error")
            }
            data["inflight"] = list(_analysis_inflight)
            data["unborn_inflight"] = list({
                "|".join(k.split("|")[:2]) for k in _unborn_inflight
            })
        return Response(json.dumps(_sanitize(data), default=_serial), mimetype="application/json")

    @app.route("/api/dont-analyze", methods=["POST"])
    @require_auth
    def api_dont_analyze():
        body = flask_request.get_json(force=True, silent=True) or {}
        pos_key = body.get("position_key", "")
        checked = bool(body.get("checked"))
        if not pos_key:
            return Response(json.dumps({"error": "Missing position_key"}), status=400, mimetype="application/json")
        with _cache_lock:
            if checked:
                _dont_analyze.add(pos_key)
            else:
                _dont_analyze.discard(pos_key)
            _save_dont_analyze(_dont_analyze)
        return Response(json.dumps({"ok": True}), mimetype="application/json")

    @app.route("/api/analyze", methods=["POST"])
    @require_auth
    def api_analyze():
        body = flask_request.get_json(force=True, silent=True) or {}
        pos_key = body.get("position_key", "")
        force = bool(body.get("force"))
        if not pos_key:
            return Response(json.dumps({"error": "Missing position_key"}), status=400, mimetype="application/json")

        with _cache_lock:
            _cached = _analysis_cache.get(pos_key)
            if _cached and not _cached.get("error") and not force:
                return Response(json.dumps(_cached), mimetype="application/json")
            # Re-run errors only after a 5-minute cooldown
            if _cached and _cached.get("error"):
                import time as _t
                if _t.time() - _cached.get("_error_at", 0) < 300:
                    return Response(json.dumps(_cached), mimetype="application/json")
            # Already running — tell the client to retry
            if pos_key in _analysis_inflight:
                return Response(
                    json.dumps({"status": "in_progress", "retry_after": 5}),
                    status=202, mimetype="application/json",
                )
            _analysis_inflight.add(pos_key)

        # Parse key: symbol|option_type|strike|expiry
        parts = pos_key.split("|")
        if len(parts) != 4:
            with _cache_lock:
                _analysis_inflight.discard(pos_key)
            return Response(json.dumps({"error": f"Bad position_key: {pos_key}"}), status=400, mimetype="application/json")
        sym, opt_type, strike_str, expiry = parts
        ticker = sym.upper()

        # ── Short-circuit: OTM position expiring today or already expired ────
        import datetime as _dt
        try:
            _exp_date = _dt.date.fromisoformat(expiry)
            _dte = (_exp_date - _dt.date.today()).days
            _strike = float(strike_str)
            _ul = get_underlying_price(ticker) or 0.0
            _otm = (opt_type.upper() == "CALL" and _ul < _strike) or \
                   (opt_type.upper() == "PUT"  and _ul > _strike)
            if _dte <= 0 and _otm:
                _result = {
                    "recommendation": "HOLD",
                    "text": (
                        f"{ticker} {opt_type.upper()} ${_strike:.2f} expires {'today' if _dte == 0 else 'expired'} "
                        f"and is OTM (u/l ${_ul:.2f}). No action needed — let it expire worthless."
                    ),
                    "ticker": ticker,
                    "auto": True,
                }
                with _cache_lock:
                    _analysis_cache[pos_key] = _result
                    _analysis_inflight.discard(pos_key)
                return Response(json.dumps(_result), mimetype="application/json")
        except Exception:
            pass  # fall through to normal analysis
        # ─────────────────────────────────────────────────────────────────────

        notebook_id = os.environ.get("NOTEBOOKLM_NOTEBOOK_ID")
        if not notebook_id:
            return Response(
                json.dumps({"error": "NOTEBOOKLM_NOTEBOOK_ID not set"}),
                status=500, mimetype="application/json",
            )

        # Re-fetch the token (may have expired) and get open positions for this ticker
        try:
            secret = os.environ.get("PUBLIC_API_SECRET", "")
            fresh_token = get_access_token(secret)
            fresh_account_id = get_account_id(fresh_token)
            open_positions = get_all_open_positions(ticker)
        except Exception as exc:
            with _cache_lock:
                _analysis_inflight.discard(pos_key)
            return Response(json.dumps({"error": str(exc)}), status=500, mimetype="application/json")

        log.info("[web] Running ROLL analysis for %s (%s %s exp %s)", ticker, opt_type, strike_str, expiry)
        try:
            result = asyncio.run(
                run_roll_for_position(fresh_token, fresh_account_id, ticker, open_positions, notebook_id, pos_key=pos_key)
            )
            if result.get("error"):
                log.error("[web] Analysis error for %s: %s", ticker, result["error"])
            else:
                log.info("[web] Analysis complete for %s → %s", ticker, result.get("recommendation"))
        except Exception as exc:
            log.exception("[web] Unhandled exception for %s", ticker)
            result = {"error": str(exc), "recommendation": "HOLD", "text": "", "ticker": ticker}
        finally:
            with _cache_lock:
                _analysis_inflight.discard(pos_key)

        with _cache_lock:
            import time as _time
            # Preserve any claude_*/openai_* second-opinion fields already
            # merged into this entry by api_claude_compare()/api_openai_compare()
            # — a plain overwrite here would silently wipe them if a slow
            # primary analysis lands after a comparison already ran, since the
            # comparison endpoints' own writes only ever merge, never replace.
            # Only when NOT forced: a forced re-analyze means the position's
            # state changed enough to warrant a fresh look (auto-rerun on
            # flag/delta/underlying change) — the old second opinions were
            # made against the prior state and are no longer valid, so they
            # should be dropped rather than carried forward against new data.
            if not force:
                _existing = _analysis_cache.get(pos_key) or {}
                _claude_fields = {k: v for k, v in _existing.items() if k.startswith(("claude_", "openai_"))}
                result.update(_claude_fields)
            if not result.get("error"):
                result["_run_at"] = _time.strftime("%-m/%-d %-I:%M %p ET", _time.localtime())
                _analysis_cache[pos_key] = result
                _save_cache(_analysis_cache)
            else:
                result["_error_at"] = _time.time()
                _analysis_cache[pos_key] = result
        return Response(json.dumps(_sanitize(result), default=_serial), mimetype="application/json")

    @app.route("/api/openai-compare", methods=["POST"])
    @require_auth
    def api_openai_compare():
        """
        Second opinion via OpenAI's GPT-5.6 Luna, stored alongside Claude's
        primary recommendation for the same pos_key — comparison only,
        doesn't touch the main `recommendation`/`text` fields. See
        openai_advisor.py for the caching design (same CORE/weekly/Fed
        prefix as Claude's, automatic OpenAI prompt caching instead of
        explicit cache_control breakpoints).
        """
        body = flask_request.get_json(force=True, silent=True) or {}
        pos_key = body.get("position_key", "")
        if not pos_key:
            return Response(json.dumps({"error": "Missing position_key"}), status=400, mimetype="application/json")

        parts = pos_key.split("|")
        if len(parts) != 4:
            return Response(json.dumps({"error": f"Bad position_key: {pos_key}"}), status=400, mimetype="application/json")
        sym, opt_type, strike_str, expiry = parts
        ticker = sym.upper()

        try:
            fresh_token = _get_valid_token()
            eval_data = get_eval_data(fresh_token, account_id, ticker=ticker, verbose=False)
            pos = None
            for p in eval_data.get("positions", []):
                try:
                    if (str(p.get("option_type", "")).lower() == opt_type.lower()
                            and float(p.get("strike") or -1) == float(strike_str)
                            and str(p.get("expiry")) == expiry):
                        pos = p
                        break
                except (TypeError, ValueError):
                    continue
            if pos is None:
                return Response(json.dumps({"error": f"Position not found: {pos_key}"}), status=404, mimetype="application/json")

            key_dates = get_key_dates(ticker)
            context = openai_advisor.build_position_context(pos, eval_data.get("vix"), key_dates)
            with _cache_lock:
                chain_candidates_text = _analysis_cache.get(pos_key, {}).get("chain_candidates_text")
            result = openai_advisor.query_openai_advisor(context, chain_candidates_text)
        except Exception as exc:
            log.exception("[openai-compare] failed for %s", pos_key)
            result = {"error": str(exc), "recommendation": None, "text": ""}

        import time as _time
        result["_run_at"] = _time.strftime("%-m/%-d %-I:%M %p ET", _time.localtime())

        with _cache_lock:
            entry = _analysis_cache.setdefault(pos_key, {})
            entry["openai_recommendation"] = result.get("recommendation")
            entry["openai_text"] = result.get("text")
            entry["openai_tail_text"] = result.get("tail_text")
            entry["openai_error"] = result.get("error")
            entry["openai_run_at"] = result["_run_at"]
            entry["openai_qa_thread"] = []  # fresh compare invalidates any prior follow-up thread
            _save_cache(_analysis_cache)

        return Response(json.dumps(_sanitize(result), default=_serial), mimetype="application/json")

    @app.route("/api/openai-ask", methods=["POST"])
    @require_auth
    def api_openai_ask():
        """Follow-up question in the same conversation as an existing Luna
        comparison (api_openai_compare)."""
        body = flask_request.get_json(force=True, silent=True) or {}
        pos_key = body.get("position_key", "")
        question = (body.get("question") or "").strip()
        if not pos_key or not question:
            return Response(json.dumps({"error": "Missing position_key or question"}), status=400, mimetype="application/json")

        with _cache_lock:
            cached = _analysis_cache.get(pos_key)
        if not cached or not cached.get("openai_text"):
            return Response(json.dumps({"error": "No Luna analysis yet for this position — click Compare with Luna first."}), status=404, mimetype="application/json")

        qa_thread = cached.get("openai_qa_thread") or []
        try:
            result = openai_advisor.ask_position_followup(
                cached.get("openai_tail_text") or "",
                cached["openai_text"],
                qa_thread,
                question,
            )
        except Exception as exc:
            log.exception("[openai-ask] failed for %s", pos_key)
            result = {"error": str(exc), "answer": ""}

        if not result.get("error"):
            with _cache_lock:
                entry = _analysis_cache.setdefault(pos_key, {})
                thread = entry.get("openai_qa_thread") or []
                thread.append({"q": question, "a": result["answer"]})
                entry["openai_qa_thread"] = thread
                _save_cache(_analysis_cache)

        return Response(json.dumps(_sanitize(result), default=_serial), mimetype="application/json")

    # ── Unborn routes ──────────────────────────────────────────────────────────
    _UNBORN_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".unborn_cache.json")

    def _load_unborn_cache() -> dict:
        try:
            with open(_UNBORN_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_unborn_cache(cache: dict) -> None:
        try:
            with open(_UNBORN_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f, default=str)
        except Exception as exc:
            log.warning("Could not save unborn cache: %s", exc)

    _unborn_cache_raw: dict[str, dict] = _load_unborn_cache()
    import datetime as _dt_uc
    _today_uc = _dt_uc.date.today().isoformat()
    _unborn_cache: dict[str, dict] = {
        k: v for k, v in _unborn_cache_raw.items()
        if not (v.get("chain") and v["chain"] and
                (v["chain"][0].get("expiry") or "9999") <= _today_uc)
    }
    if len(_unborn_cache) < len(_unborn_cache_raw):
        log.info("[unborn] Purged %d expired cache entries on startup",
                 len(_unborn_cache_raw) - len(_unborn_cache))
        _save_unborn_cache(_unborn_cache)
    _unborn_inflight: set[str] = set()

    @app.route("/api/unborn", methods=["POST"])
    @require_auth
    def api_unborn():
        body          = flask_request.get_json(force=True, silent=True) or {}
        ticker        = body.get("ticker", "").upper().strip()
        qty           = int(body.get("qty") or 1)
        strat         = body.get("strat", "CC").upper()
        force         = bool(body.get("force"))
        if not ticker:
            return Response(json.dumps({"error": "Missing ticker"}), status=400, mimetype="application/json")
        if strat not in ("CC", "CSP"):
            return Response(json.dumps({"error": f"Unknown strat: {strat}"}), status=400, mimetype="application/json")

        # Look up cost basis from DB (CC only); fall back to UI-supplied value if DB has none
        try:
            ui_cost_basis = float(body.get("cost_basis") or 0)
        except (TypeError, ValueError):
            ui_cost_basis = 0.0
        ul_cost_basis = 0.0
        if strat == "CC":
            ul_cost_basis = get_ul_cost_basis_from_db(ticker)
            if not ul_cost_basis and ui_cost_basis > 0:
                ul_cost_basis = ui_cost_basis
                log.info("[unborn] %s cost basis from UI: %.2f", ticker, ul_cost_basis)
            else:
                log.info("[unborn] %s cost basis from DB: %.2f", ticker, ul_cost_basis)

        cb_key = f"{ul_cost_basis:.2f}" if ul_cost_basis else "0"
        ub_key = f"{ticker}|{strat}|{qty}|{cb_key}"

        with _cache_lock:
            cached = _unborn_cache.get(ub_key)
            if cached and not cached.get("error") and not force:
                return Response(json.dumps(cached), mimetype="application/json")
            # Re-run errors only after a 5-minute cooldown
            if cached and cached.get("error"):
                import time as _t
                if _t.time() - cached.get("_error_at", 0) < 300:
                    return Response(json.dumps(cached), mimetype="application/json")
            if ub_key in _unborn_inflight:
                return Response(json.dumps({"status": "in_progress", "retry_after": 5}),
                                status=202, mimetype="application/json")
            _unborn_inflight.add(ub_key)

        notebook_id = os.environ.get("NOTEBOOKLM_NOTEBOOK_ID")
        if not notebook_id:
            with _cache_lock: _unborn_inflight.discard(ub_key)
            return Response(json.dumps({"error": "NOTEBOOKLM_NOTEBOOK_ID not set"}),
                            status=500, mimetype="application/json")

        try:
            secret = os.environ.get("PUBLIC_API_SECRET", "")
            fresh_token = get_access_token(secret)
            fresh_account_id = get_account_id(fresh_token)
        except Exception as exc:
            with _cache_lock: _unborn_inflight.discard(ub_key)
            return Response(json.dumps({"error": str(exc)}), status=500, mimetype="application/json")

        log.info("[unborn] Running %s analysis for %s qty=%d ul_cost_basis=%.2f",
                 strat, ticker, qty, ul_cost_basis)

        # Run the analysis in a background thread so Playwright launching Chromium
        # cannot disrupt the Flask request thread or the asyncio event loop.
        # The handler returns 202 immediately; the JS polling loop picks up the
        # cached result once the background thread finishes.
        def _run_analysis():
            try:
                result = asyncio.run(
                    asyncio.wait_for(
                        run_unborn_for_ticker(fresh_token, fresh_account_id, ticker, qty, strat,
                                              notebook_id, ul_cost_basis=ul_cost_basis),
                        timeout=300,
                    )
                )
                if result.get("error"):
                    log.error("[unborn] Error for %s: %s", ticker, result["error"])
                else:
                    log.info("[unborn] %s → %s", ticker, result.get("recommendation"))
            except asyncio.TimeoutError:
                log.error("[unborn] Analysis timed out after 300s for %s", ticker)
                result = {"error": "Analysis timed out", "recommendation": "HOLD", "text": "", "ticker": ticker, "strat": strat}
            except Exception as exc:
                log.exception("[unborn] Unhandled exception for %s", ticker)
                result = {"error": str(exc), "recommendation": "HOLD", "text": "", "ticker": ticker, "strat": strat}
            finally:
                with _cache_lock:
                    _unborn_inflight.discard(ub_key)
            with _cache_lock:
                import time as _time
                # Preserve claude_*/openai_* fields already merged in by
                # api_claude_compare_unborn()/api_openai_compare_unborn() — see
                # the matching comment in /api/analyze's write for why a plain
                # overwrite is unsafe, and why this is skipped entirely when
                # force=True (state changed enough to warrant a fresh look —
                # the old second opinions no longer apply).
                if not force:
                    _existing = _unborn_cache.get(ub_key) or {}
                    _claude_fields = {k: v for k, v in _existing.items() if k.startswith(("claude_", "openai_"))}
                    result.update(_claude_fields)
                if not result.get("error"):
                    result["_run_at"] = _time.strftime("%-m/%-d %-I:%M %p ET", _time.localtime())
                    _unborn_cache[ub_key] = result
                    _save_unborn_cache(_unborn_cache)
                else:
                    result["_error_at"] = _time.time()
                    _unborn_cache[ub_key] = result
            if not result.get("error"):
                try:
                    _persist_unborn_display_row(ticker, strat, qty, result)
                except Exception:
                    log.exception("[unborn-rows] failed to server-persist row for %s", ub_key)

        t = threading.Thread(target=_run_analysis, daemon=True)
        t.start()
        return Response(json.dumps({"status": "in_progress", "retry_after": 5}),
                        status=202, mimetype="application/json")

    @app.route("/api/openai-compare-unborn", methods=["POST"])
    @require_auth
    def api_openai_compare_unborn():
        """
        Second opinion via Luna on whether to open a NEW position — covers
        both the unborn table and the Former Positions "Analyze" button.
        See openai_advisor.py.
        """
        body = flask_request.get_json(force=True, silent=True) or {}
        ub_key = body.get("ub_key", "")
        if not ub_key:
            return Response(json.dumps({"error": "Missing ub_key"}), status=400, mimetype="application/json")

        with _cache_lock:
            cached = _unborn_cache.get(ub_key)
            if cached is None:
                prefix = ub_key + "|"
                for k, v in _unborn_cache.items():
                    if k.startswith(prefix):
                        ub_key, cached = k, v
                        break
        if not cached or cached.get("error"):
            return Response(json.dumps({"error": f"No unborn analysis cached for {ub_key} — click Find/Analyze first."}), status=404, mimetype="application/json")

        try:
            ticker = cached.get("ticker", "")
            strat = cached.get("strat", "CC")
            ul_price = cached.get("ul_price")
            ul_cost_basis = cached.get("ul_cost_basis")
            vix = get_vix()
            atr = get_atr(ticker)
            key_dates = get_key_dates(ticker)
            context = openai_advisor.build_unborn_context(ticker, strat, ul_price, ul_cost_basis, vix, atr, key_dates)
            chain_candidates_text = cached.get("chain_candidates_text")
            result = openai_advisor.query_openai_unborn_advisor(context, chain_candidates_text)
        except Exception as exc:
            log.exception("[openai-compare-unborn] failed for %s", ub_key)
            result = {"error": str(exc), "recommendation": None, "text": ""}

        import time as _time
        result["_run_at"] = _time.strftime("%-m/%-d %-I:%M %p ET", _time.localtime())

        with _cache_lock:
            entry = _unborn_cache.setdefault(ub_key, dict(cached))
            entry["openai_recommendation"] = result.get("recommendation")
            entry["openai_text"] = result.get("text")
            entry["openai_tail_text"] = result.get("tail_text")
            entry["openai_error"] = result.get("error")
            entry["openai_run_at"] = result["_run_at"]
            entry["openai_qa_thread"] = []  # fresh compare invalidates any prior follow-up thread
            _save_unborn_cache(_unborn_cache)

        return Response(json.dumps(_sanitize(result), default=_serial), mimetype="application/json")

    @app.route("/api/openai-ask-unborn", methods=["POST"])
    @require_auth
    def api_openai_ask_unborn():
        """Follow-up question in the same conversation as an existing unborn
        Luna comparison (api_openai_compare_unborn)."""
        body = flask_request.get_json(force=True, silent=True) or {}
        ub_key = body.get("ub_key", "")
        question = (body.get("question") or "").strip()
        if not ub_key or not question:
            return Response(json.dumps({"error": "Missing ub_key or question"}), status=400, mimetype="application/json")

        with _cache_lock:
            cached = _unborn_cache.get(ub_key)
            if cached is None:
                prefix = ub_key + "|"
                for k, v in _unborn_cache.items():
                    if k.startswith(prefix):
                        ub_key, cached = k, v
                        break
        if not cached or not cached.get("openai_text"):
            return Response(json.dumps({"error": "No Luna analysis yet for this ticker — click Compare with Luna first."}), status=404, mimetype="application/json")

        qa_thread = cached.get("openai_qa_thread") or []
        try:
            result = openai_advisor.ask_unborn_followup(
                cached.get("openai_tail_text") or "",
                cached["openai_text"],
                qa_thread,
                question,
            )
        except Exception as exc:
            log.exception("[openai-ask-unborn] failed for %s", ub_key)
            result = {"error": str(exc), "answer": ""}

        if not result.get("error"):
            with _cache_lock:
                entry = _unborn_cache.setdefault(ub_key, dict(cached))
                thread = entry.get("openai_qa_thread") or []
                thread.append({"q": question, "a": result["answer"]})
                entry["openai_qa_thread"] = thread
                _save_unborn_cache(_unborn_cache)

        return Response(json.dumps(_sanitize(result), default=_serial), mimetype="application/json")

    _UNBORN_ROWS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "unborn_rows.json")

    def _persist_unborn_display_row(ticker: str, strat: str, qty: int, result: dict) -> None:
        """
        Mirror what the browser's findUnborn()/analyzeFormerPosition() success
        path writes into unborn_rows.json — but from the server, right when a
        background analysis actually finishes. Without this, a completed
        result only ever reaches the dashboard's persisted display state if
        the SAME browser tab that triggered it is still alive and polling
        when it finishes — if that tab was backgrounded, navigated away, or
        hit a network hiccup during a long (multi-minute, retry-heavy)
        analysis, the server has a perfectly good cached answer that the
        Unborn/Former-Positions table silently never shows.
        """
        chain = result.get("chain") or []
        if chain:
            row = dict(chain[0])
        else:
            row = {
                "symbol": ticker, "option_type": strat,
                "_do_nothing": bool(result.get("_do_nothing")),
                "ideal_entry": "DO NOTHING" if result.get("_do_nothing") else None,
            }
        row.update({
            "_qty": qty,
            "_ubKey": urllib.parse.quote(f"{ticker}|{strat}|{qty}", safe=""),
            "_ul_cost_basis": result.get("ul_cost_basis"),
            "_run_at": result.get("_run_at"),
        })
        display_key = f"{ticker}|{strat}"
        with _cache_lock:
            try:
                if os.path.exists(_UNBORN_ROWS_FILE):
                    with open(_UNBORN_ROWS_FILE, "r", encoding="utf-8") as f:
                        rows = json.load(f)
                else:
                    rows = {}
            except (json.JSONDecodeError, OSError):
                rows = {}
            rows[display_key] = row
            try:
                with open(_UNBORN_ROWS_FILE, "w", encoding="utf-8") as f:
                    json.dump(rows, f)
                log.info("[unborn-rows] server-persisted row for %s", display_key)
            except OSError as exc:
                log.warning("[unborn-rows] server-side write failed: %s", exc)

    @app.route("/api/unborn-rows", methods=["GET"])
    @require_auth
    def api_unborn_rows_get():
        try:
            if os.path.exists(_UNBORN_ROWS_FILE):
                with open(_UNBORN_ROWS_FILE, "r", encoding="utf-8") as f:
                    return Response(f.read(), mimetype="application/json")
        except Exception as exc:
            log.warning("[unborn-rows] Read error: %s", exc)
        return Response("{}", mimetype="application/json")

    @app.route("/api/unborn-rows", methods=["POST"])
    @require_auth
    def api_unborn_rows_post():
        try:
            data = flask_request.get_json(force=True) or {}
            with open(_UNBORN_ROWS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f)
            log.info("[unborn-rows] Saved %d rows", len(data))
        except Exception as exc:
            log.warning("[unborn-rows] Write error: %s", exc)
            return Response(json.dumps({"error": str(exc)}), mimetype="application/json", status=500)
        return Response(json.dumps({"status": "ok"}), mimetype="application/json")

    @app.route("/api/cc-qty/<ticker>", methods=["GET"])
    @require_auth
    def api_cc_qty(ticker: str):
        """Return the number of CC contracts available for ticker from trades.db."""
        qty = get_stock_qty_from_db(ticker.upper().strip())
        return Response(json.dumps({"ticker": ticker.upper(), "qty": qty}), mimetype="application/json")

    @app.route("/api/cost-basis/<ticker>", methods=["GET"])
    @require_auth
    def api_cost_basis(ticker: str):
        """Return the underlying cost basis for ticker from trades.db (0 if not found)."""
        cb = get_ul_cost_basis_from_db(ticker.upper().strip())
        return Response(json.dumps({"ticker": ticker.upper(), "ul_cost_basis": cb}), mimetype="application/json")

    @app.route("/api/entry-snapshot", methods=["POST"])
    @require_auth
    def api_entry_snapshot():
        """
        Live IV/spot/Greeks for one contract, at the moment a brand-new position
        is opened. Called by the journal process right after it inserts a new
        position — the journal has no Public.com/market-data access of its own,
        this dashboard process is the only place that does. Best-effort by
        design: the caller (journal) treats any non-200 or error response as
        "couldn't capture," leaves the entry_* columns NULL, and moves on —
        trade recording must never be blocked by this.
        """
        body = flask_request.get_json(force=True, silent=True) or {}
        symbol      = str(body.get("symbol", "")).upper().strip()
        option_type = str(body.get("option_type", "")).upper().strip()
        expiry      = str(body.get("expiry", "")).strip()
        try:
            strike = float(body.get("strike"))
        except (TypeError, ValueError):
            return Response(json.dumps({"error": "bad or missing strike"}), status=400, mimetype="application/json")
        if not symbol or option_type not in ("CALL", "PUT") or not expiry:
            return Response(json.dumps({"error": "missing symbol/option_type/expiry"}), status=400, mimetype="application/json")

        def _f(v):
            try:
                return float(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None

        try:
            tok = _get_valid_token()
            pos_like = {"symbol": symbol, "expiry": expiry, "option_type": option_type, "strike": strike}
            quotes = get_option_quotes(tok, account_id, [pos_like])
            osi = build_osi_symbol(symbol, expiry, option_type, strike).replace(" ", "")
            q = quotes.get(osi, {})
            bid, ask, last = _f(q.get("bid")), _f(q.get("ask")), _f(q.get("last"))
            opt_price = (bid + ask) / 2 if bid is not None and ask is not None else last

            greeks_data = get_option_greeks_batch(tok, account_id, [osi])
            g = greeks_data.get(osi, {})
            spot = get_underlying_price(symbol)

            result = {
                "underlying_price": spot,
                "iv":    _f(g.get("impliedVolatility")),
                "delta": _f(g.get("delta")),
                "gamma": _f(g.get("gamma")),
                "theta": _f(g.get("theta")),
                "vega":  _f(g.get("vega")),
                "opt_price": opt_price,
            }
            return Response(json.dumps(result), mimetype="application/json")
        except Exception as exc:
            log.warning("[entry-snapshot] failed for %s %s %s %s: %s", symbol, option_type, strike, expiry, exc)
            return Response(json.dumps({"error": str(exc)}), status=500, mimetype="application/json")

    @app.route("/api/send-alert", methods=["POST"])
    @require_auth
    def api_send_alert():
        body      = flask_request.get_json(force=True) or {}
        message   = body.get("message", "Options alert triggered")
        user_key  = os.environ.get("PUSHOVER_USER_KEY", "")
        app_token = os.environ.get("PUSHOVER_APP_TOKEN", "")
        if not user_key or not app_token:
            log.warning("[alert] Pushover credentials not set (PUSHOVER_USER_KEY / PUSHOVER_APP_TOKEN)")
            return Response(
                json.dumps({"status": "error", "detail": "Pushover credentials not configured"}),
                mimetype="application/json", status=500,
            )
        import urllib.request as _urlreq
        payload = urllib.parse.urlencode({
            "token":   app_token,
            "user":    user_key,
            "message": message,
            "title":   "Options Alert",
        }).encode()
        req  = _urlreq.Request("https://api.pushover.net/1/messages.json", data=payload)
        resp = _urlreq.urlopen(req, timeout=10)
        result = json.loads(resp.read())
        log.info("[alert] Pushover sent: %s → %s", message, result)
        return Response(json.dumps({"status": "ok", "pushover": result}), mimetype="application/json")

    @app.route("/api/trade", methods=["POST"])
    @require_auth
    def api_trade():
        trade = flask_request.get_json(force=True) or {}
        log.info("[trade] Received trade request: %s", trade)
        t = threading.Thread(target=_launch_fidelity_trade, args=(trade,), daemon=True)
        t.start()
        return Response(json.dumps({"status": "launched", "trade": trade}), mimetype="application/json")

    @app.route("/unborn/<path:ub_key>")
    @require_auth
    def unborn_detail(ub_key: str):
        ub_key = urllib.parse.unquote(ub_key)
        # Exact match first; fall back to prefix match (handles cost-basis suffix added server-side)
        cached = _unborn_cache.get(ub_key)
        if cached is None:
            prefix = ub_key + "|"
            for k, v in _unborn_cache.items():
                if k.startswith(prefix):
                    cached = v
                    break
        parts  = ub_key.split("|")   # TICKER|STRAT|QTY
        title_str = html_mod.escape(" · ".join(parts))
        ticker_sym = html_mod.escape(parts[0].upper())
        # Font size scales down for longer tickers (e.g. 4-char vs 2-char)
        _fs = {1: 52, 2: 44, 3: 34, 4: 26}.get(len(parts[0]), 20)
        _favicon_svg = (
            f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
            f"<circle cx='32' cy='32' r='30' fill='white'/>"
            f"<text x='32' y='32' dominant-baseline='central' text-anchor='middle' "
            f"font-family='monospace' font-weight='bold' font-size='{_fs}' fill='#052e16'>{ticker_sym}</text>"
            f"</svg>"
        )
        favicon_href = "data:image/svg+xml;base64," + base64.b64encode(_favicon_svg.encode()).decode()

        if not cached:
            body_html = "<p style='color:var(--muted)'>Analysis not found. Click <b>Find</b> first.</p>"
        elif cached.get("error"):
            body_html = f"<p style='color:var(--danger)'>Error: {html_mod.escape(cached['error'])}</p>"
        else:
            strat    = cached.get("strat", "")
            if cached.get("_do_nothing") or cached.get("recommendation") == "HOLD":
                rec     = "DO NOTHING"
                rec_cls = "warn"
            else:
                rec     = "SELL TO OPEN" if strat in ("CC", "CSP") else "OPEN"
                rec_cls = "ok"
            text     = cached.get("text", "")
            def _md_cell_ub(c: str) -> str:
                return re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', html_mod.escape(c))
            lines_out = []
            text_lines = text.splitlines()
            i = 0
            while i < len(text_lines):
                ln = text_lines[i]
                if ln.strip().startswith('|') and ln.strip().count('|') >= 2:
                    tbl_lines = []
                    while i < len(text_lines):
                        stripped = text_lines[i].strip()
                        if stripped.startswith('|'):
                            tbl_lines.append(text_lines[i])
                            i += 1
                        elif stripped == '':
                            i += 1
                        else:
                            break
                    header, body_rows = None, []
                    for tl in tbl_lines:
                        cells = [c.strip() for c in tl.strip().strip('|').split('|')]
                        if all(re.match(r'^:?-+:?$', c) for c in cells if c):
                            continue
                        if header is None:
                            header = cells
                        else:
                            body_rows.append(cells)
                    if header:
                        ths = ''.join(f'<th>{_md_cell_ub(c)}</th>' for c in header)
                        trs = ''.join(
                            '<tr>' + ''.join(f'<td>{_md_cell_ub(c)}</td>' for c in row) + '</tr>'
                            for row in body_rows
                        )
                        lines_out.append(f'<table class="atbl"><thead><tr>{ths}</tr></thead><tbody>{trs}</tbody></table>')
                    continue
                ln_esc = html_mod.escape(ln)
                if ln_esc.startswith("### "):
                    lines_out.append(f"<h3>{re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', ln_esc[4:])}</h3>")
                elif ln_esc.startswith("## "):  lines_out.append(f"<h2>{ln_esc[3:]}</h2>")
                elif ln_esc.startswith("# "):   lines_out.append(f"<h1>{ln_esc[2:]}</h1>")
                elif ln_esc.startswith("- ") or ln_esc.startswith("• "): lines_out.append(f"<li>{ln_esc[2:]}</li>")
                elif ln_esc.strip() == "":      lines_out.append("<br>")
                else:
                    ln_esc = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', ln_esc)
                    lines_out.append(f"<p>{ln_esc}</p>")
                i += 1
            run_at_html = (
                f'<div style="font-size:11px;color:var(--muted);margin-bottom:12px">Updated {html_mod.escape(cached["_run_at"])}</div>'
                if cached.get("_run_at") else ""
            )

            # Luna (GPT-5.6) second opinion on whether to open this position —
            # SELL/WAIT vocabulary, not ROLL/HOLD/ASSIGNMENT (no existing
            # position to manage). Comparison only. See openai_advisor.py.
            _main_action = "HOLD" if (cached.get("_do_nothing") or cached.get("recommendation") == "HOLD") else "SELL"
            openai_rec    = cached.get("openai_recommendation")
            openai_text   = cached.get("openai_text")
            openai_error  = cached.get("openai_error")
            openai_run_at = cached.get("openai_run_at")
            if openai_rec:
                _ai2_cls = "ok" if openai_rec == "SELL" else "warn"
                _ai2_matches = (openai_rec == "SELL") == (_main_action == "SELL")
                if _ai2_matches:
                    _agree_tag = '<span style="color:var(--ok);font-size:11px;margin-left:6px">&#10003; agrees</span>'
                else:
                    _agree_tag = '<span style="color:var(--warn);font-size:11px;margin-left:6px">&#9888; disagrees</span>'
                openai_badge_html = (
                    f'<span class="badge badge-{_ai2_cls}" style="font-size:11px;padding:3px 10px;margin-left:8px" '
                    f'title="Luna (GPT-5.6) second opinion">Luna: {html_mod.escape(openai_rec)}</span>{_agree_tag}'
                )
            else:
                openai_badge_html = (
                    '<button onclick="openaiCompareUnborn(this)" '
                    'style="font-size:11px;padding:3px 10px;margin-left:8px;background:#10a37f;color:#fff;'
                    'border:none;border-radius:4px;cursor:pointer">Compare with Luna</button>'
                )
            if openai_text:
                _ai2_body = _render_advisor_markdown(openai_text)
                _ai2_updated = (
                    f'&nbsp;&nbsp;<span style="font-weight:normal;color:var(--muted);font-size:11px">'
                    f'Updated {html_mod.escape(openai_run_at)}</span>' if openai_run_at else ""
                )
                openai_card_html = (
                    f'<div id="openai-card" class="chain-pnl" style="margin-top:14px;border-color:#10a37f">'
                    f'<span class="chain-label" style="color:#10a37f">Luna’s Take (GPT-5.6, second opinion){_ai2_updated}</span>'
                    f'<span class="chain-working" style="white-space:normal;line-height:1.6;display:block;margin-top:6px">{_ai2_body}</span>'
                    f'</div>'
                )
            elif openai_error:
                openai_card_html = (
                    f'<div id="openai-card" class="chain-pnl" style="margin-top:14px;border-color:#10a37f">'
                    f'<span class="chain-label" style="color:#10a37f">Luna’s Take</span>'
                    f'<span class="chain-working" style="color:var(--danger);display:block;margin-top:6px">Error: {html_mod.escape(openai_error)}</span>'
                    f'</div>'
                )
            else:
                openai_card_html = '<div id="openai-card"></div>'

            primary_ask_html = (
                '<div class="ask-section">'
                '<div class="ask-thread" id="ask-thread"></div>'
                '<div class="ask-input">'
                '<textarea id="ask-q" rows="3" placeholder="Ask Claude a follow-up question…"></textarea>'
                '<div class="ask-input-row">'
                '<button onclick="submitAsk()">Ask</button>'
                '<div id="ask-spinner"></div>'
                '</div></div></div>'
            )
            openai_ask_html = (
                '<div class="ask-section" style="margin-top:20px">'
                '<div class="ask-thread" id="openai-ask-thread"></div>'
                '<div class="ask-input">'
                '<textarea id="openai-ask-q" rows="3" placeholder="Ask Luna a follow-up question…"></textarea>'
                '<div class="ask-input-row">'
                '<button onclick="submitOpenaiAskUnborn()" style="background:#10a37f;border-color:#10a37f">Ask Luna</button>'
                '<div id="openai-ask-spinner"></div>'
                '</div></div></div>'
            )

            body_html = (
                f'<div style="margin-bottom:16px">'
                f'<span class="badge badge-{rec_cls}" style="font-size:15px;padding:6px 16px">{html_mod.escape(rec)}</span>'
                f'<span id="openai-badge-slot">{openai_badge_html}</span>'
                f'{run_at_html}'
                f'</div>'
                f'<div class="rec-body">{"".join(lines_out)}</div>'
                f'{primary_ask_html}'
                f'{openai_card_html}'
                f'{openai_ask_html}'
            )

        _main_action_js = None
        if cached and not cached.get("error"):
            _main_action_js = "HOLD" if (cached.get("_do_nothing") or cached.get("recommendation") == "HOLD") else "SELL"

        page = f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title_str} — Unborn</title>
<script>
(function(){{
  var c=document.createElement('canvas');
  c.width=c.height=64;
  var x=c.getContext('2d');
  x.fillStyle='white';
  x.beginPath();x.arc(32,32,30,0,Math.PI*2);x.fill();
  x.fillStyle='#052e16';
  x.textAlign='center';x.textBaseline='middle';
  x.font='bold {_fs}px monospace';
  x.fillText('{ticker_sym}',32,32);
  var l=document.querySelector("link[rel~='icon']")||document.createElement('link');
  l.rel='icon';l.href=c.toDataURL();
  document.head.appendChild(l);
}})();
</script>
<style>
  :root{{--bg:#0f1117;--surface:#1a1d27;--border:#2a2d3a;--text:#e2e8f0;--muted:#8892a4;
    --accent:#4f8ef7;--ok:#22c55e;--warn:#f59e0b;--danger:#ef4444;
    --ok-bg:#052e16;--warn-bg:#451a03;--danger-bg:#450a0a}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:'SF Mono','Fira Code',monospace;font-size:13px;line-height:1.6;padding:24px}}
  header{{display:flex;align-items:center;gap:16px;margin-bottom:24px;flex-wrap:wrap}}
  header h1{{font-size:15px;font-weight:600;color:var(--accent)}}
  .pos-label{{color:var(--muted);font-size:12px}}
  a.back{{color:var(--accent);text-decoration:none;font-size:12px}}
  a.back:hover{{text-decoration:underline}}
  .badge{{display:inline-block;border-radius:4px;padding:2px 7px;font-size:11px;font-weight:600;letter-spacing:.05em}}
  .badge-ok{{background:var(--ok-bg);color:var(--ok)}}
  .badge-hold{{background:#0c1a2e;color:var(--accent);border:1px solid var(--accent)}}
  .badge-warn{{background:var(--warn-bg);color:var(--warn)}}
  .badge-danger{{background:var(--danger-bg);color:var(--danger)}}
  .rec-body h1,.rec-body h2{{color:var(--accent);margin:16px 0 6px;font-size:14px}}
  .rec-body p{{margin:4px 0;max-width:860px}}
  .rec-body li{{margin:2px 0 2px 20px;max-width:860px}}
  .rec-body strong{{color:var(--text)}}
  .rec-body br{{display:block;margin:6px 0;content:""}}
  .chain-pnl{{margin-top:28px;padding:14px 18px;background:var(--surface);border:1px solid var(--border);border-radius:8px;max-width:860px}}
  .chain-label{{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:6px}}
  .chain-working{{font-size:13px;color:var(--text)}}
  .ask-section{{margin-top:36px;max-width:860px}}
  .ask-thread{{margin-bottom:12px;display:flex;flex-direction:column;gap:12px}}
  .ask-q{{background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:10px 14px;font-size:12px;color:var(--muted)}}
  .ask-q::before{{content:'You: ';color:var(--accent);font-weight:600}}
  .ask-a{{background:#12151f;border:1px solid var(--border);border-radius:6px;padding:10px 14px;font-size:12px;color:var(--text);white-space:pre-wrap;line-height:1.6}}
  .ask-a::before{{content:'Claude: ';color:var(--ok);font-weight:600}}
  .ask-a.openai-ask-a::before{{content:'Luna: ';color:#10a37f}}
  .ask-a.err{{color:var(--danger)}}
  .ask-input{{display:flex;flex-direction:column;gap:8px}}
  .ask-input textarea{{background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:10px 12px;font-size:12px;font-family:inherit;resize:vertical;min-height:64px;outline:none}}
  .ask-input textarea:focus{{border-color:var(--accent)}}
  .ask-input-row{{display:flex;gap:8px;align-items:center}}
  .ask-input-row button{{padding:6px 16px}}
  #ask-spinner{{display:none;width:16px;height:16px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin 0.7s linear infinite;flex-shrink:0}}
  @keyframes spin{{to{{transform:rotate(360deg)}}}}
</style></head><body>
<header>
  <a class="back" href="/" onclick="window.close();return false;">&#8592; Back</a>
  <h1>&#9660; Unborn Analysis</h1>
  <span class="pos-label">{title_str}</span>
</header>
{body_html}
<script>
const _POS_KEY = {json.dumps(ub_key)};
const _MAIN_ACTION = {json.dumps(_main_action_js)};
const _OPENAI_SAVED_QA = {json.dumps(cached.get("openai_qa_thread", []) if cached else [])};
function esc(s) {{
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}
function _advisorInline(s) {{
  s = esc(s);
  s = s.replace(/\\*\\*(.+?)\\*\\*/g, '<strong>$1</strong>');
  s = s.replace(/\\*(.+?)\\*/g, '<em>$1</em>');
  return s;
}}
function renderAdvisorMarkdown(text) {{
  const blocks = [];
  let bulletBuf = [];
  const flushBullets = () => {{
    if (bulletBuf.length) {{ blocks.push('<ul style="margin:4px 0 4px 20px">' + bulletBuf.join('') + '</ul>'); bulletBuf = []; }}
  }};
  for (const raw of text.split('\\n')) {{
    const line = raw.trim();
    if (!line) {{ flushBullets(); continue; }}
    if (/^-{{3,}}$|^\\*{{3,}}$/.test(line)) {{
      flushBullets();
      blocks.push('<hr style="border:none;border-top:1px solid var(--border);margin:10px 0">');
      continue;
    }}
    const headingM = line.match(/^(#{{1,4}})\\s+(.*)/);
    if (headingM) {{
      flushBullets();
      blocks.push(`<p style="margin-top:10px;font-weight:600;color:var(--accent)">${{_advisorInline(headingM[2])}}</p>`);
      continue;
    }}
    if (line.startsWith('- ') || line.startsWith('• ')) {{
      bulletBuf.push(`<li>${{_advisorInline(line.slice(2))}}</li>`);
      continue;
    }}
    flushBullets();
    blocks.push(`<p style="margin-bottom:10px">${{_advisorInline(line)}}</p>`);
  }}
  flushBullets();
  return blocks.join('');
}}
async function openaiCompareUnborn(btn) {{
  if (btn) {{ btn.disabled = true; btn.textContent = 'Comparing…'; }}
  try {{
    const r = await fetch('/api/openai-compare-unborn', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ub_key: _POS_KEY}})
    }});
    const d = await r.json();
    const slot = document.getElementById('openai-badge-slot');
    const card = document.getElementById('openai-card');
    if (d.error) {{
      if (slot) slot.innerHTML = `<span style="color:var(--danger);font-size:11px;margin-left:8px">Luna error: ${{esc(d.error)}}</span>`;
      return;
    }}
    if (slot) {{
      const cls = d.recommendation === 'SELL' ? 'ok' : 'warn';
      const matches = _MAIN_ACTION && ((d.recommendation === 'SELL') === (_MAIN_ACTION === 'SELL'));
      const agreeTag = !_MAIN_ACTION ? '' : matches
        ? '<span style="color:var(--ok);font-size:11px;margin-left:6px">&#10003; agrees</span>'
        : '<span style="color:var(--warn);font-size:11px;margin-left:6px">&#9888; disagrees</span>';
      slot.innerHTML = `<span class="badge badge-${{cls}}" style="font-size:11px;padding:3px 10px;margin-left:8px" title="Luna (GPT-5.6) second opinion">Luna: ${{esc(d.recommendation||'?')}}</span>${{agreeTag}}`;
    }}
    if (card) {{
      const body = renderAdvisorMarkdown(d.text || '');
      card.outerHTML = `<div id="openai-card" class="chain-pnl" style="margin-top:14px;border-color:#10a37f">`
        + `<span class="chain-label" style="color:#10a37f">Luna’s Take (GPT-5.6, second opinion)</span>`
        + `<span class="chain-working" style="white-space:normal;line-height:1.6;display:block;margin-top:6px">${{body}}</span>`
        + `</div>`;
    }}
  }} catch(e) {{
    const slot = document.getElementById('openai-badge-slot');
    if (slot) slot.innerHTML = `<span style="color:var(--danger);font-size:11px;margin-left:8px">Luna error: ${{esc(e.message)}}</span>`;
  }} finally {{
    if (btn) {{ btn.disabled = false; btn.textContent = 'Compare with Luna'; }}
  }}
}}
const _SAVED_QA = {json.dumps(cached.get("qa_thread", []) if cached else [])};
(function() {{
  const thread = document.getElementById('ask-thread');
  for (const item of _SAVED_QA) {{
    const qEl = document.createElement('div'); qEl.className='ask-q'; qEl.textContent=item.q; thread.appendChild(qEl);
    const aEl = document.createElement('div'); aEl.className='ask-a'; aEl.innerHTML=renderAdvisorMarkdown(item.a); thread.appendChild(aEl);
  }}
  const openaiThread = document.getElementById('openai-ask-thread');
  for (const item of _OPENAI_SAVED_QA) {{
    const qEl = document.createElement('div'); qEl.className='ask-q'; qEl.textContent=item.q; openaiThread.appendChild(qEl);
    const aEl = document.createElement('div'); aEl.className='ask-a openai-ask-a'; aEl.innerHTML=renderAdvisorMarkdown(item.a); openaiThread.appendChild(aEl);
  }}
}})();
async function submitAsk() {{
  const ta = document.getElementById('ask-q');
  const q = ta.value.trim();
  if (!q) return;
  const thread = document.getElementById('ask-thread');
  const qEl = document.createElement('div'); qEl.className='ask-q'; qEl.textContent=q; thread.appendChild(qEl);
  ta.value = '';
  document.getElementById('ask-spinner').style.display = 'block';
  const aEl = document.createElement('div'); aEl.className='ask-a'; aEl.textContent='…'; thread.appendChild(aEl);
  aEl.scrollIntoView({{behavior:'smooth'}});
  try {{
    const r = await fetch('/api/ask', {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{position_key: _POS_KEY, question: q}})
    }});
    const d = await r.json();
    if (d.error) {{ aEl.className='ask-a err'; aEl.textContent=d.error; }}
    else {{ aEl.innerHTML=renderAdvisorMarkdown(d.answer || ''); }}
  }} catch(e) {{ aEl.className='ask-a err'; aEl.textContent=e.message; }}
  finally {{
    document.getElementById('ask-spinner').style.display='none';
    aEl.scrollIntoView({{behavior:'smooth'}});
  }}
}}
document.getElementById('ask-q').addEventListener('keydown', e => {{
  if (e.key==='Enter' && !e.shiftKey) {{ e.preventDefault(); submitAsk(); }}
}});
async function submitOpenaiAskUnborn() {{
  const ta = document.getElementById('openai-ask-q');
  const q = ta.value.trim();
  if (!q) return;
  const thread = document.getElementById('openai-ask-thread');
  const qEl = document.createElement('div'); qEl.className='ask-q'; qEl.textContent=q; thread.appendChild(qEl);
  ta.value = '';
  document.getElementById('openai-ask-spinner').style.display = 'block';
  const aEl = document.createElement('div'); aEl.className='ask-a openai-ask-a'; aEl.textContent='…'; thread.appendChild(aEl);
  aEl.scrollIntoView({{behavior:'smooth'}});
  try {{
    const r = await fetch('/api/openai-ask-unborn', {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{ub_key: _POS_KEY, question: q}})
    }});
    const d = await r.json();
    if (d.error) {{ aEl.className='ask-a openai-ask-a err'; aEl.textContent=d.error; }}
    else {{ aEl.innerHTML=renderAdvisorMarkdown(d.answer || ''); }}
  }} catch(e) {{ aEl.className='ask-a openai-ask-a err'; aEl.textContent=e.message; }}
  finally {{
    document.getElementById('openai-ask-spinner').style.display='none';
    aEl.scrollIntoView({{behavior:'smooth'}});
  }}
}}
document.getElementById('openai-ask-q').addEventListener('keydown', e => {{
  if (e.key==='Enter' && !e.shiftKey) {{ e.preventDefault(); submitOpenaiAskUnborn(); }}
}});
</script>
</body></html>"""
        return Response(page, mimetype="text/html", headers={"Cache-Control": "no-store"})

    @app.route("/favicon/<ticker>.svg")
    @require_auth
    def ticker_favicon(ticker):
        t   = html_mod.escape(ticker.upper()[:6])
        fs  = {1: 36, 2: 32, 3: 24, 4: 18}.get(len(t), 14)
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
            f'<circle cx="32" cy="32" r="30" fill="white"/>'
            f'<text x="32" y="32" dominant-baseline="central" text-anchor="middle" '
            f'font-family="monospace" font-weight="bold" font-size="{fs}" fill="#052e16">{t}</text>'
            f'</svg>'
        )
        return Response(svg, mimetype="image/svg+xml",
                        headers={"Cache-Control": "no-store"})

    @app.route("/api/ask", methods=["POST"])
    @require_auth
    def api_ask():
        """
        Follow-up question against the primary Claude analysis (existing
        position or unborn/new-position decision) — replays the original
        cached context + Claude's own first answer + prior follow-ups via
        claude_advisor, so it has real conversational continuity and reuses
        the same cached CORE/weekly/Fed prefix as the original call. Checks
        both caches since this endpoint is shared by both page types.
        """
        body     = flask_request.get_json(force=True, silent=True) or {}
        pos_key  = body.get("position_key", "")
        question = (body.get("question") or "").strip()
        if not pos_key or not question:
            return Response(json.dumps({"error": "Missing position_key or question"}),
                            status=400, mimetype="application/json")

        with _cache_lock:
            cached = _analysis_cache.get(pos_key)
            _in_unborn = False
            if cached is None:
                cached = _unborn_cache.get(pos_key)
                _in_unborn = cached is not None
        if not cached or not cached.get("text"):
            return Response(json.dumps({"error": "No analysis yet for this position — click Analyze first."}),
                            status=404, mimetype="application/json")

        qa_thread = cached.get("qa_thread") or []
        ask_fn = claude_advisor.ask_unborn_followup if _in_unborn else claude_advisor.ask_position_followup
        try:
            result = ask_fn(cached.get("tail_text") or "", cached["text"], qa_thread, question)
        except Exception as exc:
            log.exception("[ask] failed for %s", pos_key)
            result = {"error": str(exc), "answer": ""}

        if not result.get("error"):
            answer = result["answer"]
            log.info("[ask] pos_key=%s q=%s… answer_len=%d", pos_key, question[:40], len(answer))
            # Persist Q&A in the analysis cache so it survives page reloads
            with _cache_lock:
                if _in_unborn:
                    entry = _unborn_cache.setdefault(pos_key, {})
                    qa = entry.setdefault("qa_thread", [])
                    qa.append({"q": question, "a": answer})
                    _save_unborn_cache(_unborn_cache)
                else:
                    entry = _analysis_cache.setdefault(pos_key, {})
                    qa = entry.setdefault("qa_thread", [])
                    qa.append({"q": question, "a": answer})
                    _save_cache(_analysis_cache)

        return Response(json.dumps(_sanitize(result), default=_serial), mimetype="application/json")

    @app.route("/api/reset-cache-entry", methods=["POST"])
    @require_auth
    def api_reset_cache_entry():
        body    = flask_request.get_json(force=True, silent=True) or {}
        pos_key = body.get("position_key", "")
        with _cache_lock:
            _analysis_cache.pop(pos_key, None)
            _save_cache(_analysis_cache)
        log.info("[reset-entry] cleared cache for %s", pos_key)
        return Response(json.dumps({"status": "ok"}), mimetype="application/json")

    @app.route("/api/unborn-cache-delete", methods=["POST"])
    @require_auth
    def api_unborn_cache_delete():
        body    = flask_request.get_json(force=True, silent=True) or {}
        row_key = body.get("row_key", "")   # e.g. "SLB|CC"
        # row_key is TICKER|STRAT; ub_key in cache is TICKER|STRAT|QTY — match by prefix
        with _cache_lock:
            to_delete = [k for k in _unborn_cache if k.startswith(row_key)]
            for k in to_delete:
                del _unborn_cache[k]
            if to_delete:
                _save_unborn_cache(_unborn_cache)
        log.info("[unborn-cache-delete] removed %d entries for %s", len(to_delete), row_key)
        return Response(json.dumps({"deleted": to_delete}), mimetype="application/json")

    @app.route("/api/reset-cache", methods=["POST"])
    @require_auth
    def api_reset_cache():
        with _cache_lock:
            _analysis_cache.clear()
            _save_cache(_analysis_cache)
        log.info("[reset] analysis cache cleared")
        return Response(json.dumps({"status": "ok"}), mimetype="application/json")

    @app.route("/api/ensure-fed-source", methods=["POST"])
    @require_auth
    def api_ensure_fed_source():
        """Self-heals the NB notebook's next-month Fed calendar source on
        month rollover. Cheap no-op most days — meant to be hit periodically
        (scheduled_refresh.py) so it doesn't depend on the dashboard process
        staying up across a month boundary."""
        notebook_id = os.environ.get("NOTEBOOKLM_NOTEBOOK_ID")
        if not notebook_id:
            return Response(json.dumps({"error": "NOTEBOOKLM_NOTEBOOK_ID not set"}), status=500, mimetype="application/json")
        try:
            from notebooklm import NotebookLMClient
            async def _run():
                async with NotebookLMClient.from_storage() as client:
                    await _ensure_fed_calendar_source(client, notebook_id)
            asyncio.run(_run())
        except Exception as exc:
            log.warning("[fed-calendar] ensure-source failed: %s", exc)
            return Response(json.dumps({"error": str(exc)}), status=500, mimetype="application/json")
        return Response(json.dumps({"status": "ok"}), mimetype="application/json")

    @app.route("/api/former-positions", methods=["GET"])
    @require_auth
    def api_former_positions():
        """Tickers with past positions but no current open position, for the Former Positions table."""
        import sqlite3 as _sq
        _db = os.path.normpath(JOURNAL_DB)
        try:
            con = _sq.connect(f"file:{_db}?mode=ro", uri=True)
            con.row_factory = _sq.Row
            open_tickers = {r["symbol"].upper() for r in con.execute(
                "SELECT DISTINCT symbol FROM positions WHERE status='open'"
            ).fetchall()}
            rows = con.execute("""
                SELECT p.symbol, p.option_type, p.ul_cost_basis,
                       ABS(SUM(CASE WHEN t.action='sell' THEN t.quantity ELSE 0 END)) AS qty
                FROM positions p
                LEFT JOIN trades t ON t.position_id = p.id AND t.is_test = 0
                WHERE p.status != 'open'
                  AND p.id IN (
                      SELECT MAX(p2.id) FROM positions p2
                      WHERE p2.symbol = p.symbol AND p2.status != 'open'
                      GROUP BY p2.symbol
                  )
                GROUP BY p.symbol
                ORDER BY p.symbol
            """).fetchall()
            con.close()
            result = []
            for r in rows:
                sym = r["symbol"].upper()
                if sym in open_tickers:
                    continue
                opt = (r["option_type"] or "call").lower()
                try:
                    cb = float(r["ul_cost_basis"] or 0)
                except (TypeError, ValueError):
                    cb = 0.0
                try:
                    kd = get_key_dates(sym)
                    earnings_date = kd.get("earnings_date")
                    earnings_source = kd.get("earnings_source")
                except Exception:
                    earnings_date, earnings_source = "Unknown", "unknown"
                result.append({
                    "symbol": sym,
                    "strat": "CSP" if opt == "put" else "CC",
                    "ul_cost_basis": cb,
                    "qty": int(r["qty"] or 1),
                    "ul_price": get_underlying_price(sym),
                    "earnings_date": earnings_date,
                    "earnings_source": earnings_source,
                })
            return Response(json.dumps(_sanitize(result), default=_serial), mimetype="application/json")
        except Exception as exc:
            log.error("[former-positions] %s", exc)
            return Response(json.dumps([]), mimetype="application/json")

    @app.route("/api/analysis-updates", methods=["GET"])
    @require_auth
    def api_analysis_updates():
        """Lightweight cache snapshot — no DB query. Used by the browser to pick up
        scheduled-refresh results in real time without a full page reload."""
        with _cache_lock:
            updates = {
                k: {
                    "rec":        v.get("recommendation"),
                    "run_at":     v.get("_run_at"),
                    "chain_cash": v.get("chain_cash"),
                    "text":       v.get("text"),
                }
                for k, v in _analysis_cache.items()
                if not v.get("error") and v.get("recommendation")
            }
            inflight = list(_analysis_inflight)
            unborn_inflight = list({
                "|".join(k.split("|")[:2]) for k in _unborn_inflight
            })
        return Response(json.dumps({"updates": updates, "inflight": inflight, "unborn_inflight": unborn_inflight}), mimetype="application/json")

    @app.route("/analyze/<path:pos_key>")
    @require_auth
    def analyze_detail(pos_key: str):
        pos_key = urllib.parse.unquote(pos_key)
        cached  = _analysis_cache.get(pos_key)

        parts = pos_key.split("|")
        title_str = " · ".join(parts) if parts else pos_key
        ticker_sym = html_mod.escape(parts[0].upper()) if parts else "?"
        _fs = {1: 36, 2: 32, 3: 24, 4: 18}.get(len(parts[0]) if parts else 1, 14)

        if not cached:
            body_html = (
                "<p style='color:var(--muted)'>Analysis not found. "
                "Click <b>Analyze</b> on the dashboard first.</p>"
            )
            rec = ""
        elif cached.get("error"):
            body_html = f"<p style='color:var(--danger)'>Error: {html_mod.escape(cached['error'])}</p>"
            rec = ""
        else:
            rec  = cached.get("recommendation", "")
            text = cached.get("text", "")
            rec_cls = {"ROLL": "warn", "ASSIGNMENT": "danger", "HOLD": "hold"}.get(rec, "ok")
            # Convert markdown-ish text to simple HTML
            def _md_cell(c: str) -> str:
                return re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', html_mod.escape(c))

            lines_out = []
            text_lines = text.splitlines()
            i = 0
            while i < len(text_lines):
                ln = text_lines[i]
                # Detect markdown table block (line starts with |)
                if ln.strip().startswith('|') and ln.strip().count('|') >= 2:
                    tbl_lines = []
                    while i < len(text_lines):
                        stripped = text_lines[i].strip()
                        if stripped.startswith('|'):
                            tbl_lines.append(text_lines[i])
                            i += 1
                        elif stripped == '':
                            i += 1  # skip blank lines between table rows
                        else:
                            break
                    header, body_rows = None, []
                    for tl in tbl_lines:
                        cells = [c.strip() for c in tl.strip().strip('|').split('|')]
                        if all(re.match(r'^:?-+:?$', c) for c in cells if c):
                            continue  # separator row
                        if header is None:
                            header = cells
                        else:
                            body_rows.append(cells)
                    if header:
                        ths = ''.join(f'<th>{_md_cell(c)}</th>' for c in header)
                        trs = ''.join(
                            '<tr>' + ''.join(f'<td>{_md_cell(c)}</td>' for c in row) + '</tr>'
                            for row in body_rows
                        )
                        lines_out.append(f'<table class="atbl"><thead><tr>{ths}</tr></thead><tbody>{trs}</tbody></table>')
                    continue
                ln_esc = html_mod.escape(ln)
                if ln_esc.startswith("### "):
                    lines_out.append(f"<h3>{re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', ln_esc[4:])}</h3>")
                elif ln_esc.startswith("## "):
                    lines_out.append(f"<h2>{ln_esc[3:]}</h2>")
                elif ln_esc.startswith("# "):
                    lines_out.append(f"<h1>{ln_esc[2:]}</h1>")
                elif ln_esc.startswith("**") and ln_esc.endswith("**"):
                    lines_out.append(f"<p><strong>{ln_esc[2:-2]}</strong></p>")
                elif ln_esc.startswith("- ") or ln_esc.startswith("• "):
                    lines_out.append(f"<li>{ln_esc[2:]}</li>")
                elif ln_esc.strip() == "":
                    lines_out.append("<br>")
                else:
                    ln_esc = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', ln_esc)
                    lines_out.append(f"<p>{ln_esc}</p>")
                i += 1
            # Net Chain Cash PnL footer — recomputed live from the DB (not the frozen
            # analysis-cache snapshot) so test trades added after the analysis ran
            # (the normal workflow: analyze, then model a prospective roll via test
            # trades) show up immediately without re-running the LLM analysis.
            chain_cash = sim_chain_cash = collected = paid = n_pos = None
            has_test_trade = False
            pos_avg_price = pos_qty = None
            decay_info = None
            if len(parts) >= 4:
                pk_sym, pk_type = parts[0].upper(), parts[1].upper()
                try:
                    pk_strike = float(parts[2])
                except ValueError:
                    pk_strike = None
                pk_expiry = parts[3]
                for _p in get_all_open_positions(pk_sym):
                    try:
                        _p_strike = float(_p.get("strike", ""))
                    except (TypeError, ValueError):
                        _p_strike = None
                    if (str(_p.get("option_type", "")).upper() == pk_type
                            and _p_strike == pk_strike
                            and str(_p.get("expiry", "")) == pk_expiry):
                        _chain = get_chain_net_cash(_p.get("spread_id"), fallback_pos_id=_p.get("id"))
                        chain_cash     = _chain["net_cash"]
                        sim_chain_cash = _chain["sim_net_cash"]
                        has_test_trade = _chain["has_test"]
                        collected      = _chain["collected"]
                        paid           = _chain["paid"]
                        n_pos          = _chain["num_positions"]
                        pos_avg_price  = _p.get("avg_price")
                        pos_qty        = abs(_p.get("net_qty") or 0)

                        # Decay-quality decomposition — one live quote+greeks
                        # fetch for this single contract (analyze page only,
                        # not the main table refresh, so the extra call is fine).
                        def _f2(v):
                            try:
                                return float(v) if v not in (None, "") else None
                            except (TypeError, ValueError):
                                return None
                        try:
                            _tok = _get_valid_token()
                            _osi = build_osi_symbol(pk_sym, pk_expiry, pk_type, pk_strike).replace(" ", "")
                            _quotes = get_option_quotes(_tok, account_id, [
                                {"symbol": pk_sym, "expiry": pk_expiry, "option_type": pk_type, "strike": pk_strike}
                            ])
                            _q = _quotes.get(_osi, {})
                            _bid, _ask, _last = _f2(_q.get("bid")), _f2(_q.get("ask")), _f2(_q.get("last"))
                            _cur_price = (_bid + _ask) / 2 if _bid is not None and _ask is not None else _last
                            _g = get_option_greeks_batch(_tok, account_id, [_osi]).get(_osi, {})
                            _underlying = get_underlying_price(pk_sym)
                            try:
                                _dte = (datetime.date.fromisoformat(pk_expiry) - datetime.date.today()).days
                            except ValueError:
                                _dte = None
                            _delta_val = _f2(_g.get("delta"))
                            _pct_pnl = None
                            _net_qty = _p.get("net_qty") or 0
                            if _cur_price is not None and pos_avg_price is not None and pos_avg_price != 0:
                                _abs_pnl = (
                                    (pos_avg_price - _cur_price) if _net_qty > 0 else (_cur_price + pos_avg_price)
                                ) * 100 * pos_qty
                                _pct_pnl = _abs_pnl / (abs(pos_avg_price) * 100 * pos_qty) * 100
                            decay_info = compute_decay_signals(
                                _p, _dte, _underlying, _cur_price,
                                _delta_val, _f2(_g.get("gamma")),
                                _f2(_g.get("theta")), _f2(_g.get("vega")),
                                _f2(_g.get("impliedVolatility")),
                                _pct_pnl,
                            )
                        except Exception as _decay_exc:
                            log.warning("[analyze decay] failed for %s: %s", pos_key, _decay_exc)
                        break

            if chain_cash is not None and collected is not None and paid is not None:
                pos_lbl = f"{n_pos} position{'s' if n_pos != 1 else ''}" if n_pos else "positions"

                has_pos = pos_avg_price is not None and pos_qty is not None and pos_qty > 0

                def _fmt_pnl(val):
                    sign = "+" if val >= 0 else ""
                    clr  = "var(--ok)" if val >= 0 else "var(--danger)"
                    return f"<strong style='color:{clr}'>{sign}${val:,.2f}</strong>"

                base = f"${collected:,.2f} collected − ${paid:,.2f} paid"

                if has_test_trade and sim_chain_cash is not None:
                    # Test trade present: sim_chain_cash is the locked-in chain PnL —
                    # this is the deterministic "birth to death" figure (STO, test BTC
                    # of the old leg, test STO of the new leg, test BTC of the new leg),
                    # fees and commissions included, matching the journal exactly.
                    working = (
                        f"{base} (+ test close) = {_fmt_pnl(round(sim_chain_cash, 2))}"
                        f"&nbsp;<span style='color:var(--muted);font-size:11px'>[simulated close]</span>"
                    )
                    exp_note = ""

                elif (rec == "ROLL" and has_pos and cached.get("sto_chain_price") is not None
                      and cached.get("btc_chain_price") is not None):
                    # No test trade entered yet — project the FULL chain if the
                    # suggested roll is executed and the new leg is later closed at
                    # 40% of its own premium (60% profit capture): net chain cash to
                    # date, minus the real cost to BTC the current leg (at its
                    # confirmed chain price), plus the new leg's STO credit, minus
                    # the cost to close that new leg at 40%. This is what tells you
                    # whether the ENTIRE roll chain is profitable, not just this leg.
                    # Commission/fees on these three legs are estimated via the
                    # journal's own fee schedule (see run_roll_for_position) so this
                    # lines up with what the journal will show once the trades are
                    # actually entered — not just the gross, fee-free math.
                    _btc_p  = cached.get("btc_chain_price")
                    _sto_p  = cached["sto_chain_price"]
                    _btc_cost   = cached.get("roll_btc_cost")
                    _sto_credit = cached.get("roll_sto_credit")
                    _close_cost = cached.get("roll_close_price")
                    _close_amt  = cached.get("roll_close_cost")
                    if _btc_cost is None or _sto_credit is None or _close_amt is None:
                        # Older cached analysis, predates fee estimation — fall back
                        # to the gross (fee-free) figures rather than error out.
                        _btc_cost   = round((_btc_p or 0.0) * 100 * pos_qty, 2)
                        _sto_credit = round(_sto_p * 100 * pos_qty, 2)
                        _close_cost = round(_sto_p * 0.40, 4)
                        _close_amt  = round(_close_cost * 100 * pos_qty, 2)
                    final_pnl = round(chain_cash - _btc_cost + _sto_credit - _close_amt, 2)
                    _sto_lbl = html_mod.escape(cached.get("sto_chain_desc") or "new leg")
                    working = (
                        f"{base} "
                        f"− ${_btc_cost:,.2f} BTC current leg @ ${_btc_p:.2f} (incl. est. comm/fees) "
                        f"+ ${_sto_credit:,.2f} STO {_sto_lbl} @ ${_sto_p:.2f} (incl. est. comm/fees) "
                        f"− ${_close_amt:,.2f} close new leg @ ${_close_cost:.2f} (40%, incl. est. comm/fees) "
                        f"= {_fmt_pnl(final_pnl)}"
                    )
                    exp_note = ""

                elif rec == "ROLL" and has_pos:
                    # New leg's price couldn't be confirmed against the chain that
                    # was fetched (e.g. its expiry fell outside the fetch window) —
                    # show only the realized-to-date cash rather than guess.
                    working = (
                        f"{base} = {_fmt_pnl(round(chain_cash, 2))} "
                        f"<span style='color:var(--muted);font-size:11px'>"
                        f"[new leg price unconfirmed — enter test trades to project the roll]</span>"
                    )
                    exp_note = ""

                elif rec in ("HOLD", "ASSIGNMENT") and has_pos:
                    # HOLD / ASSIGNMENT: show two scenarios
                    buy_back_cost  = round(pos_avg_price * 0.40 * 100 * pos_qty, 2)
                    pnl_worthless  = round(chain_cash, 2)
                    pnl_60pct      = round(chain_cash - buy_back_cost, 2)
                    working = (
                        f"{base}<br>"
                        f"&nbsp;&nbsp;If expires worthless: {_fmt_pnl(pnl_worthless)}<br>"
                        f"&nbsp;&nbsp;If closed at 60% profit: − ${buy_back_cost:,.2f} close at 40% "
                        f"(${pos_avg_price:.2f} × 0.40 × 100 × {pos_qty} contracts) "
                        f"= {_fmt_pnl(pnl_60pct)}"
                    )
                    exp_note = ""

                else:
                    # No position data — show raw chain cash
                    working  = f"{base} = {_fmt_pnl(round(chain_cash, 2))}"
                    exp_note = ""

                chain_html = (
                    f'<div class="chain-pnl">'
                    f'<span class="chain-label">Net Chain Cash PnL &nbsp;<span style="font-weight:normal;color:var(--muted)">({pos_lbl})</span></span>'
                    f'<span class="chain-working">Working: {working}{html_mod.escape(exp_note)}</span>'
                    + f'</div>'
                )
            else:
                chain_html = ""

            # Confirmed BTC/STO prices from the exact chain snapshot NotebookLM was
            # given — shown next to the Execution Instructions so the Action 1/2
            # prices can be checked against real data instead of the LLM's estimate.
            btc_chain_price = cached.get("btc_chain_price")
            sto_chain_price = cached.get("sto_chain_price")
            sto_chain_desc  = cached.get("sto_chain_desc")
            if rec == "ROLL" and (btc_chain_price is not None or sto_chain_price is not None):
                _price_parts = []
                if btc_chain_price is not None:
                    _btc_desc = (
                        f"{ticker_sym} {html_mod.escape(str(parts[2]))} "
                        f"{html_mod.escape(parts[1].upper())} exp {html_mod.escape(parts[3])}"
                    ) if len(parts) >= 4 else ticker_sym
                    _price_parts.append(f"BTC leg ({_btc_desc}) @ ${btc_chain_price:.2f}")
                if sto_chain_price is not None:
                    _sto_desc = html_mod.escape(sto_chain_desc) if sto_chain_desc else "new leg"
                    _price_parts.append(f"STO leg ({_sto_desc}) @ ${sto_chain_price:.2f}")
                confirmed_html = (
                    f'<div class="chain-pnl" style="margin-top:14px">'
                    f'<span class="chain-label">Confirmed Prices &nbsp;'
                    f'<span style="font-weight:normal;color:var(--muted)">(from the chain used for this analysis)</span></span>'
                    f'<span class="chain-working">{" &nbsp;|&nbsp; ".join(_price_parts)}</span>'
                    f'</div>'
                )
            else:
                confirmed_html = ""

            # Decay-quality block: replaces the flat 60%-profit rule of thumb with
            # a Greeks-based decomposition of the price move since entry. Live
            # (recomputed on every view, like the chain-PnL footer above), and
            # gracefully "--" for any component that needs entry-snapshot data
            # this position doesn't have.
            def _dfmt(v, digits=3):
                return "—" if v is None else f"{'+' if v >= 0 else ''}${v:.{digits}f}"

            if decay_info is None:
                decay_html = ""
            else:
                d = decay_info
                if d.get("gamma_risk"):
                    _dg_str  = f'${d["dollar_gamma"]:.0f}' if d.get("dollar_gamma") is not None else "unknown"
                    _dte_str = _dte if _dte is not None else "?"
                    _ad_str  = f'{abs(_delta_val):.3f}' if _delta_val is not None else "?"
                    gamma_note = (
                        f'<br><span style="color:var(--danger)">&#9650; High gamma this close to expiry.</span> '
                        f'<span style="color:var(--muted);font-size:11px">'
                        f'Gamma = how fast delta (directional exposure) changes as the stock moves — the '
                        f'"convexity" of the position; high gamma means small stock moves cause outsized P&amp;L '
                        f'swings. This position’s $Gamma: {html_mod.escape(_dg_str)} (a 1% move in {html_mod.escape(pk_sym)} '
                        f'would shift delta exposure by roughly that many dollars). Flagged because DTE ≤ 14 '
                        f'and |delta| ≥ 0.30 (here: {_dte_str} DTE, |delta| {_ad_str}) — gamma accelerates '
                        f'sharply this close to expiry, especially near the money, so small moves swing this '
                        f'position’s value disproportionately to the theta reward left on the table.'
                        f'</span>'
                    )
                else:
                    gamma_note = ""
                if d.get("decay_quality") is None:
                    dq_line = (
                        f'<strong style="color:var(--muted)">—</strong> '
                        f'<span style="color:var(--muted);font-size:11px">'
                        f'({html_mod.escape(d.get("note") or "unavailable")})</span>'
                    )
                else:
                    pct = d["decay_quality"] * 100
                    color = "var(--ok)" if pct >= 70 else "var(--warn)" if pct >= 35 else "var(--danger)"
                    dq_line = f'<strong style="color:{color}">{pct:.0f}%</strong> of the move since entry is time decay'

                theta_day = d.get("theta_per_day")
                cap_eff   = d.get("capital_efficiency")
                dgamma    = d.get("dollar_gamma")
                vanna     = d.get("vanna")
                charm     = d.get("charm")
                opinion        = d.get("opinion")
                opinion_reason = d.get("opinion_reason")
                if opinion:
                    _op_color = ("var(--danger)" if opinion == "Close"
                                 else "var(--warn)" if opinion in ("Close/Roll", "Consider closing", "Hold (fragile)")
                                 else "var(--ok)")
                    opinion_line = (
                        f'<br><strong style="color:{_op_color}">{html_mod.escape(opinion)}</strong>'
                        f'{" — " + html_mod.escape(opinion_reason) if opinion_reason else ""}'
                    )
                else:
                    opinion_line = ""
                decay_html = (
                    f'<div class="chain-pnl" style="margin-top:14px">'
                    f'<span class="chain-label">Decay Quality Analysis</span>'
                    f'<span class="chain-working">'
                    f'{dq_line}{opinion_line}{gamma_note}<br>'
                    f'Spot: {_dfmt(d.get("spot_component"))}'
                    f'&nbsp;&nbsp;Time: {_dfmt(d.get("time_component"))}'
                    f'&nbsp;&nbsp;Vol: {_dfmt(d.get("vega_component"))}'
                    f'&nbsp;&nbsp;Residual: {_dfmt(d.get("residual"))}<br>'
                    f'Theta: {"—" if theta_day is None else f"${theta_day:.2f}/day"}'
                    f'&nbsp;&nbsp;Capital efficiency: {"—" if cap_eff is None else f"{cap_eff*100:.3f}%/day"}'
                    f'&nbsp;&nbsp;$Gamma: {"—" if dgamma is None else f"{dgamma:.0f}"}<br>'
                    f'Vanna: {"—" if vanna is None else f"{vanna:.4f}"}'
                    f'&nbsp;&nbsp;Charm: {"—" if charm is None else f"{charm:.4f}/day"}'
                    f'</span>'
                    f'</div>'
                )

            run_at_html = (
                f'<div style="font-size:11px;color:var(--muted);margin-bottom:12px">Updated {html_mod.escape(cached["_run_at"])}</div>'
                if cached.get("_run_at") else ""
            )

            # Luna (GPT-5.6) second opinion — compact badge next to the main
            # recommendation, plus a full card further down. Comparison only;
            # never overwrites Claude's own primary rec/text. See openai_advisor.py.
            openai_rec   = cached.get("openai_recommendation")
            openai_text  = cached.get("openai_text")
            openai_error = cached.get("openai_error")
            openai_run_at = cached.get("openai_run_at")
            if openai_rec:
                _ai2_cls = {"ROLL": "warn", "ASSIGNMENT": "danger", "HOLD": "hold"}.get(openai_rec, "ok")
                if not rec:
                    _agree_tag = ""  # no primary rec yet to compare against
                elif openai_rec == rec:
                    _agree_tag = '<span style="color:var(--ok);font-size:11px;margin-left:6px">&#10003; agrees</span>'
                else:
                    _agree_tag = '<span style="color:var(--warn);font-size:11px;margin-left:6px">&#9888; disagrees</span>'
                openai_badge_html = (
                    f'<span class="badge badge-{_ai2_cls}" style="font-size:11px;padding:3px 10px;margin-left:8px" '
                    f'title="Luna (GPT-5.6) second opinion">Luna: {html_mod.escape(openai_rec)}</span>{_agree_tag}'
                )
            else:
                openai_badge_html = (
                    '<button onclick="openaiCompare(this)" '
                    'style="font-size:11px;padding:3px 10px;margin-left:8px;background:#10a37f;color:#fff;'
                    'border:none;border-radius:4px;cursor:pointer">Compare with Luna</button>'
                )
            if openai_text:
                _ai2_body = _render_advisor_markdown(openai_text)
                _ai2_updated = (
                    f'&nbsp;&nbsp;<span style="font-weight:normal;color:var(--muted);font-size:11px">'
                    f'Updated {html_mod.escape(openai_run_at)}</span>' if openai_run_at else ""
                )
                openai_card_html = (
                    f'<div id="openai-card" class="chain-pnl" style="margin-top:14px;border-color:#10a37f">'
                    f'<span class="chain-label" style="color:#10a37f">Luna’s Take (GPT-5.6, second opinion){_ai2_updated}</span>'
                    f'<span class="chain-working" style="white-space:normal;line-height:1.6;display:block;margin-top:6px">{_ai2_body}</span>'
                    f'</div>'
                )
            elif openai_error:
                openai_card_html = (
                    f'<div id="openai-card" class="chain-pnl" style="margin-top:14px;border-color:#10a37f">'
                    f'<span class="chain-label" style="color:#10a37f">Luna’s Take</span>'
                    f'<span class="chain-working" style="color:var(--danger);display:block;margin-top:6px">Error: {html_mod.escape(openai_error)}</span>'
                    f'</div>'
                )
            else:
                openai_card_html = '<div id="openai-card"></div>'

            primary_ask_html = (
                '<div class="ask-section">'
                '<div class="ask-thread" id="ask-thread"></div>'
                '<div class="ask-input">'
                '<textarea id="ask-q" rows="3" placeholder="Ask Claude a follow-up question…"></textarea>'
                '<div class="ask-input-row">'
                '<button onclick="submitAsk()">Ask</button>'
                '<div id="ask-spinner"></div>'
                '</div></div></div>'
            )
            openai_ask_html = (
                '<div class="ask-section" style="margin-top:20px">'
                '<div class="ask-thread" id="openai-ask-thread"></div>'
                '<div class="ask-input">'
                '<textarea id="openai-ask-q" rows="3" placeholder="Ask Luna a follow-up question…"></textarea>'
                '<div class="ask-input-row">'
                '<button onclick="submitOpenaiAsk()" style="background:#10a37f;border-color:#10a37f">Ask Luna</button>'
                '<div id="openai-ask-spinner"></div>'
                '</div></div></div>'
            )

            body_html = (
                f'<div style="margin-bottom:16px">'
                f'<span class="badge badge-{rec_cls}" style="font-size:15px;padding:6px 16px">{html_mod.escape(rec)}</span>'
                f'<span id="openai-badge-slot">{openai_badge_html}</span>'
                f'{run_at_html}'
                f'</div>'
                f'<div class="rec-body">{"".join(lines_out)}</div>'
                f'{confirmed_html}'
                f'{chain_html}'
                f'{decay_html}'
                f'{primary_ask_html}'
                f'{openai_card_html}'
                f'{openai_ask_html}'
            )

        page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html_mod.escape(title_str)} — Recommendation</title>
<script>
(function(){{
  var c=document.createElement('canvas');
  c.width=c.height=64;
  var x=c.getContext('2d');
  x.fillStyle='white';
  x.beginPath();x.arc(32,32,30,0,Math.PI*2);x.fill();
  x.fillStyle='#052e16';
  x.textAlign='center';x.textBaseline='middle';
  x.font='bold {_fs}px monospace';
  x.fillText('{ticker_sym}',32,32);
  var l=document.querySelector("link[rel~='icon']")||document.createElement('link');
  l.rel='icon';l.href=c.toDataURL();
  document.head.appendChild(l);
}})();
</script>
<style>
  :root {{
    --bg:#0f1117;--surface:#1a1d27;--border:#2a2d3a;
    --text:#e2e8f0;--muted:#8892a4;--accent:#4f8ef7;
    --ok:#22c55e;--warn:#f59e0b;--danger:#ef4444;
    --ok-bg:#052e16;--warn-bg:#451a03;--danger-bg:#450a0a;
  }}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:'SF Mono','Fira Code',monospace;font-size:13px;line-height:1.6;padding:24px}}
  header{{display:flex;align-items:center;gap:16px;margin-bottom:24px;flex-wrap:wrap}}
  header h1{{font-size:15px;font-weight:600;color:var(--accent)}}
  .pos-label{{color:var(--muted);font-size:12px}}
  a.back{{color:var(--accent);text-decoration:none;font-size:12px}}
  a.back:hover{{text-decoration:underline}}
  .badge{{display:inline-block;border-radius:4px;padding:2px 7px;font-size:11px;font-weight:600;letter-spacing:.05em}}
  .badge-ok{{background:var(--ok-bg);color:var(--ok)}}
  .badge-hold{{background:#0c1a2e;color:var(--accent);border:1px solid var(--accent)}}
  .badge-warn{{background:var(--warn-bg);color:var(--warn)}}
  .badge-danger{{background:var(--danger-bg);color:var(--danger)}}
  .rec-body h1,.rec-body h2{{color:var(--accent);margin:16px 0 6px;font-size:14px}}
  .rec-body h3{{color:var(--text);margin:14px 0 4px;font-size:13px;font-weight:600}}
  .rec-body p{{margin:4px 0;max-width:860px}}
  .rec-body li{{margin:2px 0 2px 20px;max-width:860px}}
  .rec-body strong{{color:var(--text)}}
  .rec-body br{{display:block;margin:6px 0;content:""}}
  .rec-body table.atbl{{border-collapse:collapse;margin:14px 0;font-size:12px;max-width:860px}}
  .rec-body table.atbl th,.rec-body table.atbl td{{padding:7px 14px;border:1px solid var(--border);text-align:left;vertical-align:top;white-space:normal;line-height:1.5}}
  .rec-body table.atbl th{{background:rgba(255,255,255,.07);color:var(--accent);font-weight:600;white-space:nowrap}}
  .rec-body table.atbl td:first-child{{font-weight:600;color:var(--text);background:rgba(255,255,255,.03);white-space:nowrap}}
  .rec-body table.atbl tr:nth-child(even) td{{background:rgba(255,255,255,.025)}}
  .rec-body table.atbl tr:nth-child(even) td:first-child{{background:rgba(255,255,255,.055)}}
  .chain-pnl{{margin-top:28px;padding:14px 18px;background:var(--surface);border:1px solid var(--border);border-radius:8px;max-width:860px}}
  .chain-label{{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:6px}}
  .chain-working{{font-size:13px;color:var(--text)}}
  .ask-section{{margin-top:36px;max-width:860px}}
  .ask-thread{{margin-bottom:12px;display:flex;flex-direction:column;gap:12px}}
  .ask-q{{background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:10px 14px;font-size:12px;color:var(--muted)}}
  .ask-q::before{{content:'You: ';color:var(--accent);font-weight:600}}
  .ask-a{{background:#12151f;border:1px solid var(--border);border-radius:6px;padding:10px 14px;font-size:12px;color:var(--text);white-space:pre-wrap;line-height:1.6}}
  .ask-a::before{{content:'Claude: ';color:var(--ok);font-weight:600}}
  .ask-a.openai-ask-a::before{{content:'Luna: ';color:#10a37f}}
  .ask-a.err{{color:var(--danger)}}
  .ask-input{{display:flex;flex-direction:column;gap:8px}}
  .ask-input textarea{{background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:10px 12px;font-size:12px;font-family:inherit;resize:vertical;min-height:64px;outline:none}}
  .ask-input textarea:focus{{border-color:var(--accent)}}
  .ask-input-row{{display:flex;gap:8px;align-items:center}}
  .ask-input-row button{{padding:6px 16px}}
  #ask-spinner{{display:none;width:16px;height:16px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin 0.7s linear infinite;flex-shrink:0}}
  @keyframes spin{{to{{transform:rotate(360deg)}}}}
</style>
</head>
<body>
<header>
  <a class="back" href="/" onclick="window.close();return false;">&#8592; Back</a>
  <h1>&#9660; Recommendation</h1>
  <span class="pos-label">{html_mod.escape(title_str)}</span>
</header>
{body_html}
<script>
const _POS_KEY = {json.dumps(pos_key)};
const _MAIN_REC = {json.dumps(rec)};
const _SAVED_QA = {json.dumps(cached.get("qa_thread", []) if cached else [])};
const _OPENAI_SAVED_QA = {json.dumps(cached.get("openai_qa_thread", []) if cached else [])};
function esc(s) {{
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}
function _openaiBadgeCls(rec) {{
  return rec === 'ROLL' ? 'warn' : rec === 'ASSIGNMENT' ? 'danger' : rec === 'HOLD' ? 'hold' : 'ok';
}}
function _advisorInline(s) {{
  s = esc(s);
  s = s.replace(/\\*\\*(.+?)\\*\\*/g, '<strong>$1</strong>');
  s = s.replace(/\\*(.+?)\\*/g, '<em>$1</em>');
  return s;
}}
function renderAdvisorMarkdown(text) {{
  const blocks = [];
  let bulletBuf = [];
  const flushBullets = () => {{
    if (bulletBuf.length) {{ blocks.push('<ul style="margin:4px 0 4px 20px">' + bulletBuf.join('') + '</ul>'); bulletBuf = []; }}
  }};
  for (const raw of text.split('\\n')) {{
    const line = raw.trim();
    if (!line) {{ flushBullets(); continue; }}
    if (/^-{{3,}}$|^\\*{{3,}}$/.test(line)) {{
      flushBullets();
      blocks.push('<hr style="border:none;border-top:1px solid var(--border);margin:10px 0">');
      continue;
    }}
    const headingM = line.match(/^(#{{1,4}})\\s+(.*)/);
    if (headingM) {{
      flushBullets();
      blocks.push(`<p style="margin-top:10px;font-weight:600;color:var(--accent)">${{_advisorInline(headingM[2])}}</p>`);
      continue;
    }}
    if (line.startsWith('- ') || line.startsWith('\\u2022 ')) {{
      bulletBuf.push(`<li>${{_advisorInline(line.slice(2))}}</li>`);
      continue;
    }}
    flushBullets();
    blocks.push(`<p style="margin-bottom:10px">${{_advisorInline(line)}}</p>`);
  }}
  flushBullets();
  return blocks.join('');
}}
async function openaiCompare(btn) {{
  if (btn) {{ btn.disabled = true; btn.textContent = 'Comparing…'; }}
  try {{
    const r = await fetch('/api/openai-compare', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{position_key: _POS_KEY}})
    }});
    const d = await r.json();
    const slot = document.getElementById('openai-badge-slot');
    const card = document.getElementById('openai-card');
    if (d.error) {{
      if (slot) slot.innerHTML = `<span style="color:var(--danger);font-size:11px;margin-left:8px">Luna error: ${{esc(d.error)}}</span>`;
      return;
    }}
    if (slot) {{
      const cls = _openaiBadgeCls(d.recommendation);
      const agreeTag = !_MAIN_REC
        ? ''
        : d.recommendation === _MAIN_REC
        ? '<span style="color:var(--ok);font-size:11px;margin-left:6px">&#10003; agrees</span>'
        : '<span style="color:var(--warn);font-size:11px;margin-left:6px">&#9888; disagrees</span>';
      slot.innerHTML = `<span class="badge badge-${{cls}}" style="font-size:11px;padding:3px 10px;margin-left:8px" title="Luna (GPT-5.6) second opinion">Luna: ${{esc(d.recommendation||'?')}}</span>${{agreeTag}}`;
    }}
    if (card) {{
      const body = renderAdvisorMarkdown(d.text || '');
      card.outerHTML = `<div id="openai-card" class="chain-pnl" style="margin-top:14px;border-color:#10a37f">`
        + `<span class="chain-label" style="color:#10a37f">Luna’s Take (GPT-5.6, second opinion)</span>`
        + `<span class="chain-working" style="white-space:normal;line-height:1.6;display:block;margin-top:6px">${{body}}</span>`
        + `</div>`;
    }}
  }} catch(e) {{
    const slot = document.getElementById('openai-badge-slot');
    if (slot) slot.innerHTML = `<span style="color:var(--danger);font-size:11px;margin-left:8px">Luna error: ${{esc(e.message)}}</span>`;
  }} finally {{
    if (btn) {{ btn.disabled = false; btn.textContent = 'Compare with Luna'; }}
  }}
}}
(function() {{
  const thread = document.getElementById('ask-thread');
  for (const item of _SAVED_QA) {{
    const qEl = document.createElement('div'); qEl.className='ask-q'; qEl.textContent=item.q; thread.appendChild(qEl);
    const aEl = document.createElement('div'); aEl.className='ask-a'; aEl.innerHTML=renderAdvisorMarkdown(item.a); thread.appendChild(aEl);
  }}
  const openaiThread = document.getElementById('openai-ask-thread');
  for (const item of _OPENAI_SAVED_QA) {{
    const qEl = document.createElement('div'); qEl.className='ask-q'; qEl.textContent=item.q; openaiThread.appendChild(qEl);
    const aEl = document.createElement('div'); aEl.className='ask-a openai-ask-a'; aEl.innerHTML=renderAdvisorMarkdown(item.a); openaiThread.appendChild(aEl);
  }}
}})();
async function submitOpenaiAsk() {{
  const ta = document.getElementById('openai-ask-q');
  const q = ta.value.trim();
  if (!q) return;
  const thread = document.getElementById('openai-ask-thread');
  const qEl = document.createElement('div');
  qEl.className = 'ask-q';
  qEl.textContent = q;
  thread.appendChild(qEl);
  ta.value = '';
  document.getElementById('openai-ask-spinner').style.display = 'block';
  const aEl = document.createElement('div');
  aEl.className = 'ask-a openai-ask-a';
  aEl.textContent = '…';
  thread.appendChild(aEl);
  aEl.scrollIntoView({{behavior:'smooth'}});
  try {{
    const r = await fetch('/api/openai-ask', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{position_key: _POS_KEY, question: q}})
    }});
    const d = await r.json();
    if (d.error) {{ aEl.className = 'ask-a openai-ask-a err'; aEl.textContent = d.error; }}
    else {{ aEl.innerHTML = renderAdvisorMarkdown(d.answer || ''); }}
  }} catch(e) {{
    aEl.className = 'ask-a openai-ask-a err'; aEl.textContent = e.message;
  }} finally {{
    document.getElementById('openai-ask-spinner').style.display = 'none';
    aEl.scrollIntoView({{behavior:'smooth'}});
  }}
}}
document.getElementById('openai-ask-q').addEventListener('keydown', e => {{
  if (e.key === 'Enter' && !e.shiftKey) {{ e.preventDefault(); submitOpenaiAsk(); }}
}});
async function submitAsk() {{
  const ta = document.getElementById('ask-q');
  const q = ta.value.trim();
  if (!q) return;
  const thread = document.getElementById('ask-thread');
  const qEl = document.createElement('div');
  qEl.className = 'ask-q';
  qEl.textContent = q;
  thread.appendChild(qEl);
  ta.value = '';
  document.getElementById('ask-spinner').style.display = 'block';
  const aEl = document.createElement('div');
  aEl.className = 'ask-a';
  aEl.textContent = '…';
  thread.appendChild(aEl);
  aEl.scrollIntoView({{behavior:'smooth'}});
  try {{
    const r = await fetch('/api/ask', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{position_key: _POS_KEY, question: q}})
    }});
    const d = await r.json();
    if (d.error) {{ aEl.className = 'ask-a err'; aEl.textContent = d.error; }}
    else {{ aEl.innerHTML = renderAdvisorMarkdown(d.answer || ''); }}
  }} catch(e) {{
    aEl.className = 'ask-a err'; aEl.textContent = e.message;
  }} finally {{
    document.getElementById('ask-spinner').style.display = 'none';
    aEl.scrollIntoView({{behavior:'smooth'}});
  }}
}}
document.getElementById('ask-q').addEventListener('keydown', e => {{
  if (e.key === 'Enter' && !e.shiftKey) {{ e.preventDefault(); submitAsk(); }}
}});
</script>
</body>
</html>"""
        return Response(page, mimetype="text/html", headers={"Cache-Control": "no-store"})

    port = 5051
    url  = f"http://127.0.0.1:{port}"
    log.info("Starting portfolio dashboard at %s", url)
    print(f"\nStarting portfolio dashboard at {url}")
    print("Press Ctrl+C to stop.\n")

    def _open_browser():
        import time
        time.sleep(0.8)
        webbrowser.open(url)

    import signal

    def _sigterm_handler(signum, frame):
        # launchd owns the restart — just exit cleanly and let it respawn.
        log.warning("SIGTERM received — exiting for launchd restart")
        os._exit(0)

    signal.signal(signal.SIGTERM, _sigterm_handler)

    # Only open a browser tab on a fresh start, not when bounced via SIGTERM (kill -15).
    if not os.environ.get("_PORTFOLIO_BOUNCE"):
        threading.Thread(target=_open_browser, daemon=True).start()
    else:
        log.info("Bounce restart detected (_PORTFOLIO_BOUNCE set) — skipping browser open")
        # Clear the flag so any further child bounces are also suppressed correctly
        os.environ.pop("_PORTFOLIO_BOUNCE", None)
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False, threaded=True)


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
    except ImportError as _yf_err:
        log.error("yfinance import failed (%s) — pip install yfinance", _yf_err)
        return {
            "earnings_date": "Unknown", "earnings_source": "unknown",
            "exdiv_date": "Unknown", "exdiv_source": "unknown",
            "dividend_amount": "N/A",
        }

    today = datetime.date.today()
    result = {
        "earnings_date": "Unknown",
        "earnings_source": "unknown",
        "exdiv_date": "Unknown",
        "exdiv_source": "unknown",
        "dividend_amount": "N/A",
    }

    t = yf.Ticker(ticker)

    # ── Earnings date (skip for ETFs — they have no earnings calendar) ─────────
    all_cal_dates = []  # every earnings date the calendar knows about, past or future
    try:
        fi_check = t.fast_info
        # ETFs report quoteType as "ETF"; skip earnings lookup to avoid quoteSummary 404s
        if getattr(fi_check, "quote_type", "").upper() == "ETF":
            raise StopIteration  # jump to except, leave earnings_date as "Unknown"
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
                all_cal_dates.append(dt)
                if dt >= today:
                    future.append(dt)
            except Exception:
                pass
        if future:
            result["earnings_date"] = str(min(future))
            result["earnings_source"] = "confirmed"
    except Exception:
        pass

    # Estimate earnings if not found: anchor off the most recent *report* date
    # + ~91 days (one quarter). Prefer the calendar's own past earnings date
    # over quarterly_income_stmt, whose columns are fiscal quarter-end dates
    # (not report dates) and understate the gap to the next report by weeks.
    if result["earnings_source"] != "confirmed":
        try:
            if getattr(t.fast_info, "quote_type", "").upper() == "ETF":
                raise StopIteration
            hist_earnings = list(all_cal_dates)
            if not hist_earnings:
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
    # Use fast_info (no quoteSummary call — safe for ETFs like GDX, SLV, GLD)
    try:
        fi = t.fast_info
        ex_ts = getattr(fi, "last_dividend_date", None)
        if ex_ts:
            ex_date = ex_ts.date() if hasattr(ex_ts, "date") else datetime.date.fromtimestamp(int(ex_ts))
            if ex_date >= today:
                result["exdiv_date"] = str(ex_date)
                result["exdiv_source"] = "confirmed"
            last_div = getattr(fi, "last_dividend_value", None)
            if last_div:
                result["dividend_amount"] = f"${float(last_div):.4f}".rstrip("0").rstrip(".")
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


async def _ensure_fed_calendar_source(client, notebook_id: str) -> None:
    """
    Keep a NEXT-month NY Fed Economic Indicators Calendar source in the
    notebook, alongside the pre-existing current-month source (that one's
    the base https://.../nationalecon_cal URL, added manually — untouched
    here). The base source only ever shows "this month"; late in the month,
    a 1-week-ahead view needs next month's page too, which only exists at
    its own dated URL (i-{mon}{yy}.html). Self-heals on month rollover: any
    stale i-*.html source we previously added gets replaced with the
    current correct one. Best-effort — failures here must never break the
    caller (dashboard startup / scheduled refresh).
    """
    import datetime
    today = datetime.date.today()
    next_month = (today.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    correct_url = claude_advisor.fed_month_url(next_month)

    try:
        sources = await client.sources.list(notebook_id)
    except Exception as exc:
        log.warning("[fed-calendar] Could not list sources: %s", exc)
        return

    stale_ids = []
    already_present = False
    for source in sources:
        url = getattr(source, "url", None) or ""
        if "/research/calendars/i-" not in url:
            continue
        if url == correct_url:
            already_present = True
        else:
            stale_ids.append(source.id)

    for sid in stale_ids:
        try:
            await client.sources.delete(notebook_id, sid)
            log.info("[fed-calendar] Removed stale month source: %s", sid)
        except Exception as exc:
            log.warning("[fed-calendar] Failed to delete stale source %s: %s", sid, exc)

    if not already_present:
        try:
            await client.sources.add_url(notebook_id, correct_url)
            log.info("[fed-calendar] Added next-month source: %s", correct_url)
        except Exception as exc:
            log.warning("[fed-calendar] Failed to add %s: %s", correct_url, exc)


def _is_ticker_csv_source(title: str) -> bool:
    """True if a notebook source title looks like a {TICKER}.csv upload."""
    stem = title.split(".")[0]
    return 1 <= len(stem) <= 5 and stem.isupper() and title.endswith(".csv")


async def _purge_stale_ticker_sources(client, notebook_id: str, uploading_ticker: str | None = None) -> None:
    """
    Before uploading a new ticker CSV, remove any existing sources for that
    same ticker (prevents same-ticker dupes). Also age out any other ticker
    CSV sources that are more than 1 day old (catches leaked sources whose
    post-query delete failed).

    A "ticker source" is identified by:
      • filename ends with .csv
      • filename stem (left of the first '.') is all-uppercase, 1–5 characters
    """
    now    = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=1)
    target = (uploading_ticker or "").upper()

    try:
        sources = await client.sources.list(notebook_id)
    except Exception as exc:
        log.warning("[cleanup] Could not list sources: %s", exc)
        return

    for source in sources:
        title = source.title or ""
        if not _is_ticker_csv_source(title):
            continue
        stem = title.split(".")[0].upper()

        # Always remove existing copies of the ticker we're about to upload
        if target and stem == target:
            try:
                await client.sources.delete(notebook_id, source.id)
                log.info("[cleanup] Replaced existing source: %r", title)
            except Exception as exc:
                log.error("[cleanup] Failed to delete %r: %s", title, exc)
            continue

        # Age out other ticker sources whose post-query delete apparently failed
        created_at = source.created_at
        if created_at is None:
            continue
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=datetime.timezone.utc)
        if created_at > cutoff:
            continue
        try:
            await client.sources.delete(notebook_id, source.id)
            log.info("[cleanup] Aged out source: %r (uploaded %s)", title, created_at.date())
        except Exception as exc:
            log.error("[cleanup] Failed to delete %r: %s", title, exc)


async def _purge_all_ticker_sources(notebook_id: str) -> None:
    """Remove every {TICKER}.csv source from the notebook. Used by --purge-notebook."""
    from notebooklm import NotebookLMClient
    async with NotebookLMClient.from_storage() as client:
        try:
            sources = await client.sources.list(notebook_id)
        except Exception as exc:
            print(f"ERROR: Could not list sources: {exc}")
            return
        for source in sources:
            title = source.title or ""
            if not _is_ticker_csv_source(title):
                continue
            try:
                await client.sources.delete(notebook_id, source.id)
                print(f"  Deleted: {title}")
            except Exception as exc:
                print(f"  ERROR deleting {title!r}: {exc}")


async def upload_to_notebooklm(file_path: str, notebook_id: str) -> str | None:
    """Upload a file as a new source to the specified NotebookLM notebook.

    Uses wait=False to avoid the GET_NOTEBOOK polling that causes timeouts.
    Deletes any previously tracked source for this ticker before uploading
    a new one (avoids duplicates without needing sources.list()).

    Returns the new source ID, or None if the upload was skipped or failed.
    Callers should sleep ~15s before querying to let the source process.
    """
    try:
        from notebooklm import NotebookLMClient
    except ImportError:
        log.error("notebooklm-py is not installed — pip install 'notebooklm-py[browser]'")
        raise ImportError("notebooklm-py is not installed")

    base = os.path.basename(file_path)
    stem = base.split(".")[0].upper()
    uploading_ticker = stem if (1 <= len(stem) <= 5 and stem.isupper()) else None

    # Skip upload if we recently uploaded this ticker
    if uploading_ticker:
        last = _last_upload_time.get(uploading_ticker)
        if last and (datetime.datetime.now() - last).total_seconds() < _UPLOAD_TTL_MINUTES * 60:
            log.info("Skipping upload for %s — uploaded %.0f min ago (TTL %d min)",
                     uploading_ticker,
                     (datetime.datetime.now() - last).total_seconds() / 60,
                     _UPLOAD_TTL_MINUTES)
            return None

    log.info("Uploading %s to NotebookLM notebook %s ...", file_path, notebook_id)
    try:
        async with NotebookLMClient.from_storage() as client:
            # Sweep any leaked source for this ticker (or aged-out sources from other
            # tickers) before uploading. Catches dupes left behind when a previous
            # run's delete-after-query never fired (process restart, crashed mid-query,
            # etc.) — the in-memory _last_source_ids dict alone can't recover from that.
            _last_source_ids.pop(uploading_ticker, None) if uploading_ticker else None
            await _purge_stale_ticker_sources(client, notebook_id, uploading_ticker)

            # wait=False avoids the GET_NOTEBOOK polling that times out
            source = await client.sources.add_file(notebook_id, file_path, wait=False)

        source_id = source.id if source else None
        if uploading_ticker:
            _last_upload_time[uploading_ticker] = datetime.datetime.now()
            if source_id:
                _last_source_ids[uploading_ticker] = source_id
        log.info("Upload complete: %s (source_id=%s)", os.path.basename(file_path), source_id)
        return source_id
    except Exception as exc:
        log.error("Upload failed — %s", exc)
        raise


async def delete_notebooklm_source(notebook_id: str, source_id: str | None) -> None:
    """Delete a single source from the notebook by ID. No-op if source_id is None."""
    if not source_id:
        return
    try:
        from notebooklm import NotebookLMClient
        async with NotebookLMClient.from_storage() as client:
            await client.sources.delete(notebook_id, source_id)
        log.info("[cleanup] Deleted source %s from notebook", source_id)
    except Exception as exc:
        log.warning("[cleanup] Could not delete source %s: %s", source_id, exc)


STRAT_LABELS = {
    "CC":   "Covered Call",
    "CSP":  "Cash-Secured Put",
    "ROLL": "Roll",
}


COL_WIDTH = 125  # total terminal width for the output box

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


async def _reset_notebooklm_conversation(client, notebook_id: str) -> None:
    """
    Delete the notebook's current server-side conversation (if any) before
    asking a fresh, independent question.

    Every call site in this file already builds a fully self-contained
    prompt — original analysis + recent Q&A embedded as plain text — so
    nothing here actually depends on NotebookLM's own server-side
    conversation memory. Left alone, `chat.ask(notebook_id, question)` with
    no `conversation_id` just keeps extending the SAME one conversation
    forever (confirmed via the notebooklm-py client's own docstring), and
    since this notebook is shared across every ticker, every scheduled
    refresh, and every follow-up question, that conversation had — after
    ~3 weeks of continuous automated use — grown long enough that NotebookLM
    started returning responses over the client's 50MB RPC size cap
    (`RPC response exceeded 52428800 bytes`) or stalling mid-stream, on an
    increasing fraction of calls, across multiple unrelated tickers (LULU,
    BIRK, MSFT, DG all hit it the same morning). Deleting the prior
    conversation before each independent ask keeps every request small and
    fast, matching what it should have been the whole time. Best-effort:
    failures here (e.g. no conversation exists yet) must never block the
    actual question.
    """
    try:
        conv_id = await client.chat.get_conversation_id(notebook_id)
        if conv_id:
            await client.chat.delete_conversation(notebook_id, conv_id)
    except Exception as exc:
        log.warning("[notebooklm] could not reset conversation before asking: %s", exc)


async def query_notebooklm(
    notebook_id: str,
    ticker: str,
    strat: str,
    dates: dict,
    positions: list[dict] | None = None,
    vix: float | None = None,
    silent: bool = False,
    ul_cost_basis: float | None = None,
    chain_data: dict | None = None,
    current_leg_price: float | None = None,
) -> str | None:
    """Ask NotebookLM for the best CC or CSP choice given the ticker source."""
    try:
        from notebooklm import NotebookLMClient
    except ImportError:
        log.error("notebooklm-py is not installed — pip install 'notebooklm-py[browser]'")
        raise ImportError("notebooklm-py is not installed")

    strat_label = STRAT_LABELS[strat]

    # Embed known dates so the LLM can reason about expiry selection precisely
    # Only include clauses where data is actually known
    _earnings_known = dates.get("earnings_date", "Unknown").lower() not in ("unknown", "n/a", "")
    _exdiv_known    = dates.get("exdiv_date",   "Unknown").lower() not in ("unknown", "n/a", "")
    earnings_note = (
        f"the next earnings announcement is {dates['earnings_date']} ({dates['earnings_source']})"
        if _earnings_known else ""
    )
    exdiv_note = (
        f"the next ex-dividend date is {dates['exdiv_date']} "
        f"({dates['exdiv_source']}, {dates['dividend_amount']} per share)"
        if _exdiv_known else ""
    )
    known_notes = [n for n in [earnings_note, exdiv_note] if n]
    dates_clause = ("Note that " + " and ".join(known_notes) + ". ") if known_notes else ""

    vix_note = f"the current VIX is {vix:.2f}" if vix is not None else ""

    # ── Underlying price (live from yfinance) ─────────────────────────────────
    ul_price = get_underlying_price(ticker)
    ul_price_note = (
        f"The current underlying price of {ticker} is ${ul_price:.2f}. "
        if ul_price is not None else ""
    )

    # ── Cost basis clause ──────────────────────────────────────────────────────
    _cb = float(ul_cost_basis) if ul_cost_basis else 0.0
    if strat == "CSP":
        # Cost basis is not relevant for CSPs — omit entirely
        cost_basis_clause = ""
    elif _cb > 0:
        if strat == "ROLL":
            cost_basis_clause = (
                f"The cost basis of the underlying {ticker} shares is ${_cb:.2f} per share. "
                f"Factor this into your roll recommendation — in particular, flag any scenario "
                f"where rolling to a lower strike would place it below cost basis, which could "
                f"result in a loss on assignment. "
            )
        else:  # CC
            cost_basis_clause = (
                f"The cost basis of the underlying {ticker} shares is ${_cb:.2f} per share. "
                f"Please ensure the recommended strike is at or above the cost basis to avoid "
                f"realizing a loss on assignment, and factor this into your strike selection. "
            )
    else:
        # Don't flag missing cost basis when rolling put positions — stock isn't owned
        _rolling_puts = (
            strat == "ROLL"
            and positions
            and all(str(p.get("option_type", "")).upper() == "PUT" for p in positions)
        )
        if _rolling_puts:
            cost_basis_clause = ""
        else:
            cost_basis_clause = (
                f"Note: The cost basis of the underlying {ticker} is not available "
                f"(ul_cost_basis is 0 or not set). Please flag this gap in your analysis. "
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

        if chain_data and chain_data.get("num_positions", 0) > 0:
            net   = chain_data["net_cash"]
            coll  = chain_data["collected"]
            paid_ = chain_data["paid"]
            n_pos = chain_data["num_positions"]
            net_sign = "+" if net >= 0 else ""

            # BTC cost to close the current leg at market before rolling
            btc_cost = round((current_leg_price or 0.0) * 100 * total_contracts, 2)
            # Adjusted net after closing the current leg
            adj_net  = round(net - btc_cost, 2)
            adj_sign = "+" if adj_net >= 0 else ""

            if adj_net < 0:
                # min STO premium so that adj_net + P×0.60×100×C > 0
                min_sto = round(abs(adj_net) / (0.60 * 100 * total_contracts), 2)
                leg_clause = (
                    f"Step 1 — close current leg: "
                    f"${net_sign}{net:,.2f} (chain to date) "
                    f"− ${btc_cost:,.2f} BTC current leg at ${current_leg_price:.2f} "
                    f"= ${adj_sign}${abs(adj_net):,.2f} adjusted net. "
                ) if current_leg_price else (
                    f"Step 1 — close current leg at market (BTC cost unknown — use live mid). "
                )
                viability = (
                    f"{leg_clause}"
                    f"Step 2 — the new STO premium must exceed ${min_sto:.2f}/share "
                    f"(= ${abs(adj_net):,.2f} ÷ (0.60 × 100 × {total_contracts} contracts)) "
                    f"for the chain to break even. "
                    f"REJECT any roll candidate with STO premium ≤ ${min_sto:.2f}. "
                    f"If no candidate clears this threshold, recommend 'Do Nothing'."
                )
            else:
                min_sto = 0.0
                viability = (
                    f"After closing the current leg at ${current_leg_price:.2f} "
                    f"(BTC cost ${btc_cost:,.2f}), the adjusted chain net is "
                    f"{adj_sign}${abs(adj_net):,.2f}. "
                    f"Any roll collecting additional premium will improve it."
                    if current_leg_price else
                    f"The chain net is {net_sign}${net:,.2f}. Any roll collecting "
                    f"additional premium improves it."
                )

            pnl_instruction = (
                f"CHAIN PNL ANALYSIS — complete this before finalising your recommendation. "
                f"Net chain cash to date: {net_sign}${net:,.2f} "
                f"(${coll:,.2f} collected − ${paid_:,.2f} paid across {n_pos} position(s)). "
                f"{viability} "
                f"Do NOT include a projected net chain PnL dollar figure in the Execution "
                f"Instructions — the dashboard computes that deterministically from actual "
                f"and test trades once entered. Use the CHAIN PNL ANALYSIS above only to "
                f"decide whether the roll clears the breakeven threshold."
            )
        else:
            pnl_instruction = (
                f"Do NOT include a projected PnL dollar figure in the Execution Instructions "
                f"— the dashboard computes that deterministically from actual and test trades "
                f"once entered."
            )
        vix_clause = f", the VIX ({vix_note})," if vix_note else ""
        import zoneinfo as _zi
        _now_et = datetime.datetime.now(_zi.ZoneInfo("America/New_York"))
        _today_preamble = _now_et.strftime(
            "Today is %A, %B %-d, %Y at %-I:%M %p ET. Start your response with a line "
            "reading exactly 'It is %A, %-m/%-d @ %-H:%M.' using this exact date/time, "
            "before anything else, so I can confirm you have the correct current date/time. "
            "Do not compute or invent the weekday or date of any other day yourself, and "
            "never pair a weekday range with a calendar-date range (e.g. 'Mon-Fri (Jul "
            "13-19)') unless every date in it is verified against its correct weekday — "
            "prefer relative phrasing like 'the rest of this week' or 'within 1-2 trading "
            "days' instead. "
        )
        question = (
            f"{_today_preamble}"
            f"Based on the updated {ticker} source, the latest economic release calendar, "
            f"and the latest PLAN and REVIEW sources{vix_clause} determine the best roll or "
            f"do nothing strategy. "
            f"{ul_price_note}"
            f"{pos_context}"
            f"{dates_clause}"
            f"{cost_basis_clause}"
            f"{pnl_instruction}"
        )
    else:
        vix_clause = f", the VIX ({vix_note})," if vix_note else ""
        import zoneinfo as _zi
        _now_et = datetime.datetime.now(_zi.ZoneInfo("America/New_York"))
        _today_preamble = _now_et.strftime(
            "Today is %A, %B %-d, %Y at %-I:%M %p ET. Start your response with a line "
            "reading exactly 'It is %A, %-m/%-d @ %-H:%M.' using this exact date/time, "
            "before anything else, so I can confirm you have the correct current date/time. "
            "Do not compute or invent the weekday or date of any other day yourself, and "
            "never pair a weekday range with a calendar-date range (e.g. 'Mon-Fri (Jul "
            "13-19)') unless every date in it is verified against its correct weekday — "
            "prefer relative phrasing like 'the rest of this week' or 'within 1-2 trading "
            "days' instead. "
        )
        question = (
            f"{_today_preamble}"
            f"Given the {ticker} source, what is the best {strat_label} choice, "
            f"taking into consideration the economic calendar releases, "
            f"the latest PLAN and REVIEW sources{vix_clause} and the upcoming "
            f"dividends and/or earnings releases?\n\n"
            f"{ul_price_note}"
            f"{dates_clause}\n\n"
            f"{cost_basis_clause}\n\n"
            f"Explicitly audit every recommendation against the T-Bill hurdle rate for the "
            f"current week. Calculate the annualized yield for the suggested strike and compare "
            f"it to the risk-free rate. If no strike at or above my cost basis clearly beats "
            f"the T-Bill baseline, recommend 'Doing Nothing' as the most professional move, "
            f"as per the strategy manuals."
        )

    log.info(
        "[query_notebooklm] prompt for %s (copy/paste ready):\n%s\n%s\n%s",
        ticker,
        "-" * 72,
        question.strip(),
        "-" * 72,
    )
    if not silent:
        print(f"\nQuerying NotebookLM...")

    _TRANSPORT_KEYWORDS = ("timeout", "timed out", "transport", "network", "connection", "reset", "eof", "read", "rpc", "malformed")
    _MAX_ATTEMPTS = 4
    _TRANSPORT_BACKOFF = [15, 30, 60]  # seconds between transport-error retries
    # Per-attempt cap on chat.ask() itself, independent of the various outer
    # 300s budgets (api_unborn's _run_analysis, scheduled_refresh's POLL_MAX).
    # Without this, a stalled streaming response (HTTP 200 received, then the
    # body just stops arriving mid-stream with no further bytes — observed
    # directly in the logs via httpcore's receive_response_body.started with
    # no .complete for 4+ minutes) blocks silently inside this single await
    # with no exception to trigger the retry loop below, so the entire outer
    # budget gets burned on one hung attempt with zero retries actually
    # happening — exactly what produced the "Analysis timed out" the user saw
    # for LULU with no warnings/retries logged in between. Bounding it here
    # converts a silent full-budget hang into a normal retryable failure.
    _PER_ATTEMPT_TIMEOUT = 90

    last_exc = None
    async with NotebookLMClient.from_storage() as client:
        await _reset_notebooklm_conversation(client, notebook_id)
        for attempt in range(_MAX_ATTEMPTS):
            try:
                result = await asyncio.wait_for(
                    client.chat.ask(notebook_id, question), timeout=_PER_ATTEMPT_TIMEOUT
                )
                # The notebooklm client sometimes can't find NotebookLM's actual
                # "marked answer" in the raw API response (logs "No marked answer
                # found; falling back to longest unmarked text") and falls back to
                # whatever text looks longest — which can be NotebookLM's own
                # internal reasoning/tool-call trace instead of a real answer.
                # Every valid response is required (by our own system prompt) to
                # open with "It is [weekday]..." — use that as a cheap, reliable
                # sanity check rather than silently caching garbage as a real
                # recommendation.
                _candidate = getattr(result, "answer", None) or str(result)
                if not _candidate.strip().lower().startswith("it is "):
                    raise RuntimeError(
                        "NotebookLM returned a malformed response (missing the "
                        "expected 'It is [weekday]...' opening line — likely its "
                        "own internal reasoning leaking through after an API "
                        f"hiccup, not a real answer). Got: {_candidate[:150]!r}"
                    )
                break
            except asyncio.TimeoutError as exc:
                # asyncio.TimeoutError carries no message (str(exc) == ""), so
                # it would never match _TRANSPORT_KEYWORDS below — give it an
                # explicit message so it's treated as the retryable transport
                # error it is, not silently mis-bucketed.
                exc = asyncio.TimeoutError(
                    f"chat.ask stalled past the {_PER_ATTEMPT_TIMEOUT}s per-attempt timeout"
                )
                last_exc = exc
                msg = str(exc).lower()
                log.warning(
                    "[query_notebooklm] chat.ask stalled past %ds (attempt %d/%d)",
                    _PER_ATTEMPT_TIMEOUT, attempt + 1, _MAX_ATTEMPTS,
                )
                if attempt < _MAX_ATTEMPTS - 1:
                    wait = _TRANSPORT_BACKOFF[min(attempt, len(_TRANSPORT_BACKOFF) - 1)]
                    log.warning("[query_notebooklm] retrying in %ds", wait)
                    await asyncio.sleep(wait)
                else:
                    log.warning("[query_notebooklm] stalled on final attempt — giving up")
                    raise RuntimeError(
                        f"NotebookLM stalled mid-response after {_MAX_ATTEMPTS} attempts "
                        "(connection opens, then no data arrives) — try again in a few minutes."
                    ) from exc
            except Exception as exc:
                last_exc = exc
                msg = str(exc).lower()
                if "rate" in msg or "limit" in msg or "429" in msg or "reject" in msg:
                    if attempt == 0:
                        log.warning("[query_notebooklm] rate limited, waiting 20s before one retry (exc: %s)", exc)
                        await asyncio.sleep(20)
                    else:
                        log.warning("[query_notebooklm] rate limited on retry — giving up (exc: %s)", exc)
                        raise RuntimeError(
                            "NotebookLM is rate limiting your account. Wait 15–30 minutes before trying again."
                        ) from exc
                elif any(kw in msg for kw in _TRANSPORT_KEYWORDS):
                    if attempt < _MAX_ATTEMPTS - 1:
                        wait = _TRANSPORT_BACKOFF[min(attempt, len(_TRANSPORT_BACKOFF) - 1)]
                        log.warning(
                            "[query_notebooklm] transport/timeout error (attempt %d/%d), retrying in %ds — %s",
                            attempt + 1, _MAX_ATTEMPTS, wait, exc,
                        )
                        await asyncio.sleep(wait)
                    else:
                        log.warning("[query_notebooklm] transport/timeout error on final attempt — giving up: %s", exc)
                        raise RuntimeError(
                            f"NotebookLM timed out after {_MAX_ATTEMPTS} attempts. "
                            "NotebookLM may be overloaded — try again in a few minutes."
                        ) from exc
                else:
                    if not silent:
                        print(f"ERROR: Query failed — {exc}", file=sys.stderr)
                        sys.exit(1)
                    return None
        else:
            if not silent:
                print(f"ERROR: Query failed after retries — {last_exc}", file=sys.stderr)
                sys.exit(1)
            return None

    answer = getattr(result, "answer", None) or str(result)
    # Strip LaTeX / math notation that NotebookLM sometimes emits
    answer = re.sub(r'\\times', ' x ', answer)          # \times → x
    answer = re.sub(r'\\cdot', ' x ', answer)            # \cdot → x
    answer = re.sub(r'\\text\{([^}]*)\}', r'\1', answer) # \text{foo} → foo
    answer = re.sub(r'\$([^$]+)\$', r'\1', answer)       # $...$ inline math → content
    answer = re.sub(r'\\\$', '$', answer)                 # \$ → $
    answer = re.sub(r'\\%', '%', answer)                  # \% → %
    answer = re.sub(r'\\ ', ' ', answer)                  # \ (escaped space) → space
    answer = re.sub(r'\\,', ' ', answer)                  # \, (thin space) → space

    if not silent:
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

    return answer


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
    python option_dashboard.py --ticker IBM --num 5

  Fetch, save, and upload to NotebookLM:
    python option_dashboard.py --ticker IBM --num 5 --upload

  Skip fetch — upload an existing IBM.csv to NotebookLM:
    python option_dashboard.py --ticker IBM --onlyload

  Ask NotebookLM for the best Covered Call (source already uploaded):
    python option_dashboard.py --ticker IBM --strat CC

  Full pipeline — fetch, upload, then get a CSP recommendation:
    python option_dashboard.py --ticker IBM --num 5 --upload --strat CSP

  Roll analysis — fetch fresh chain, upload, read journal, query for roll/hold:
    python option_dashboard.py --ticker IBM --num 5 --upload --strat ROLL

  Roll analysis using existing CSV (no re-fetch):
    python option_dashboard.py --ticker IBM --onlyload --strat ROLL

  Evaluate ALL open positions for risk signals (delta, DTE, ATM, ITM):
    python option_dashboard.py --eval

  Evaluate only IBM open positions:
    python option_dashboard.py --eval --ticker IBM

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
        required=False,
        metavar="SYMBOL",
        help="Stock ticker symbol, e.g. IBM  (required unless using --eval)",
    )
    parser.add_argument(
        "--eval",
        action="store_true",
        help=(
            "Evaluate all open positions for risk signals: |delta| >= 0.40, "
            f"DTE <= 21, near ATM (within {ATM_THRESHOLD_PCT:.0f}%%), or ITM. "
            "Requires PUBLIC_API_SECRET. Combine with --ticker to filter "
            "to one symbol."
        ),
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help=(
            "Launch a live web dashboard showing all open positions with risk "
            "evaluation. Auto-refreshes every 5 minutes. Requires PUBLIC_API_SECRET. "
            "Opens http://127.0.0.1:5051 in your browser."
        ),
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
    parser.add_argument(
        "--purge-notebook",
        action="store_true",
        help="Remove all {TICKER}.csv sources from the NotebookLM notebook and exit",
    )
    args = parser.parse_args()

    # ── --purge-notebook: remove all ticker CSVs from notebook and exit ───────
    if args.purge_notebook:
        notebook_id = os.environ.get("NOTEBOOKLM_NOTEBOOK_ID")
        if not notebook_id:
            print("ERROR: NOTEBOOKLM_NOTEBOOK_ID not set", file=sys.stderr)
            sys.exit(1)
        print("Removing all {TICKER}.csv sources from notebook...")
        asyncio.run(_purge_all_ticker_sources(notebook_id))
        print("Done.")
        sys.exit(0)

    # ── --eval / --web: require PUBLIC_API_SECRET ─────────────────────────────
    if args.eval or args.web:
        secret = os.environ.get("PUBLIC_API_SECRET")
        if not secret:
            print(
                "ERROR: PUBLIC_API_SECRET is required for --eval / --web.\n"
                "  1. Go to https://public.com (Settings → API)\n"
                "  2. Generate a Secret Token\n"
                "  3. Run:  export PUBLIC_API_SECRET=your_secret_here",
                file=sys.stderr,
            )
            sys.exit(1)
        token = get_access_token(secret)
        account_id = get_account_id(token)

        if args.web:
            run_web_dashboard(token, account_id)
            sys.exit(0)

        # --eval (CLI mode)
        eval_ticker = args.ticker.upper() if args.ticker else None
        eval_open_positions(token, account_id, eval_ticker)
        sys.exit(0)

    # All non-eval modes require --ticker
    if not args.ticker:
        parser.error("--ticker is required unless using --eval or --web")

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

    # Step 7b — Fetch VIX (when --strat is used)
    current_vix = None
    if args.strat:
        print("Fetching VIX...", end=" ", flush=True)
        current_vix = get_vix()
        if current_vix is not None:
            print(f"VIX = {current_vix:.2f}")
        else:
            print("unavailable")

    # Step 8 — Optionally upload to NotebookLM
    _cli_source_id = None
    if not strat_only and (args.upload or args.onlyload):
        _cli_source_id = asyncio.run(upload_to_notebooklm(output_file, notebook_id))
        if not args.onlyload:
            try:
                os.unlink(output_file)
            except OSError:
                pass

    # Step 9 — Optionally query NotebookLM for strategy recommendation
    if args.strat:
        asyncio.run(
            query_notebooklm(
                notebook_id, ticker, args.strat, key_dates, open_positions, current_vix
            )
        )
        asyncio.run(delete_notebooklm_source(notebook_id, _cli_source_id))


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
