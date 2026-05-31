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
import logging
import os
import re
import sys
import textwrap
import threading


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

    # Console handler — WARN and above only
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.WARNING)
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


def get_chain_net_cash(spread_id, fallback_ticker: str | None = None) -> dict:
    """
    Return a breakdown of net cash for all positions sharing the same spread_id.
    If spread_id is NULL, falls back to all positions for fallback_ticker.

    Returns dict with:
        net_cash     : float  — (collected − paid) × 100
        collected    : float  — total sell proceeds × 100
        paid         : float  — total buy costs × 100
        num_positions: int    — number of positions in the chain
        scoped_by    : str    — 'spread_id' or 'symbol' (indicates which filter was used)
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
    elif fallback_ticker:
        where  = "UPPER(p.symbol) = UPPER(?)"
        param  = fallback_ticker
        scoped = "symbol"
        log.debug("get_chain_net_cash: spread_id is NULL, falling back to symbol=%r", fallback_ticker)
    else:
        return {"net_cash": 0.0, "collected": 0.0, "paid": 0.0, "num_positions": 0, "scoped_by": "none"}

    sql = f"""
        SELECT
            SUM(CASE WHEN t.action = 'sell' THEN  t.price * t.quantity ELSE 0 END) AS gross_collected,
            SUM(CASE WHEN t.action = 'buy'  THEN  t.price * t.quantity ELSE 0 END) AS gross_paid,
            COUNT(DISTINCT p.id) AS num_positions
        FROM positions p
        JOIN trades t ON t.position_id = p.id
        WHERE {where}
          AND t.is_test = 0
    """
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = con.execute(sql, (param,)).fetchone()
        con.close()
        collected = round(float(row[0] or 0.0) * 100, 2)
        paid      = round(float(row[1] or 0.0) * 100, 2)
        n_pos     = int(row[2] or 0)
        log.debug("get_chain_net_cash: collected=%.2f paid=%.2f positions=%d", collected, paid, n_pos)
        return {
            "net_cash":      round(collected - paid, 2),
            "collected":     collected,
            "paid":          paid,
            "num_positions": n_pos,
            "scoped_by":     scoped,
        }
    except sqlite3.OperationalError as exc:
        log.error("get_chain_net_cash query failed: %s", exc)
        return {"net_cash": 0.0, "collected": 0.0, "paid": 0.0, "num_positions": 0, "scoped_by": "error"}


# Cache underlying prices within a session to avoid redundant yfinance calls
_price_cache: dict[str, float | None] = {}


def get_underlying_price(ticker: str) -> float | None:
    """Return the current underlying stock price via yfinance (cached per run)."""
    if ticker in _price_cache:
        return _price_cache[ticker]
    price: float | None = None
    try:
        import yfinance as yf
        fi = yf.Ticker(ticker).fast_info
        price = float(fi.last_price) if getattr(fi, "last_price", None) else None
        if not price:
            info = yf.Ticker(ticker).info
            raw = (
                info.get("currentPrice")
                or info.get("regularMarketPrice")
                or info.get("previousClose")
            )
            price = float(raw) if raw else None
    except Exception:
        pass
    _price_cache[ticker] = price
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

    # ── Per-ticker caches (underlying price + ATR) ────────────────────────────
    price_cache_local: dict[str, float | None] = {}
    atr_cache: dict[str, tuple[float | None, float | None]] = {}

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

        osi = build_osi_symbol(sym, expiry, opt_type, strike).replace(" ", "")

        # Delta from greeks endpoint
        greeks = greeks_data.get(osi, {})
        delta: float | None = _to_f(greeks.get("delta"))

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
            abs_pnl = (
                (avg_price - current_price) if net_qty > 0 else (current_price - avg_price)
            ) * 100 * qty
            pct_pnl = abs_pnl / (avg_price * 100 * qty) * 100

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

        # ── Condition 4: Near ATM ─────────────────────────────────────────────
        if underlying is not None:
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
                        f"Within ATR buffer (put): "
                        f"u/l ${underlying:.2f} − strike ${strike:.2f} "
                        f"= ${gap:.2f}  <  buffer ${buffer:.2f}  "
                        f"(1.5 × ATR ${atr_val:.2f})"
                    )
            elif opt_type == "CALL":
                gap = strike - underlying
                if gap < buffer:
                    reasons.append(
                        f"Within ATR buffer (call): "
                        f"strike ${strike:.2f} − u/l ${underlying:.2f} "
                        f"= ${gap:.2f}  <  buffer ${buffer:.2f}  "
                        f"(1.5 × ATR ${atr_val:.2f})"
                    )

        result_positions.append({
            **pos,
            "dte": dte,
            "delta": delta,
            "underlying": underlying,
            "atr": atr_val,
            "buffer": buffer,
            "current_price": current_price,
            "abs_pnl": abs_pnl,
            "pct_pnl": pct_pnl,
            "reasons": reasons,
            "flagged": bool(reasons),
        })

    flagged_count = sum(1 for p in result_positions if p["flagged"])
    vix = get_vix()
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


def _parse_recommended_option(text: str, chain_rows: list[dict]) -> dict | None:
    """
    Try to extract the recommended strike and expiry from NotebookLM response text
    and return the matching chain row, or None if not found.
    """
    # Extract strike: $12.50, $12, 12.50 strike, strike of 12.50, etc.
    strike_match = re.search(
        r'\$\s*(\d+(?:\.\d+)?)\s*strike'
        r'|strike\s+(?:of\s+|price\s+(?:of\s+)?)?\$?\s*(\d+(?:\.\d+)?)'
        r'|\$\s*(\d+(?:\.\d+)?)\s+(?:put|call|strike)'
        r'|\b(\d+(?:\.\d+)?)\s+(?:call|put)\b',
        text, re.IGNORECASE
    )
    if not strike_match:
        return None
    strike_str = next(g for g in strike_match.groups() if g is not None)
    try:
        target_strike = float(strike_str)
    except ValueError:
        return None

    # Extract expiry: YYYY-MM-DD, or Month DD YYYY, or Month DD, YYYY
    date_match = re.search(
        r'(\d{4}-\d{2}-\d{2})'
        r'|(\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?'
        r'|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
        r'\s+\d{1,2},?\s+\d{4})',
        text, re.IGNORECASE
    )
    target_expiry = None
    if date_match:
        raw_date = date_match.group(1) or date_match.group(2)
        for fmt in ("%Y-%m-%d", "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"):
            try:
                target_expiry = datetime.datetime.strptime(raw_date.replace(",", ", ").strip(), fmt).strftime("%Y-%m-%d")
                break
            except ValueError:
                continue

    # Find best matching row: exact strike + expiry if possible, else nearest strike
    best = None
    best_dist = float("inf")
    for row in chain_rows:
        try:
            row_strike = float(row.get("strike") or 0)
        except (TypeError, ValueError):
            continue
        dist = abs(row_strike - target_strike)
        expiry_match = (target_expiry is None or row.get("expiry") == target_expiry)
        # Prefer exact expiry match; use distance as tiebreaker
        score = dist + (0 if expiry_match else 1000)
        if score < best_dist:
            best_dist = score
            best = row
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


def _detect_recommendation(text: str) -> str:
    """Parse a NotebookLM ROLL response and return ROLL, HOLD, or ASSIGNMENT."""
    low = text.lower()
    if re.search(r"accept.{0,20}assignment|take.{0,20}assignment|let.{0,10}assign|allow.{0,10}assignment", low):
        return "ASSIGNMENT"
    # Check for explicit "do nothing" / hold before checking for "roll"
    # (a roll recommendation will often mention "do nothing" as the rejected option)
    first_heading_match = re.search(
        r"(?:recommendation|strategy|action)[:\s]+([^\n.]+)", low
    )
    if first_heading_match:
        snippet = first_heading_match.group(1)
        if re.search(r"\bdo nothing\b|\bhold\b|\bno action\b|\bno roll\b", snippet):
            return "HOLD"
        if re.search(r"\broll\b", snippet):
            return "ROLL"
        if re.search(r"\bassignment\b", snippet):
            return "ASSIGNMENT"
    # Fallback: count occurrences
    roll_n   = len(re.findall(r"\broll(?:ing|ed)?\b", low))
    do_n     = len(re.findall(r"\bdo nothing\b|\bhold\b", low))
    assign_n = len(re.findall(r"\bassignment\b", low))
    if assign_n > roll_n and assign_n > do_n:
        return "ASSIGNMENT"
    if roll_n >= do_n:
        return "ROLL"
    return "HOLD"


async def run_roll_for_position(
    token: str,
    account_id: str,
    ticker: str,
    open_positions: list[dict],
    notebook_id: str,
    pos_key: str | None = None,
    num_expirations: int = 10,
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
                "error": f"Option chain returned no contracts for {ticker} — "
                         "data may be unavailable or markets may be closed.",
                "recommendation": "HOLD",
                "text": "",
                "ticker": ticker,
            }

        output_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), f"{ticker.upper()}.csv"
        )
        with open(output_file, "w", newline="") as tmp:
            writer = csv.DictWriter(tmp, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(all_rows)

        try:
            await upload_to_notebooklm(output_file, notebook_id)
        finally:
            try:
                os.unlink(output_file)
            except OSError:
                pass

        key_dates = get_key_dates(ticker)
        vix = get_vix()

        text = await query_notebooklm(
            notebook_id, ticker, "ROLL", key_dates, open_positions, vix,
            silent=True,
        )
        if not text:
            return {"error": "Empty response from NotebookLM", "recommendation": "HOLD", "text": "", "ticker": ticker}

        # Resolve spread_id from the open position matching this pos_key.
        # Strike is a float in the DB but a bare number string in the JS pos_key,
        # so compare as floats to avoid "5.0" != "5" mismatches.
        spread_id = None
        if pos_key:
            pk_parts = pos_key.split("|")  # symbol|option_type|strike|expiry
            pk_sym    = pk_parts[0].upper()  if len(pk_parts) > 0 else ""
            pk_type   = pk_parts[1].upper()  if len(pk_parts) > 1 else ""
            pk_expiry = pk_parts[3]          if len(pk_parts) > 3 else ""
            try:
                pk_strike = float(pk_parts[2]) if len(pk_parts) > 2 else None
            except ValueError:
                pk_strike = None
            matched_pos = None
            for p in open_positions:
                try:
                    p_strike = float(p.get("strike", ""))
                except (TypeError, ValueError):
                    p_strike = None
                if (str(p.get("symbol","")).upper() == pk_sym
                        and str(p.get("option_type","")).upper() == pk_type
                        and p_strike == pk_strike
                        and str(p.get("expiry","")) == pk_expiry):
                    spread_id  = p.get("spread_id")
                    matched_pos = p
                    log.debug("Matched pos_key=%r → spread_id=%r", pos_key, spread_id)
                    break
            else:
                log.warning("No position matched pos_key=%r in open_positions", pos_key)
        chain = get_chain_net_cash(spread_id, fallback_ticker=ticker)

        # Expected PnL: close current position at 40% of sale price (keep 60%)
        pos_avg_price = float(matched_pos["avg_price"]) if matched_pos and matched_pos.get("avg_price") is not None else None
        pos_qty       = abs(int(matched_pos["net_qty"])) if matched_pos and matched_pos.get("net_qty") is not None else None
        return {
            "recommendation": _detect_recommendation(text),
            "text": text,
            "ticker": ticker,
            "chain_cash":      chain["net_cash"],       # for dashboard badge
            "chain_collected": chain["collected"],
            "chain_paid":      chain["paid"],
            "chain_positions": chain["num_positions"],
            "pos_avg_price":   pos_avg_price,
            "pos_qty":         pos_qty,
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
    num_expirations: int = 10,
) -> dict:
    """
    CC/CSP analysis for a ticker with no existing open position ('unborn').
      1. Fetch & write option chain CSV
      2. Upload to NotebookLM
      3. Query NotebookLM with CC or CSP strategy
    Returns dict: {recommendation, text, ticker, strat, error}
    """
    try:
        all_expirations = get_expirations(token, account_id, ticker)
        if not all_expirations:
            return {
                "error": f"No option expirations found for {ticker} on Public.com.",
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

        output_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), f"{ticker.upper()}.csv"
        )
        with open(output_file, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(all_rows)

        try:
            await upload_to_notebooklm(output_file, notebook_id)
        finally:
            try:
                os.unlink(output_file)
            except OSError:
                pass

        key_dates = get_key_dates(ticker)
        vix = get_vix()

        # Synthetic position representing the intended trade size
        synthetic_positions = [{"symbol": ticker, "option_type": strat, "net_qty": -qty,
                                 "avg_price": None, "strike": None, "expiry": None}]

        text = await query_notebooklm(
            notebook_id, ticker, strat, key_dates, synthetic_positions, vix,
            silent=True,
        )
        if not text:
            return {"error": "Empty response from NotebookLM", "recommendation": "HOLD",
                    "text": "", "ticker": ticker, "strat": strat, "chain": display_rows}

        log.info("[unborn] display_rows count: %d, first: %s", len(display_rows), display_rows[0] if display_rows else None)
        rec = _detect_recommendation(text)
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
            "ticker": ticker,
            "strat": strat,
            "chain": [chosen] if chosen else [],
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
<title>Portfolio Eval</title>
<style>
  :root {
    --bg: #0f1117; --surface: #1a1d27; --border: #2a2d3a;
    --text: #e2e8f0; --muted: #8892a4; --accent: #4f8ef7;
    --ok: #22c55e; --warn: #f59e0b; --danger: #ef4444;
    --ok-bg: #052e16; --warn-bg: #451a03; --danger-bg: #450a0a;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'SF Mono', 'Fira Code', monospace; font-size: 13px; }
  header { background: var(--surface); border-bottom: 1px solid var(--border); padding: 14px 24px; display: flex; align-items: center; gap: 24px; flex-wrap: wrap; }
  header h1 { font-size: 16px; font-weight: 600; color: var(--accent); letter-spacing: 0.05em; }
  .stat { display: flex; flex-direction: column; gap: 2px; }
  .stat-label { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; }
  .stat-value { font-size: 15px; font-weight: 600; }
  .ok    { color: var(--ok); }
  .warn  { color: var(--warn); }
  .danger { color: var(--danger); }
  .muted { color: var(--muted); }
  .actions { margin-left: auto; display: flex; align-items: center; gap: 12px; }
  button { background: var(--accent); color: #fff; border: none; border-radius: 6px; padding: 6px 14px; cursor: pointer; font-size: 12px; font-family: inherit; }
  button:hover { opacity: 0.85; }
  #countdown { font-size: 11px; color: var(--muted); }
  main { padding: 20px 24px; }
  .section-title { font-size: 11px; text-transform: uppercase; letter-spacing: 0.1em; color: var(--muted); margin-bottom: 10px; margin-top: 20px; }
  table { width: 100%; border-collapse: collapse; }
  th { background: var(--surface); color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--border); cursor: pointer; user-select: none; white-space: nowrap; position: sticky; top: 0; z-index: 10; }
  th:hover { color: var(--text); }
  th.sorted-asc::after  { content: " ▲"; }
  th.sorted-desc::after { content: " ▼"; }
  td { padding: 8px 10px; border-bottom: 1px solid var(--border); vertical-align: top; white-space: nowrap; }
  tr.ok-row   td { background: transparent; }
  tr.warn-row td { background: #1f1a0a; }
  tr.danger-row td { background: #1f0a0a; }
  tr:hover td { filter: brightness(1.15); }
  .badge { display: inline-block; border-radius: 4px; padding: 2px 7px; font-size: 10px; font-weight: 600; letter-spacing: 0.05em; }
  .badge-ok     { background: var(--ok-bg);   color: var(--ok);   }
  .badge-warn   { background: var(--warn-bg); color: var(--warn); }
  .badge-danger { background: var(--danger-bg); color: var(--danger); }
  .reasons { font-size: 11px; color: var(--muted); white-space: normal; max-width: 360px; }
  .err-tip { position: relative; display: inline-block; cursor: default; }
  .err-tip .err-msg {
    display: none; position: absolute; z-index: 100; bottom: calc(100% + 6px); left: 50%;
    transform: translateX(-50%); background: #1e1e2e; color: #f87171; border: 1px solid #f87171;
    border-radius: 6px; padding: 7px 10px; font-size: 11px; white-space: pre-wrap;
    max-width: 340px; min-width: 140px; word-break: break-word; box-shadow: 0 4px 16px #0008;
    pointer-events: none;
  }
  .err-tip:hover .err-msg { display: block; }
  .reasons li { margin-top: 3px; }
  .pnl-pos { color: var(--ok); }
  .pnl-neg { color: var(--danger); }
  .skipped-box { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px 16px; margin-top: 16px; }
  .skipped-box li { color: var(--muted); font-size: 12px; margin-top: 4px; }
  .unborn-bar { display:flex; align-items:center; gap:10px; background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:10px 14px; margin-bottom:18px; flex-wrap:wrap; }
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
  #spinner { display: none; font-size: 11px; color: var(--muted); margin-left: 10px; }
  #spinner.active { display: inline; }
  #error-bar { display: none; background: var(--danger-bg); color: var(--danger); padding: 8px 16px; font-size: 12px; border-bottom: 1px solid var(--danger); }
</style>
</head>
<body>
<div id="error-bar"></div>
<header>
  <h1>&#9660; Portfolio Eval</h1>
  <div class="stat"><span class="stat-label">Total</span><span class="stat-value" id="h-total">—</span></div>
  <div class="stat"><span class="stat-label">Flagged</span><span class="stat-value" id="h-flagged">—</span></div>
  <div class="stat"><span class="stat-label">VIX</span><span class="stat-value" id="h-vix">—</span></div>
  <div class="stat"><span class="stat-label">As of</span><span class="stat-value muted" id="h-time" style="font-size:12px">—</span></div>
  <div class="actions">
    <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px;color:var(--muted)">
      <input type="checkbox" id="collapse-ok" onchange="applyCollapseOk()" style="cursor:pointer;accent-color:var(--accent)">
      Collapse OK
    </label>
    <span id="spinner">&#8635; refreshing…</span>
    <button onclick="fetchData()">&#8635; Refresh</button>
  </div>
</header>
<main>
  <div class="unborn-bar">
    <label for="ub-ticker">Ticker</label>
    <input type="text" id="ub-ticker" placeholder="IBM" maxlength="10" style="text-transform:uppercase">
    <label for="ub-qty">Qty</label>
    <input type="number" id="ub-qty" placeholder="1" min="1" value="1" style="width:60px">
    <label for="ub-strat">Strategy</label>
    <select id="ub-strat">
      <option value="CC" selected>CC</option>
      <option value="CSP">CSP</option>
    </select>
    <button onclick="findUnborn()">Find</button>
    <span id="unborn-result"></span>
  </div>
  <div id="unborn-chain"></div>
  <div class="section-title">Open Positions</div>
  <table id="pos-table">
    <thead>
      <tr>
        <th onclick="sortTable(0)">Symbol</th>
        <th onclick="sortTable(1)">Type</th>
        <th onclick="sortTable(2)">Strike</th>
        <th onclick="sortTable(3)">Expiry</th>
        <th onclick="sortTable(4)">Side</th>
        <th onclick="sortTable(5)">Qty</th>
        <th onclick="sortTable(6)">DTE</th>
        <th onclick="sortTable(7)">Δ</th>
        <th onclick="sortTable(8)">U/L Price</th>
        <th onclick="sortTable(9)">Opt Price</th>
        <th onclick="sortTable(10)">PnL $</th>
        <th onclick="sortTable(11)">PnL %</th>
        <th>Status / Flags</th>
        <th>Action<br><button onclick="resetRecommendations()" style="font-size:10px;padding:2px 7px;margin-top:4px;font-weight:normal">Reset</button></th>
      </tr>
    </thead>
    <tbody id="pos-body"></tbody>
  </table>
  <div id="skipped-section"></div>
</main>
<script>
let _data = [];
let _sortCol = 3, _sortDir = 1;

async function fetchData() {
  document.getElementById('spinner').classList.add('active');
  try {
    const r = await fetch('/api/eval');
    if (!r.ok) throw new Error(await r.text());
    const d = await r.json();
    document.getElementById('error-bar').style.display = 'none';
    applyData(d);
  } catch(e) {
    const bar = document.getElementById('error-bar');
    bar.textContent = 'Refresh failed: ' + e.message;
    bar.style.display = 'block';
  } finally {
    document.getElementById('spinner').classList.remove('active');
  }
}

function applyData(d) {
  _data = d.positions || [];
  // Restore any server-cached analysis results so badges survive refresh
  const serverRecs = d.cached_recommendations || {};
  let changed = false;
  for (const [key, val] of Object.entries(serverRecs)) {
    if (!_recommendations[key] && val.recommendation) {
      _recommendations[key] = {rec: val.recommendation};
      changed = true;
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
}

function fmt(v, digits, prefix='') {
  return v == null ? '—' : prefix + v.toFixed(digits);
}

function renderTable() {
  const rows = [..._data];
  rows.sort((a, b) => {
    const cols = [
      r => r.symbol, r => r.option_type, r => parseFloat(r.strike||0),
      r => r.expiry, r => (r.net_qty > 0 ? 'Short' : 'Long'),
      r => Math.abs(r.net_qty||0), r => r.dte??999, r => r.delta??-99,
      r => r.underlying??0, r => r.current_price??0,
      r => r.abs_pnl??-999999, r => r.pct_pnl??-999999,
    ];
    const fn = cols[_sortCol] || (r => 0);
    const av = fn(a), bv = fn(b);
    return _sortDir * (av < bv ? -1 : av > bv ? 1 : 0);
  });

  const tbody = document.getElementById('pos-body');
  tbody.innerHTML = '';
  for (const p of rows) {
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
    const pnlAbs = p.abs_pnl;
    const pnlPct = p.pct_pnl;
    const pnlAbsStr = pnlAbs == null ? '—'
      : '<span class="' + (pnlAbs>=0?'pnl-pos':'pnl-neg') + '">'
        + (pnlAbs>=0?'+':'') + '$' + pnlAbs.toFixed(2).replace(/\\B(?=(\\d{3})+(?!\\d))/g,',') + '</span>';
    const pnlPctStr = pnlPct == null ? '—'
      : '<span class="' + (pnlPct>=0?'pnl-pos':'pnl-neg') + '">'
        + (pnlPct>=0?'+':'') + pnlPct.toFixed(1) + '%</span>';

    const reasons = (p.reasons||[]).length === 0 ? '' :
      '<ul class="reasons">' + p.reasons.map(r => '<li>' + esc(r) + '</li>').join('') + '</ul>';

    const posKey = [p.symbol, p.option_type, String(p.strike||''), p.expiry||''].join('|');
    const cached = _recommendations[posKey];
    const actionCell = cached
      ? recBadge(cached.rec, posKey, cached.chainCash)
      : `<button onclick="analyzePosition('${posKey.replace(/'/g,"\\'")}', this)" style="font-size:11px;padding:4px 10px">Analyze</button>`;
    const actionTdAttr = ` data-poskey="${posKey.replace(/"/g,'&quot;')}"`;


    const tr = document.createElement('tr');
    tr.className = rowCls;
    tr.innerHTML = `
      <td><b>${esc(p.symbol)}</b></td>
      <td>${esc((p.option_type||'').toUpperCase())}</td>
      <td>$${parseFloat(p.strike||0).toFixed(2)}</td>
      <td>${esc(p.expiry||'')}</td>
      <td>${side}</td>
      <td>${qty}</td>
      <td>${p.dte??'—'}</td>
      <td>${p.delta!=null ? (p.delta>=0?'+':'')+p.delta.toFixed(3) : '—'}</td>
      <td>${p.underlying!=null ? '$'+p.underlying.toFixed(2) : '—'}</td>
      <td>${p.current_price!=null ? '$'+p.current_price.toFixed(2) : '—'}</td>
      <td>${pnlAbsStr}</td>
      <td>${pnlPctStr}</td>
      <td>${badge}${reasons}</td>
      <td${actionTdAttr}>${actionCell}</td>`;
    tbody.appendChild(tr);
  }
  applyCollapseOk();
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

// Persist recommendations across browser refreshes via localStorage
const _LS_KEY = 'optionsRecs';
function _loadRecs() {
  try { return JSON.parse(localStorage.getItem(_LS_KEY) || '{}'); } catch { return {}; }
}
function _saveRecs(recs) {
  try { localStorage.setItem(_LS_KEY, JSON.stringify(recs)); } catch {}
}
const _recommendations = _loadRecs(); // posKey -> {rec}

// Proposed trades — server-side persistence via /api/unborn-rows
const _unbornRows = {};   // populated on load from server

async function _loadUnbornFromServer() {
  try {
    const r = await fetch('/api/unborn-rows');
    if (!r.ok) return;
    const d = await r.json();
    Object.assign(_unbornRows, d);
    _renderUnbornTable();
  } catch(e) { /* silently ignore */ }
}

async function _saveUnbornToServer() {
  try {
    await fetch('/api/unborn-rows', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(_unbornRows)
    });
  } catch(e) { /* silently ignore */ }
}

function _renderUnbornTable() {
  const el = document.getElementById('unborn-chain');
  const keys = Object.keys(_unbornRows).sort();
  if (!keys.length) { el.innerHTML = ''; return; }
  const fv = (v, d, p='') => v == null ? '—' : p + parseFloat(v).toFixed(d);
  const rows = keys.map(k => {
    const c = _unbornRows[k];
    const qty = c._qty || 1;
    const detailHref = c._ubKey ? `/unborn/${c._ubKey}` : null;
    const symCell = detailHref
      ? `<a href="${detailHref}" target="_blank" style="color:var(--accent);text-decoration:none"><b>${esc(c.symbol)}</b></a>`
      : `<b>${esc(c.symbol)}</b>`;
    return `<tr>
      <td>${symCell}</td>
      <td>${esc((c.option_type||'').toUpperCase())}</td>
      <td>${fv(c.strike,2,'$')}</td>
      <td>${esc(c.expiry||'—')}</td>
      <td>${esc(c.side||'Short')}</td>
      <td>${qty}</td>
      <td>${c.dte??'—'}</td>
      <td>${c.delta!=null?(c.delta>=0?'+':'')+parseFloat(c.delta).toFixed(3):'—'}</td>
      <td>${fv(c.ul_price,2,'$')}</td>
      <td>${fv(c.opt_price,2,'$')}</td>
      <td style="color:var(--muted);font-size:11px;white-space:normal;max-width:130px">${esc(c.ideal_entry||'—')}</td>
      <td><button data-key="${esc(k)}" onclick="placeTrade(this.dataset.key,this)" style="font-size:11px;padding:3px 8px;background:var(--accent);color:#fff;border:none;border-radius:4px;cursor:pointer">Trade</button></td>
    </tr>`;
  }).join('');
  el.innerHTML = `<table>
    <thead><tr>
      <th>Symbol</th><th>Type</th><th>Strike</th><th>Expiry</th>
      <th>Side</th><th>Qty</th><th>DTE</th><th>&Delta;</th>
      <th>U/L Price</th><th>Opt Price</th><th>Ideal Entry</th><th></th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

function recBadge(rec, key, chainCash) {
  const cls = rec === 'ROLL' ? 'warn' : rec === 'ASSIGNMENT' ? 'danger' : 'ok';
  const label = rec === 'HOLD' ? 'HOLD' : rec;
  const cashLine = (rec === 'ROLL' && chainCash != null)
    ? `<div style="font-size:10px;margin-top:3px;color:var(--${chainCash >= 0 ? 'ok' : 'danger'})">`
      + `Net chain: ${chainCash >= 0 ? '+' : ''}$${chainCash.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2})}</div>`
    : '';
  return `<div style="display:inline-block;text-align:center">
    <a href="/analyze/${encodeURIComponent(key)}" target="_blank"
      class="badge badge-${cls}" style="text-decoration:none;cursor:pointer">${label}</a>${cashLine}
  </div>`;
}

function _actionCell(key) {
  return document.querySelector(`[data-poskey]`) &&
    [...document.querySelectorAll('[data-poskey]')].find(el => el.dataset.poskey === key);
}

async function analyzePosition(key, btn) {
  btn.disabled = true;
  btn.textContent = '⟳ Analyzing…';
  console.log('[analyze] starting:', key);
  try {
    let d;
    // Poll until the server finishes (handles concurrent in-progress queries)
    while (true) {
      const r = await fetch('/api/analyze', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({position_key: key})
      });
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
    _recommendations[key] = {rec: d.recommendation, chainCash: d.chain_cash ?? null};
    _saveRecs(_recommendations);
    const cell = _actionCell(key);
    if (cell) cell.innerHTML = recBadge(d.recommendation, key, d.chain_cash ?? null);
  } catch(e) {
    console.error('[analyze] error for', key, ':', e.message);
    const cell = _actionCell(key);
    if (cell) {
      cell.innerHTML = `<span class="err-tip" style="color:var(--danger);font-size:11px">&#9888; Error<span class="err-msg">${esc(e.message)}</span></span>
        <button onclick="analyzePosition('${key.replace(/'/g,"\\'")}', this)" style="font-size:10px;padding:2px 6px;margin-left:4px">Retry</button>`;
    } else {
      btn.disabled = false;
      btn.textContent = 'Retry';
      btn.title = e.message;
      btn.style.color = 'var(--danger)';
    }
  }
}

async function findUnborn() {
  const ticker   = document.getElementById('ub-ticker').value.trim().toUpperCase();
  const qty      = parseInt(document.getElementById('ub-qty').value) || 1;
  const strat    = document.getElementById('ub-strat').value;
  const resultEl = document.getElementById('unborn-result');
  if (!ticker) { resultEl.innerHTML = '<span style="color:var(--danger);font-size:12px">Enter a ticker.</span>'; return; }

  resultEl.innerHTML = '<span style="color:var(--muted);font-size:12px">⟳ Analyzing…</span>';
  try {
    const r = await fetch('/api/unborn', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ticker, qty, strat})
    });
    let raw, d;
    try { raw = await r.text(); d = JSON.parse(raw); }
    catch { throw new Error('Server returned non-JSON: ' + (raw||'').slice(0,200)); }
    if (d.error) throw new Error(d.error);

    // Recommendation badge
    const cls   = d.recommendation === 'ROLL' ? 'warn' : d.recommendation === 'ASSIGNMENT' ? 'danger' : 'ok';
    const ubKey = encodeURIComponent(ticker + '|' + strat + '|' + qty);
    resultEl.innerHTML = `<a href="/unborn/${ubKey}" target="_blank"
      class="badge badge-${cls}" style="text-decoration:none;cursor:pointer;font-size:12px;padding:4px 10px">
      Details</a>`;

    // Accumulate proposed trades — add/update this ticker|strat entry, keep others
    const chain = d.chain || [];
    if (chain.length) {
      const row = Object.assign({}, chain[0], {_qty: qty, _ubKey: ubKey});
      _unbornRows[ticker + '|' + strat] = row;
      await _saveUnbornToServer();
      _renderUnbornTable();
      // Clear inputs after successful display
      document.getElementById('ub-ticker').value = '';
      document.getElementById('ub-qty').value = '1';
      document.getElementById('ub-strat').value = 'CC';
      resultEl.innerHTML = '';
    }
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
      _showTradeModal(d.trade);
    } else {
      throw new Error(d.error || 'Unknown error');
    }
  } catch(e) {
    btn.textContent = 'Err';
    btn.style.background = 'var(--danger)';
    btn.title = e.message;
    btn.disabled = false;
  }
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
      <span onclick="document.getElementById('trade-modal').remove()" style="cursor:pointer;color:#fff;font-size:20px;line-height:1;padding:0 2px">&times;</span>
    </div>
    <div style="padding:14px 16px">
      <table style="border-collapse:collapse;width:100%">
        <tr><td style="color:var(--muted);padding:5px 14px 5px 0;white-space:nowrap">1. Action</td>
            <td><b style="color:var(--ok)">Sell to Open</b></td></tr>
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

function resetRecommendations() {
  if (!confirm('Reset all action statuses back to Analyze?')) return;
  Object.keys(_recommendations).forEach(k => delete _recommendations[k]);
  _saveRecs({});
  renderTable();
}

function sortTable(col) {
  const ths = document.querySelectorAll('th');
  ths.forEach(th => th.classList.remove('sorted-asc','sorted-desc'));
  if (_sortCol === col) { _sortDir *= -1; }
  else { _sortCol = col; _sortDir = 1; }
  ths[col].classList.add(_sortDir === 1 ? 'sorted-asc' : 'sorted-desc');
  renderTable();
}

// Sort by expiry by default (col 5, ascending)
document.querySelectorAll('th')[3].classList.add('sorted-asc');
_loadUnbornFromServer();
fetchData();
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


def _osascript_js(js: str) -> str:
    """Wrap JS in an osascript AppleScript tell-block for Chrome's front tab."""
    safe = js.replace("\\", "\\\\").replace('"', '\\"')
    return f'tell application "Google Chrome" to execute front window\'s active tab javascript "{safe}"'


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

    # Wait for expiry/strike dropdowns to populate after symbol confirmation
    _time.sleep(3.0)

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
    _time.sleep(0.5)

    # ── 6. Expiry dropdown ──
    if expiry_label:
        _run_js(_click_id("exp_dropdown-0"), "open-expiry-dropdown")
        _time.sleep(1.5)
        _run_js(_click_dropdown_option(expiry_label), "select-expiry")
        _time.sleep(0.5)

    # ── 7. Strike dropdown ──
    if strike_label:
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


def run_web_dashboard(token: str, account_id: str) -> None:
    """Start a Flask web dashboard showing all open positions with live data."""
    setup_logging()

    try:
        from flask import Flask, Response, request as flask_request
    except ImportError:
        log.error("Flask is required for --web mode. Install with: pip install flask")
        sys.exit(1)

    import html as html_mod
    import json
    import urllib.parse
    import webbrowser
    import threading

    app = Flask(__name__)
    app.config["JSON_SORT_KEYS"] = False

    # In-memory cache: posKey -> {recommendation, text, ticker, error}
    _analysis_cache:    dict[str, dict] = {}   # posKey -> result
    _analysis_inflight: set[str]        = set() # posKeys currently being queried
    _cache_lock = threading.Lock()

    def _serial(obj):
        if obj is None:
            return None
        raise TypeError(f"Object of type {type(obj)} is not JSON serialisable")

    @app.route("/")
    def index():
        return Response(_WEB_DASHBOARD_HTML, mimetype="text/html")

    @app.route("/api/eval")
    def api_eval():
        data = get_eval_data(token, account_id, ticker=None, verbose=False)
        # Include server-side analysis cache so the client can restore
        # recommendation badges after a manual refresh
        with _cache_lock:
            data["cached_recommendations"] = {
                k: {"recommendation": v.get("recommendation"), "error": v.get("error")}
                for k, v in _analysis_cache.items()
                if not v.get("error")
            }
        return Response(json.dumps(data, default=_serial), mimetype="application/json")

    @app.route("/api/analyze", methods=["POST"])
    def api_analyze():
        body = flask_request.get_json(force=True, silent=True) or {}
        pos_key = body.get("position_key", "")
        if not pos_key:
            return Response(json.dumps({"error": "Missing position_key"}), status=400, mimetype="application/json")

        with _cache_lock:
            # Already have a result — return it immediately
            if pos_key in _analysis_cache:
                return Response(json.dumps(_analysis_cache[pos_key]), mimetype="application/json")
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
            return Response(json.dumps({"error": f"Bad position_key: {pos_key}"}), status=400, mimetype="application/json")
        sym, opt_type, strike_str, expiry = parts
        ticker = sym.upper()

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
            _analysis_cache[pos_key] = result
        return Response(json.dumps(result, default=_serial), mimetype="application/json")

    # ── Unborn routes ──────────────────────────────────────────────────────────
    _unborn_cache: dict[str, dict] = {}   # "{TICKER}|{STRAT}|{QTY}" -> result
    _unborn_inflight: set[str] = set()

    @app.route("/api/unborn", methods=["POST"])
    def api_unborn():
        body   = flask_request.get_json(force=True, silent=True) or {}
        ticker = body.get("ticker", "").upper().strip()
        qty    = int(body.get("qty") or 1)
        strat  = body.get("strat", "CC").upper()
        if not ticker:
            return Response(json.dumps({"error": "Missing ticker"}), status=400, mimetype="application/json")
        if strat not in ("CC", "CSP"):
            return Response(json.dumps({"error": f"Unknown strat: {strat}"}), status=400, mimetype="application/json")

        ub_key = f"{ticker}|{strat}|{qty}"

        with _cache_lock:
            if ub_key in _unborn_cache:
                return Response(json.dumps(_unborn_cache[ub_key]), mimetype="application/json")
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

        log.info("[unborn] Running %s analysis for %s qty=%d", strat, ticker, qty)
        try:
            result = asyncio.run(
                run_unborn_for_ticker(fresh_token, fresh_account_id, ticker, qty, strat, notebook_id)
            )
            if result.get("error"):
                log.error("[unborn] Error for %s: %s", ticker, result["error"])
            else:
                log.info("[unborn] %s → %s", ticker, result.get("recommendation"))
        except Exception as exc:
            log.exception("[unborn] Unhandled exception for %s", ticker)
            result = {"error": str(exc), "recommendation": "HOLD", "text": "", "ticker": ticker, "strat": strat}
        finally:
            with _cache_lock:
                _unborn_inflight.discard(ub_key)

        with _cache_lock:
            _unborn_cache[ub_key] = result
        return Response(json.dumps(result, default=_serial), mimetype="application/json")

    _UNBORN_ROWS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "unborn_rows.json")

    @app.route("/api/unborn-rows", methods=["GET"])
    def api_unborn_rows_get():
        try:
            if os.path.exists(_UNBORN_ROWS_FILE):
                with open(_UNBORN_ROWS_FILE, "r", encoding="utf-8") as f:
                    return Response(f.read(), mimetype="application/json")
        except Exception as exc:
            log.warning("[unborn-rows] Read error: %s", exc)
        return Response("{}", mimetype="application/json")

    @app.route("/api/unborn-rows", methods=["POST"])
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

    @app.route("/api/trade", methods=["POST"])
    def api_trade():
        trade = flask_request.get_json(force=True) or {}
        log.info("[trade] Received trade request: %s", trade)
        t = threading.Thread(target=_launch_fidelity_trade, args=(trade,), daemon=True)
        t.start()
        return Response(json.dumps({"status": "launched", "trade": trade}), mimetype="application/json")

    @app.route("/unborn/<path:ub_key>")
    def unborn_detail(ub_key: str):
        ub_key = urllib.parse.unquote(ub_key)
        cached = _unborn_cache.get(ub_key)
        parts  = ub_key.split("|")   # TICKER|STRAT|QTY
        title_str = html_mod.escape(" · ".join(parts))

        if not cached:
            body_html = "<p style='color:var(--muted)'>Analysis not found. Click <b>Find</b> first.</p>"
        elif cached.get("error"):
            body_html = f"<p style='color:var(--danger)'>Error: {html_mod.escape(cached['error'])}</p>"
        else:
            rec      = cached.get("recommendation", "")
            text     = cached.get("text", "")
            rec_cls  = {"ROLL": "warn", "ASSIGNMENT": "danger", "HOLD": "ok"}.get(rec, "ok")
            lines_out = []
            for ln in text.splitlines():
                ln_esc = html_mod.escape(ln)
                if ln_esc.startswith("## "):   lines_out.append(f"<h2>{ln_esc[3:]}</h2>")
                elif ln_esc.startswith("# "): lines_out.append(f"<h1>{ln_esc[2:]}</h1>")
                elif ln_esc.startswith("- ") or ln_esc.startswith("• "): lines_out.append(f"<li>{ln_esc[2:]}</li>")
                elif ln_esc.strip() == "":    lines_out.append("<br>")
                else:
                    ln_esc = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', ln_esc)
                    lines_out.append(f"<p>{ln_esc}</p>")
            body_html = (
                f'<div style="margin-bottom:16px">'
                f'<span class="badge badge-{rec_cls}" style="font-size:15px;padding:6px 16px">{html_mod.escape(rec)}</span>'
                f'</div>'
                f'<div class="rec-body">{"".join(lines_out)}</div>'
            )

        page = f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title_str} — Unborn</title>
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
  .badge-warn{{background:var(--warn-bg);color:var(--warn)}}
  .badge-danger{{background:var(--danger-bg);color:var(--danger)}}
  .rec-body h1,.rec-body h2{{color:var(--accent);margin:16px 0 6px;font-size:14px}}
  .rec-body p{{margin:4px 0;max-width:860px}}
  .rec-body li{{margin:2px 0 2px 20px;max-width:860px}}
  .rec-body strong{{color:var(--text)}}
  .rec-body br{{display:block;margin:6px 0;content:""}}
</style></head><body>
<header>
  <a class="back" href="/" onclick="window.close();return false;">&#8592; Back</a>
  <h1>&#9660; Unborn Analysis</h1>
  <span class="pos-label">{title_str}</span>
</header>
{body_html}
</body></html>"""
        return Response(page, mimetype="text/html")

    @app.route("/analyze/<path:pos_key>")
    def analyze_detail(pos_key: str):
        pos_key = urllib.parse.unquote(pos_key)
        cached  = _analysis_cache.get(pos_key)

        parts = pos_key.split("|")
        title_str = " · ".join(parts) if parts else pos_key

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
            rec_cls = {"ROLL": "warn", "ASSIGNMENT": "danger", "HOLD": "ok"}.get(rec, "ok")
            # Convert markdown-ish text to simple HTML
            lines_out = []
            for ln in text.splitlines():
                ln_esc = html_mod.escape(ln)
                if ln_esc.startswith("## "):
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
                    # Bold inline **text**
                    ln_esc = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', ln_esc)
                    lines_out.append(f"<p>{ln_esc}</p>")
            # Net Chain Cash PnL footer
            chain_cash    = cached.get("chain_cash")
            collected     = cached.get("chain_collected")
            paid          = cached.get("chain_paid")
            n_pos         = cached.get("chain_positions")
            pos_avg_price = cached.get("pos_avg_price")
            pos_qty       = cached.get("pos_qty")

            if chain_cash is not None and collected is not None and paid is not None:
                pos_lbl = f"{n_pos} position{'s' if n_pos != 1 else ''}" if n_pos else "positions"

                has_pos = pos_avg_price is not None and pos_qty is not None and pos_qty > 0

                if rec == "ROLL" and has_pos:
                    # ROLL: close current leg at 40% of premium before rolling (keep 60%)
                    buy_back_cost = round(pos_avg_price * 0.40 * 100 * pos_qty, 2)
                    expected_pnl  = round(pos_avg_price * 0.60 * 100 * pos_qty, 2)
                    final_pnl     = round(chain_cash - buy_back_cost, 2)
                    final_sign    = "+" if final_pnl >= 0 else ""
                    final_clr     = "var(--ok)" if final_pnl >= 0 else "var(--danger)"
                    working = (
                        f"${collected:,.2f} collected "
                        f"− ${paid:,.2f} paid "
                        f"− ${buy_back_cost:,.2f} close at 40% "
                        f"(${pos_avg_price:.2f} x 0.40 x 100 x {pos_qty} contracts) "
                        f"= <strong style='color:{final_clr}'>{final_sign}${final_pnl:,.2f}</strong>"
                    )
                    exp_note = f". Expected PnL if closed today: +${expected_pnl:,.2f}"

                elif rec == "HOLD" and has_pos:
                    # HOLD: position expected to expire worthless — keep full remaining premium
                    remaining     = round(pos_avg_price * 100 * pos_qty, 2)
                    final_pnl     = round(chain_cash, 2)   # chain_cash already includes open premium
                    final_sign    = "+" if final_pnl >= 0 else ""
                    final_clr     = "var(--ok)" if final_pnl >= 0 else "var(--danger)"
                    working = (
                        f"${collected:,.2f} collected "
                        f"− ${paid:,.2f} paid "
                        f"(${remaining:,.2f} remaining if expires worthless: "
                        f"${pos_avg_price:.2f} x 100 x {pos_qty} contracts) "
                        f"= <strong style='color:{final_clr}'>{final_sign}${final_pnl:,.2f}</strong>"
                    )
                    exp_note = ""

                else:
                    # No position data or ASSIGNMENT — show raw chain cash
                    sign    = "+" if chain_cash >= 0 else ""
                    clr     = "var(--ok)" if chain_cash >= 0 else "var(--danger)"
                    working = (
                        f"${collected:,.2f} collected "
                        f"− ${paid:,.2f} paid "
                        f"= <strong style='color:{clr}'>{sign}${chain_cash:,.2f}</strong>"
                    )
                    exp_note = ""

                chain_html = (
                    f'<div class="chain-pnl">'
                    f'<span class="chain-label">Net Chain Cash PnL &nbsp;<span style="font-weight:normal;color:var(--muted)">({pos_lbl})</span></span>'
                    f'<span class="chain-working">Working: {working}{html_mod.escape(exp_note)}</span>'
                    + f'</div>'
                )
            else:
                chain_html = ""

            body_html = (
                f'<div style="margin-bottom:16px">'
                f'<span class="badge badge-{rec_cls}" style="font-size:15px;padding:6px 16px">{html_mod.escape(rec)}</span>'
                f'</div>'
                f'<div class="rec-body">{"".join(lines_out)}</div>'
                f'{chain_html}'
            )

        page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html_mod.escape(title_str)} — Recommendation</title>
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
</style>
</head>
<body>
<header>
  <a class="back" href="/" onclick="window.close();return false;">&#8592; Back</a>
  <h1>&#9660; Recommendation</h1>
  <span class="pos-label">{html_mod.escape(title_str)}</span>
</header>
{body_html}
</body>
</html>"""
        return Response(page, mimetype="text/html")

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
        log.warning("SIGTERM received — restarting server...")
        print("\nSIGTERM received — bouncing server...", flush=True)
        # Spawn a lightweight detached process (close_fds=True so it does NOT
        # inherit the Flask socket) that waits for us to release the port, then
        # starts a fresh server. Parent exits immediately via os._exit().
        import subprocess
        cmd = (
            f"import time, os, sys; "
            f"time.sleep(1.5); "
            f"os.execv({sys.executable!r}, {[sys.executable] + sys.argv!r})"
        )
        subprocess.Popen(
            [sys.executable, "-c", cmd],
            close_fds=True,
            start_new_session=True,
        )
        os._exit(0)

    signal.signal(signal.SIGTERM, _sigterm_handler)

    threading.Thread(target=_open_browser, daemon=True).start()
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


async def _purge_stale_ticker_sources(client, notebook_id: str, uploading_ticker: str | None = None) -> None:
    """
    Delete notebook sources that look like ticker CSV uploads when either:
      • The stem matches ``uploading_ticker`` exactly (always delete before re-upload), OR
      • The source is ≥1 day old (routine age-based cleanup).

    A "ticker source" is identified by:
      • filename stem (left of the first '.') is all-uppercase
      • filename stem is 1–5 characters long   (e.g. IBM, CLX, AAPL)
    """
    import datetime

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
        stem  = title.split(".")[0]          # filename left of first '.'

        if not (1 <= len(stem) <= 5):
            continue
        if not stem.isupper():
            continue

        # Always remove if this is the ticker we're about to upload
        if target and stem == target:
            try:
                await client.sources.delete(notebook_id, source.id)
                log.info("[cleanup] Replaced existing source: %r", title)
            except Exception as exc:
                log.error("[cleanup] Failed to delete %r: %s", title, exc)
            continue

        # Also remove any other ticker source that's gone stale (≥1 day old)
        created_at = source.created_at
        if created_at is None:
            continue
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=datetime.timezone.utc)
        if created_at > cutoff:
            continue                         # less than 1 day old — keep it

        try:
            await client.sources.delete(notebook_id, source.id)
            log.info("[cleanup] Removed old source: %r (uploaded %s)", title, created_at.date())
        except Exception as exc:
            log.error("[cleanup] Failed to delete %r: %s", title, exc)


async def upload_to_notebooklm(file_path: str, notebook_id: str) -> None:
    """Upload a file as a new source to the specified NotebookLM notebook.
    Stale ticker CSV sources (≥1 day old, ALL-CAPS stem, 1–5 chars) are
    removed first to keep the notebook tidy.
    """
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

    # Extract ticker from the filename stem (e.g. "IBM.csv" → "IBM")
    base = os.path.basename(file_path)
    stem = base.split(".")[0].upper()
    uploading_ticker = stem if (1 <= len(stem) <= 5 and stem.isupper()) else None

    log.info("Uploading %s to NotebookLM notebook %s ...", file_path, notebook_id)
    try:
        async with NotebookLMClient.from_storage() as client:
            await _purge_stale_ticker_sources(client, notebook_id, uploading_ticker)
            await client.sources.add_file(notebook_id, file_path, wait=True)
        log.info("Upload complete: %s", os.path.basename(file_path))
    except Exception as exc:
        print(f"ERROR: Upload failed — {exc}", file=sys.stderr)
        sys.exit(1)


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


async def query_notebooklm(
    notebook_id: str,
    ticker: str,
    strat: str,
    dates: dict,
    positions: list[dict] | None = None,
    vix: float | None = None,
    silent: bool = False,
) -> str | None:
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
    vix_note = f"the current VIX is {vix:.2f}" if vix is not None else ""

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
            f"Label it 'Expected PnL (buy-back at 40% of premium)'."
        )
        vix_clause = f", the VIX ({vix_note})," if vix_note else ""
        question = (
            f"Based on the updated {ticker} source, the latest economic release calendar, "
            f"and the latest PLAN and REVIEW sources{vix_clause} determine the best roll or "
            f"do nothing strategy. "
            f"{pos_context}"
            f"Note that {earnings_note} and {exdiv_note}. "
            f"{pnl_instruction}"
        )
    else:
        vix_clause = f", the VIX ({vix_note})," if vix_note else ""
        question = (
            f"Given the {ticker} source, what is the best {strat_label} choice, "
            f"taking into consideration the economic calendar releases, "
            f"the latest PLAN and REVIEW sources{vix_clause} and the upcoming "
            f"dividends and/or earnings releases? "
            f"Note that {earnings_note} and {exdiv_note}."
        )

    if not silent:
        print(f"\nQuerying NotebookLM...")

    try:
        async with NotebookLMClient.from_storage() as client:
            result = await client.chat.ask(notebook_id, question)
    except Exception as exc:
        if not silent:
            print(f"ERROR: Query failed — {exc}", file=sys.stderr)
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

  Evaluate ALL open positions for risk signals (delta, DTE, ATM, ITM):
    python get_option_chain.py --eval

  Evaluate only IBM open positions:
    python get_option_chain.py --eval --ticker IBM

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
    args = parser.parse_args()

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
    if not strat_only and (args.upload or args.onlyload):
        asyncio.run(upload_to_notebooklm(output_file, notebook_id))

    # Step 9 — Optionally query NotebookLM for strategy recommendation
    if args.strat:
        asyncio.run(
            query_notebooklm(
                notebook_id, ticker, args.strat, key_dates, open_positions, current_vix
            )
        )


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
