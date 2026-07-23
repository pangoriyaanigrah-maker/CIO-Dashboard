#!/usr/bin/env python3
"""
DL India Core — nightly signal builder (v2: live momentum + valuation + quality).

What it does
------------
1. Fetches 13 months of daily prices for a ~60-name NSE universe
   (portfolio names + Nifty large/mid caps) via yfinance. No API key.
2. Fetches fundamentals per name (trailing P/E, P/B, ROE, debt/equity,
   operating margin) from Yahoo.
3. Computes cross-sectional z-scores vs the whole universe:
     momo : 0.5 * (12M return skipping last 1M) + 0.5 * 6M return
     val  : mean z of earnings yield (E/P) and book yield (B/P)
            -> positive = CHEAPER than universe (matches dashboard legend)
     qual : mean z of ROE, operating margin, and -debt/equity
            -> positive = higher quality
4. Writes signals.json containing ONLY the portfolio tickers,
   in the exact shape the dashboard reads.

Fundamentals coverage on Yahoo for NSE is decent but not perfect;
any missing metric is simply dropped from that stock's composite.
If a stock has no usable metrics at all, val/qual come out null and
the dashboard shows "—".

Run:  python3 build_signals.py
Out:  signals.json
"""

import json
import math
import sys
import time
from datetime import datetime, timezone, timedelta

import yfinance as yf

# ------------------------------------------------------------------ config
# Portfolio names — these are what lands in signals.json
PORTFOLIO = {
    "HDFCBANK":  "HDFCBANK.NS",
    "BEL":       "BEL.NS",
    "SUNPHARMA": "SUNPHARMA.NS",
    "TITAN":     "TITAN.NS",
    "DIXON":     "DIXON.NS",
    "PERSISTENT":"PERSISTENT.NS",
    "KAYNES":    "KAYNES.NS",
}

# Cross-section universe — used ONLY for statistics (mean/std of each signal).
# Wider list -> z-scores closer to true index-relative. Add freely.
UNIVERSE_EXTRA = [
    # Nifty 50 core
    "RELIANCE.NS","TCS.NS","INFY.NS","ICICIBANK.NS","SBIN.NS","AXISBANK.NS",
    "KOTAKBANK.NS","BHARTIARTL.NS","ITC.NS","HINDUNILVR.NS","LT.NS",
    "BAJFINANCE.NS","MARUTI.NS","M&M.NS","TATAMOTORS.NS","NTPC.NS",
    "POWERGRID.NS","ULTRACEMCO.NS","ASIANPAINT.NS","NESTLEIND.NS",
    "WIPRO.NS","HCLTECH.NS","TECHM.NS","DRREDDY.NS","CIPLA.NS",
    "DIVISLAB.NS","APOLLOHOSP.NS","TATASTEEL.NS","JSWSTEEL.NS",
    "HINDALCO.NS","COALINDIA.NS","ONGC.NS","BPCL.NS","GRASIM.NS",
    "ADANIENT.NS","ADANIPORTS.NS","EICHERMOT.NS","BAJAJ-AUTO.NS",
    "HEROMOTOCO.NS","INDUSINDBK.NS","SBILIFE.NS","HDFCLIFE.NS",
    "TATACONSUM.NS","BRITANNIA.NS","SHRIRAMFIN.NS",
    # mid/small flavour (closer peers to DIXON/KAYNES/PERSISTENT)
    "HAL.NS","BDL.NS","MAZDOCK.NS","SOLARINDS.NS","CGPOWER.NS",
    "POLYCAB.NS","AMBER.NS","SYRMA.NS","COFORGE.NS","MPHASIS.NS",
    "LTIM.NS","CUMMINSIND.NS","ABB.NS","SIEMENS.NS","TIINDIA.NS",
]

CRORE = 1e7
PAUSE = 0.4          # seconds between Yahoo calls, be polite
Z_CLAMP = 3.0        # clamp extreme z-scores

# ------------------------------------------------------------------ fetch
def fetch_prices(ysym):
    """Return dict with cmp, lo (52w), mcap (cr), momo_raw — or raise."""
    t = yf.Ticker(ysym)
    hist = t.history(period="13mo", auto_adjust=True)
    if hist.empty or len(hist) < 120:
        raise RuntimeError(f"insufficient history ({len(hist)} rows)")
    close = hist["Close"]
    cmp_ = float(close.iloc[-1])
    lo52 = float(close[close.index >= close.index[-1] - timedelta(days=365)].min())

    def ret_from(days_ago, skip=0):
        end = close.iloc[-1 - skip] if skip else close.iloc[-1]
        idx = close.index[-1] - timedelta(days=days_ago)
        past = close[close.index <= idx]
        if past.empty:
            past = close.iloc[:1]
        return float(end / past.iloc[-1] - 1.0)

    momo_raw = 0.5 * ret_from(365, skip=21) + 0.5 * ret_from(182)

    mcap = None
    try:
        fi = t.fast_info
        raw = getattr(fi, "market_cap", None)
        if raw is None and hasattr(fi, "get"):
            raw = fi.get("marketCap")
        if raw:
            mcap = round(raw / CRORE)
    except Exception:
        pass
    return {"cmp": round(cmp_, 1), "lo": round(lo52, 1), "mcap": mcap,
            "momo_raw": momo_raw, "_ticker_obj": t}


def fetch_fundamentals(t):
    """Return raw metric dict; missing metrics -> None."""
    try:
        info = t.info or {}
    except Exception:
        info = {}
    def g(k):
        v = info.get(k)
        return float(v) if isinstance(v, (int, float)) and math.isfinite(v) else None

    pe  = g("trailingPE")
    pb  = g("priceToBook")
    roe = g("returnOnEquity")          # fraction, e.g. 0.17
    de  = g("debtToEquity")            # percent-ish, e.g. 45.3
    om  = g("operatingMargins")        # fraction

    return {
        "ey": (1.0 / pe) if pe and pe > 0 else (None if pe is None else -0.05),
        #      earnings yield; loss-makers get a punitive fixed low yield
        "by": (1.0 / pb) if pb and pb > 0 else None,   # book yield
        "roe": roe,
        "negde": (-de) if de is not None else None,     # less debt = better
        "om": om,
    }

# ------------------------------------------------------------------ stats
def zmap(values):
    """values: {name: raw or None} -> {name: clamped z or None}."""
    xs = [v for v in values.values() if v is not None]
    if len(xs) < 5:
        return {k: None for k in values}
    mu = sum(xs) / len(xs)
    sd = math.sqrt(sum((x - mu) ** 2 for x in xs) / len(xs)) or 1.0
    out = {}
    for k, v in values.items():
        out[k] = None if v is None else max(-Z_CLAMP, min(Z_CLAMP, (v - mu) / sd))
    return out


def composite(zmaps, name):
    """Mean of available component z's for one stock; None if none available."""
    vals = [zm[name] for zm in zmaps if zm.get(name) is not None]
    return round(sum(vals) / len(vals), 2) if vals else None

# ------------------------------------------------------------------ main
def main():
    all_syms = {**PORTFOLIO, **{s.replace(".NS", ""): s for s in UNIVERSE_EXTRA}}
    prices, funda, errors = {}, {}, []

    print(f"Universe: {len(all_syms)} names")
    for dkey, ysym in all_syms.items():
        try:
            row = fetch_prices(ysym)
            t = row.pop("_ticker_obj")
            prices[dkey] = row
            funda[dkey] = fetch_fundamentals(t)
            print(f"  ok   {dkey:<12} cmp={row['cmp']:>10}")
        except Exception as e:
            msg = f"{dkey}: {e}"
            errors.append(msg)
            print(f"  FAIL {msg}", file=sys.stderr)
        time.sleep(PAUSE)

    missing_port = [k for k in PORTFOLIO if k not in prices]
    if missing_port:
        print(f"WARNING: portfolio names missing: {missing_port}", file=sys.stderr)
    if len(prices) < 15:
        sys.exit("Universe too small after failures — aborting, keeping previous signals.json")

    # ---- cross-sectional z-scores over the WHOLE universe
    momo_z = zmap({k: prices[k]["momo_raw"] for k in prices})
    ey_z   = zmap({k: funda[k]["ey"]    for k in prices})
    by_z   = zmap({k: funda[k]["by"]    for k in prices})
    roe_z  = zmap({k: funda[k]["roe"]   for k in prices})
    de_z   = zmap({k: funda[k]["negde"] for k in prices})
    om_z   = zmap({k: funda[k]["om"]    for k in prices})

    # ---- emit portfolio names only
    out = {}
    for dkey in PORTFOLIO:
        if dkey not in prices:
            continue
        p = prices[dkey]
        out[dkey] = {
            "cmp": p["cmp"], "mcap": p["mcap"], "lo": p["lo"],
            "momo": round(momo_z[dkey], 2) if momo_z.get(dkey) is not None else None,
            "val":  composite([ey_z, by_z], dkey),
            "qual": composite([roe_z, om_z, de_z], dkey),
        }

    ist = timezone(timedelta(hours=5, minutes=30))
    payload = {
        "asof": datetime.now(ist).strftime("%d %b %Y, %H:%M IST"),
        "note": (f"z-scores cross-sectional vs {len(prices)}-name NSE universe. "
                 "val = earnings+book yield (positive=cheap); "
                 "qual = ROE+op margin−leverage; momo = 12M−1M & 6M blend. "
                 "Source: Yahoo Finance nightly."),
        "universe_size": len(prices),
        "errors": errors,
        "signals": out,
    }
    with open("signals.json", "w") as f:
        json.dump(payload, f, indent=1)
    print(f"\nWrote signals.json — {len(out)} portfolio names, "
          f"universe {len(prices)}, asof {payload['asof']}")


if __name__ == "__main__":
    main()
