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


def eval_open_positions(token: str, account_id: str, ticker: str | None = None) -> None:
    """
    Fetch live option data for every open position and flag those that meet
    any of the following criteria:
        • abs(delta) >= 0.40
        • DTE <= 21
        • Near ATM  (underlying within ATM_THRESHOLD_PCT % of strike)
        • ITM       (call: underlying > strike; put: underlying < strike)
        • Within 1.5× ATR buffer of the strike
    Uses a single POST /quotes call for all positions instead of one
    option-chain fetch per expiry date.
    """
    import datetime

    positions = get_all_open_positions(ticker)
    if not positions:
        label = f"{ticker} positions" if ticker else "positions"
        print_box([f"  No open {label} found in the journal."], title="  Portfolio Evaluation  ")
        return

    today = datetime.date.today()
    unique_tickers = {pos["symbol"].upper() for pos in positions}
    print(
        f"\nEvaluating {len(positions)} open position(s) across "
        f"{len(unique_tickers)} ticker(s)..."
    )

    # Pre-filter expired / unparseable positions
    active: list[dict] = []
    skipped: list[str] = []
    for pos in positions:
        sym = pos["symbol"].upper()
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
        print_box(["  No active (non-expired) open positions found."], title="  Portfolio Evaluation  ")
        return

    # ── Batch API calls: quotes (price) + greeks (delta) ─────────────────────
    osi_list = [
        build_osi_symbol(p["symbol"].upper(), p["expiry"], p["option_type"].upper(), float(p["strike"]))
        for p in active
    ]
    print(f"  Fetching quotes for {len(active)} contract(s)...", end=" ", flush=True)
    quotes = get_option_quotes(token, account_id, active)
    print(f"{len(quotes)} received.")
    print(f"  Fetching greeks for {len(osi_list)} contract(s)...", end=" ", flush=True)
    greeks_data = get_option_greeks_batch(token, account_id, osi_list)
    print(f"{len(greeks_data)} received.")

    # ── Per-ticker caches (underlying price + ATR) ────────────────────────────
    price_cache_local: dict[str, float | None] = {}
    atr_cache: dict[str, tuple[float | None, float | None]] = {}

    def _to_f(v: object) -> float | None:
        try:
            return float(v) if v not in (None, "") else None
        except (ValueError, TypeError):
            return None

    flagged: list[dict] = []

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

        osi   = build_osi_symbol(sym, expiry, opt_type, strike).replace(" ", "")

        # Delta from greeks endpoint
        greeks = greeks_data.get(osi, {})
        delta: float | None = _to_f(greeks.get("delta"))

        # Current price from quotes endpoint: prefer mid=(bid+ask)/2, then last
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
            pct = abs(underlying - strike) / strike * 100
            if pct <= ATM_THRESHOLD_PCT:
                reasons.append(
                    f"Near ATM: u/l ${underlying:.2f} vs strike ${strike:.2f}"
                    f" ({pct:.1f}% away)"
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

        if reasons:
            flagged.append({
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
            })

    # ── Output ────────────────────────────────────────────────────────────────
    if not flagged:
        print_box(
            ["  All open positions are within normal parameters. No action needed."],
            title="  Portfolio Evaluation — No Alerts  ",
        )
    else:
        lines: list[str] = [
            f"  {len(flagged)} of {len(positions)} position(s) flagged for review:",
            "",
        ]
        for item in flagged:
            lines.append(f"  {format_position(item)}")
            # Current price, delta, and PnL — always shown when data is available
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
        async with NotebookLMClient.from_storage() as client:
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

    print(f"\nQuerying NotebookLM...")

    try:
        async with NotebookLMClient.from_storage() as client:
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

    # ── --eval: standalone portfolio evaluation mode ──────────────────────────
    if args.eval:
        secret = os.environ.get("PUBLIC_API_SECRET")
        if not secret:
            print(
                "ERROR: PUBLIC_API_SECRET is required for --eval.\n"
                "  1. Go to https://public.com (Settings → API)\n"
                "  2. Generate a Secret Token\n"
                "  3. Run:  export PUBLIC_API_SECRET=your_secret_here",
                file=sys.stderr,
            )
            sys.exit(1)
        token = get_access_token(secret)
        account_id = get_account_id(token)
        eval_ticker = args.ticker.upper() if args.ticker else None
        eval_open_positions(token, account_id, eval_ticker)
        sys.exit(0)

    # All non-eval modes require --ticker
    if not args.ticker:
        parser.error("--ticker is required unless using --eval")

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
    main()
