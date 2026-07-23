#!/usr/bin/env python3
"""
DL India Core — nightly signal builder.

Fetches NSE prices via yfinance, computes momentum z-scores
cross-sectionally across the portfolio universe, and writes signals.json
in the shape the dashboard consumes:

  { "asof": "23 Jul 2026, 18:05 IST",
    "signals": { "HDFCBANK": {cmp, mcap, lo, val, momo, qual}, ... } }

val / qual are Phase 2 (need a fundamentals feed); until then they are
carried from an optional overrides file or emitted as null — the
dashboard already renders "—" for nulls.

Run:  python3 build_signals.py
Out:  signals.json  (same directory)
"""

import json
import math
import sys
from datetime import datetime, timezone, timedelta

import yfinance as yf

# ---------------------------------------------------------------- config
TICKERS = {
    # dashboard key -> Yahoo symbol
    "HDFCBANK":  "HDFCBANK.NS",
    "BEL":       "BEL.NS",
    "SUNPHARMA": "SUNPHARMA.NS",
    "TITAN":     "TITAN.NS",
    "DIXON":     "DIXON.NS",
    "PERSISTENT":"PERSISTENT.NS",
    "KAYNES":    "KAYNES.NS",
}

# Phase-2 placeholders: value/quality z-scores need a fundamentals feed
# (EODHD / FactSet). Keep analyst-maintained overrides here meanwhile,
# or set to None to show "—" in the UI.
VALQUAL_OVERRIDES = {
    "HDFCBANK":  {"val": +0.9, "qual": +0.7},
    "BEL":       {"val": -1.1, "qual": +0.8},
    "SUNPHARMA": {"val": -0.2, "qual": +0.5},
    "TITAN":     {"val": -1.5, "qual": +0.9},
    "DIXON":     {"val": -2.0, "qual": +0.3},
    "PERSISTENT":{"val": -0.9, "qual": +0.6},
    "KAYNES":    {"val": -1.7, "qual": -0.3},
}

CRORE = 1e7  # dashboard mcap unit = ₹ crore

# ---------------------------------------------------------------- fetch
def fetch_one(dkey: str, ysym: str):
    t = yf.Ticker(ysym)
    hist = t.history(period="13mo", auto_adjust=True)
    if hist.empty or len(hist) < 140:
        raise RuntimeError(f"{ysym}: insufficient history ({len(hist)} rows)")

    close = hist["Close"]
    cmp_ = float(close.iloc[-1])
    lo52 = float(close[close.index >= close.index[-1] - timedelta(days=365)].min())

    # momentum raw: blend of 12M-1M and 6M total return
    def ret_from(days_ago: int, skip: int = 0) -> float:
        end = close.iloc[-1 - skip] if skip else close.iloc[-1]
        idx = close.index[-1] - timedelta(days=days_ago)
        past = close[close.index <= idx]
        if past.empty:
            past = close.iloc[:1]
        return float(end / past.iloc[-1] - 1.0)

    r12_1 = ret_from(365, skip=21)   # 12M return, skipping last ~1M
    r6 = ret_from(182)
    momo_raw = 0.5 * r12_1 + 0.5 * r6

    # market cap in ₹ crore
    mcap = None
    try:
        fi = t.fast_info
        raw = getattr(fi, "market_cap", None) or fi.get("marketCap")
        if raw:
            mcap = round(raw / CRORE)
    except Exception:
        pass

    return {"cmp": round(cmp_, 1), "mcap": mcap, "lo": round(lo52, 1),
            "momo_raw": momo_raw}


def zscores(values: dict[str, float]) -> dict[str, float]:
    xs = list(values.values())
    n = len(xs)
    mu = sum(xs) / n
    sd = math.sqrt(sum((x - mu) ** 2 for x in xs) / n) or 1.0
    return {k: round((v - mu) / sd, 2) for k, v in values.items()}


def main():
    out, momo_raw, errors = {}, {}, []
    for dkey, ysym in TICKERS.items():
        try:
            row = fetch_one(dkey, ysym)
            momo_raw[dkey] = row.pop("momo_raw")
            out[dkey] = row
            print(f"  ok  {dkey:<11} cmp={row['cmp']:>9}  mcap(cr)={row['mcap']}")
        except Exception as e:
            errors.append(f"{dkey}: {e}")
            print(f"  FAIL {dkey}: {e}", file=sys.stderr)

    if not out:
        sys.exit("No tickers fetched — aborting, keeping previous signals.json")

    # NOTE: z vs portfolio names only (7-name cross-section), not Nifty 500.
    # Widen the universe in TICKERS to approach true index-relative scores.
    momo_z = zscores(momo_raw)
    for dkey in out:
        out[dkey]["momo"] = momo_z[dkey]
        ov = VALQUAL_OVERRIDES.get(dkey, {})
        out[dkey]["val"] = ov.get("val")
        out[dkey]["qual"] = ov.get("qual")

    ist = timezone(timedelta(hours=5, minutes=30))
    payload = {
        "asof": datetime.now(ist).strftime("%d %b %Y, %H:%M IST"),
        "note": "momo z is cross-sectional vs portfolio names; val/qual are analyst overrides pending fundamentals feed",
        "errors": errors,
        "signals": out,
    }
    with open("signals.json", "w") as f:
        json.dump(payload, f, indent=1)
    print(f"\nWrote signals.json — {len(out)} tickers, asof {payload['asof']}")


if __name__ == "__main__":
    main()