"""
Deterministic option pricing and Greeks for the P&L-decomposition / decay-quality
feature. No LLM involvement anywhere in this module by design — see the roll-PnL
work in option_dashboard.py for why (NotebookLM's own arithmetic proved unreliable
this session; the same reasoning applies here, more so given the compounding
error risk of second/third-order Greeks).

Pricing model: Barone-Adesi-Whaley (BAW), a standard closed-form-ish American
option approximation. Chosen over vanilla European Black-Scholes because these
are American-style equity options on dividend-paying names, and BAW captures the
early-exercise premium that plain BS ignores (the dashboard's existing
"dividend >= extrinsic" early-assignment heuristic is a crude proxy for the same
concern this formally supersedes for repricing purposes).

Greeks (delta, gamma, theta, vega, rho, vanna, charm) are computed via central
finite differences on the BAW price function itself, rather than hand-derived
analytic BAW formulas. This is a deliberate engineering choice: analytic BAW
Greeks (especially vanna/charm, which are third-order) are easy to get sign- or
term-wrong when transcribed from a paper, and errors there would be silent and
hard to catch. Finite differences on a single, validated pricing function is
slower per-call but mechanically simple to get right and to verify (see the
verification step run against known Black-Scholes reference values).

Known limitations (deliberately not solved further — see the plan discussion):
  - Volatility is treated as flat (no skew/smile). BAW/BS assume a single sigma;
    real markets don't. This mostly affects vanna/charm precision.
  - Risk-free rate is a static approximation (DEFAULT_RISK_FREE_RATE), not a live
    curve. Rho's impact on short-dated equity options is small enough that this
    is an acceptable simplification.
  - Dividend yield is approximated from the last known quarterly dividend amount
    annualized (x4) and divided by spot — not a real forward dividend schedule.
  - No jump risk, no stochastic vol. This is a decay-quality heuristic, not a
    trading-grade risk model.
"""

from __future__ import annotations

import math

DEFAULT_RISK_FREE_RATE = 0.045  # static approximation; rho's impact is minor here

_SQRT_2PI = math.sqrt(2.0 * math.pi)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT_2PI


def dividend_yield_from_quarterly(dividend_amount: float | None, spot: float | None) -> float:
    """Approximate a continuous dividend yield from a quarterly $ dividend."""
    if not dividend_amount or not spot or spot <= 0:
        return 0.0
    return max(0.0, (dividend_amount * 4.0) / spot)


def bs_price(S: float, K: float, T: float, r: float, b: float, sigma: float, is_call: bool) -> float:
    """European option price with cost-of-carry b (b = r for no dividend, b = r - q with a
    continuous dividend yield q). This is the building block BAW's early-exercise
    premium is added on top of."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        intrinsic = max(0.0, S - K) if is_call else max(0.0, K - S)
        return intrinsic
    sig_sqrt_t = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (b + 0.5 * sigma * sigma) * T) / sig_sqrt_t
    d2 = d1 - sig_sqrt_t
    if is_call:
        return S * math.exp((b - r) * T) * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    return K * math.exp(-r * T) * norm_cdf(-d2) - S * math.exp((b - r) * T) * norm_cdf(-d1)


def _baw_critical_price(K: float, T: float, r: float, b: float, sigma: float, is_call: bool) -> float:
    """
    Bisection solve for the early-exercise critical stock price (S* for calls,
    S** for puts) per Barone-Adesi & Whaley (1987). Deliberately bisection, not
    Newton-Raphson: the analytic derivative of the defining equation is easy to
    get sign/term-wrong (an earlier version of this function did, and silently
    converged to a floor value instead of the true root — caught by the American
    put q=0 sanity check, which should sit strictly above the European price and
    didn't). Bisection only needs the defining equation itself, which is directly
    verified against known Black-Scholes reference values elsewhere.
    """
    sig2 = sigma * sigma
    M = 2.0 * r / sig2
    N = 2.0 * b / sig2
    Kc = 1.0 - math.exp(-r * T)
    sign = 1.0 if is_call else -1.0
    q = (-(N - 1.0) + sign * math.sqrt((N - 1.0) ** 2 + 4.0 * M / Kc)) / 2.0
    sig_sqrt_t = sigma * math.sqrt(T)

    def residual(Si: float) -> float:
        eu = bs_price(Si, K, T, r, b, sigma, is_call)
        d1 = (math.log(Si / K) + (b + 0.5 * sig2) * T) / sig_sqrt_t
        if is_call:
            return (Si - K) - (eu + (1.0 - math.exp((b - r) * T) * norm_cdf(d1)) * Si / q)
        return (K - Si) - (eu - (1.0 - math.exp((b - r) * T) * norm_cdf(-d1)) * Si / q)

    if is_call:
        # Root is at or above K; expand the upper bound until the residual turns
        # positive (it's negative approaching K from above).
        lo, hi = K, K * 2.0
        f_lo = residual(lo)
        f_hi = residual(hi)
        _tries = 0
        while f_hi < 0 and _tries < 40:
            hi *= 2.0
            f_hi = residual(hi)
            _tries += 1
        if f_lo > 0:
            return lo  # degenerate: already past the boundary at K
    else:
        # Root is strictly below K.
        lo, hi = 1e-6, K
        f_lo = residual(lo)
        f_hi = residual(hi)
        if f_hi > 0:
            return hi  # degenerate: boundary is at/above K

    if f_lo * f_hi > 0:
        # No sign change found (can happen for extreme/degenerate inputs) —
        # fall back to K rather than an unbounded/nonsensical value.
        return K

    for _ in range(80):
        mid = (lo + hi) / 2.0
        f_mid = residual(mid)
        if abs(f_mid) < 1e-8 or (hi - lo) < 1e-8:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2.0


def baw_price(S: float, K: float, T: float, r: float, q: float, sigma: float, is_call: bool) -> float:
    """American option price via Barone-Adesi-Whaley. q = continuous dividend yield;
    b = r - q is the cost of carry used throughout."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(0.0, S - K) if is_call else max(0.0, K - S)

    b = r - q
    eu = bs_price(S, K, T, r, b, sigma, is_call)

    # American call on a non-dividend-paying underlying is never optimal to
    # exercise early — European price is exact, skip the early-exercise solve.
    if is_call and q <= 0:
        return eu

    sig2 = sigma * sigma
    M = 2.0 * r / sig2
    N = 2.0 * b / sig2
    Kc = 1.0 - math.exp(-r * T)
    sign = 1.0 if is_call else -1.0
    qroot = (-(N - 1.0) + sign * math.sqrt((N - 1.0) ** 2 + 4.0 * M / Kc)) / 2.0

    Scrit = _baw_critical_price(K, T, r, b, sigma, is_call)
    sig_sqrt_t = sigma * math.sqrt(T)
    d1_crit = (math.log(Scrit / K) + (b + 0.5 * sig2) * T) / sig_sqrt_t

    if is_call:
        if S >= Scrit:
            return S - K
        A2 = (Scrit / qroot) * (1.0 - math.exp((b - r) * T) * norm_cdf(d1_crit))
        return eu + A2 * (S / Scrit) ** qroot
    else:
        if S <= Scrit:
            return K - S
        A1 = -(Scrit / qroot) * (1.0 - math.exp((b - r) * T) * norm_cdf(-d1_crit))
        return eu + A1 * (S / Scrit) ** qroot


def price(S: float, K: float, T: float, sigma: float, is_call: bool,
          r: float = DEFAULT_RISK_FREE_RATE, q: float = 0.0) -> float:
    """Convenience wrapper: American option price for the given inputs."""
    return baw_price(S, K, T, r, q, sigma, is_call)


def greeks(S: float, K: float, T: float, sigma: float, is_call: bool,
           r: float = DEFAULT_RISK_FREE_RATE, q: float = 0.0) -> dict[str, float | None]:
    """
    First/second/third-order Greeks via central finite differences on baw_price.
    Returns per-share values (not yet scaled by contract multiplier or quantity):
        delta, gamma, theta (per calendar day, negative = losing value),
        vega (per 1 vol point, e.g. IV 20 -> 21), rho (per 1% rate move),
        vanna (d(delta)/d(vol), per 1 vol point),
        charm (d(delta)/d(time), per calendar day)
    Returns None for any Greek that can't be computed (T <= 0, e.g. at/after
    expiry) rather than a misleading number.
    """
    if T is None or T <= 0 or S is None or S <= 0 or K is None or K <= 0 or sigma is None or sigma <= 0:
        return {k: None for k in ("delta", "gamma", "theta", "vega", "rho", "vanna", "charm")}

    hS = max(S * 1e-3, 1e-4)
    hV = 1e-3           # vol bump (absolute, e.g. 0.20 -> 0.201)
    hR = 1e-4           # rate bump
    one_day = 1.0 / 365.0
    hT = min(one_day * 0.5, T * 0.25) if T > one_day else T * 0.25
    if hT <= 0:
        hT = T / 4.0 or 1e-6

    def _p(Sx=S, Tx=T, sigx=sigma, rx=r):
        return baw_price(Sx, K, Tx, rx, q, sigx, is_call)

    def _delta(Sx=S, Tx=T, sigx=sigma):
        return (baw_price(Sx + hS, K, Tx, r, q, sigx, is_call)
                - baw_price(Sx - hS, K, Tx, r, q, sigx, is_call)) / (2 * hS)

    try:
        delta = _delta()
        gamma = (_p(Sx=S + hS) - 2 * _p() + _p(Sx=S - hS)) / (hS * hS)
        vega  = (_p(sigx=sigma + hV) - _p(sigx=sigma - hV)) / (2 * hV)
        rho   = (_p(rx=r + hR) - _p(rx=r - hR)) / (2 * hR)
        # theta: value lost as T decreases by one day, expressed as a negative
        # per-day number (consistent with the usual "theta is negative" convention)
        T_minus = max(T - hT, 1e-6)
        theta = (_p(Tx=T_minus) - _p()) / hT * one_day if hT else None
        vanna = (_delta(sigx=sigma + hV) - _delta(sigx=sigma - hV)) / (2 * hV)
        charm = (_delta(Tx=T_minus) - delta) / hT * one_day if hT else None
    except (ValueError, OverflowError, ZeroDivisionError):
        return {k: None for k in ("delta", "gamma", "theta", "vega", "rho", "vanna", "charm")}

    return {
        "delta": delta, "gamma": gamma, "theta": theta, "vega": vega,
        "rho": rho, "vanna": vanna, "charm": charm,
    }
