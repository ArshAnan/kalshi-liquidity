"""Rank every Kalshi series by current trading activity, so we can pick the study universe.

Usage:  python -m kalshi_liq.universe            -> data/universe_candidates.csv

Kalshi's raw market list is dominated by auto-generated multivariate parlays
(KXMVE...), so we exclude those and aggregate by series.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

import pandas as pd

from .client import KalshiClient


def f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/universe_candidates.csv")
    args = ap.parse_args()

    c = KalshiClient()
    series = {s["ticker"]: s for s in c.all_series()}
    print(f"{len(series)} series listed")

    agg = defaultdict(lambda: dict(n_open=0, vol_24h=0.0, vol_total=0.0, oi=0.0,
                                   n_two_sided=0, spread_sum=0.0))
    n = 0
    for m in c.paginate("/markets", "markets", status="open", mve_filter="exclude"):
        n += 1
        st = m["event_ticker"].split("-")[0]
        a = agg[st]
        a["n_open"] += 1
        a["vol_24h"] += f(m.get("volume_24h_fp"))
        a["vol_total"] += f(m.get("volume_fp"))
        a["oi"] += f(m.get("open_interest_fp"))
        bid, ask = f(m.get("yes_bid_dollars")), f(m.get("yes_ask_dollars"))
        if 0 < bid < ask < 1:
            a["n_two_sided"] += 1
            a["spread_sum"] += ask - bid
        if n % 5000 == 0:
            print(f"  scanned {n} open markets")
    print(f"scanned {n} open markets in {len(agg)} series")

    rows = []
    for st, a in agg.items():
        s = series.get(st, {})
        rows.append(dict(
            series_ticker=st,
            title=s.get("title"),
            category=s.get("category"),
            frequency=s.get("frequency"),
            fee_type=s.get("fee_type"),
            fee_multiplier=s.get("fee_multiplier"),
            n_open_markets=a["n_open"],
            volume_24h=a["vol_24h"],
            volume_open_markets=a["vol_total"],
            open_interest=a["oi"],
            n_two_sided=a["n_two_sided"],
            avg_spread=(a["spread_sum"] / a["n_two_sided"]) if a["n_two_sided"] else None,
        ))
    df = pd.DataFrame(rows).sort_values("volume_24h", ascending=False)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.out, index=False)
    print(f"wrote {args.out}")
    print(df.head(40).to_string(index=False, max_colwidth=40))


if __name__ == "__main__":
    main()
