"""Liquidity Incentive Program (LIP) scoring, plus a public order-book snapshotter.

Scoring follows help.kalshi.com/en/articles/13823851 (per snapshot, per side):
  1. Walk down from the best bid. Reference price = first level where cumulative
     size >= target_size / 5.
  2. Orders needed to reach target_size qualify (whole levels, best price first).
     If the side never reaches target_size, nothing on that side qualifies.
  3. Multiplier = 1 at/better than reference, discount_factor ** ticks_behind otherwise.
  4. Your snapshot score on a side = your (size * multiplier) / everyone's.
  Period reward = your share of all snapshot scores * period reward
                  * (snapshots with BOTH sides at target / all snapshots).

Kalshi only publishes aggregate depth per price, not who owns it, so `score_side`
answers "if we add `my_size` at `my_price`, what share would we get?". Within a
price level we assume we queue behind existing size (matters only for whether the
level is inside the target cutoff).

Snapshotter usage (public endpoint, no API key):
  python -m kalshi_liq.lip snap --top 40 --interval 5 --hours 24
  -> data/book_snapshots/YYYY-MM-DD/HH.parquet  (recv_ts, ticker, side, price, size)
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .client import KalshiClient

TICK = 0.01


def score_side(levels: list[tuple[float, float]], target: float, discount: float,
               my_price: float | None = None, my_size: float = 0.0) -> dict:
    """levels: [(price, size)] bids on one side (YES bids or NO bids), any order."""
    book = sorted(((float(p), float(s)) for p, s in levels), key=lambda x: -x[0])
    entries = [(p, s, False) for p, s in book]
    if my_price is not None and my_size > 0:
        # insert after existing size at the same price (time priority)
        i = next((k for k, (p, _, _) in enumerate(entries) if p < my_price - 1e-9), len(entries))
        entries.insert(i, (my_price, my_size, True))

    cum, ref, qual = 0.0, None, []
    for p, s, mine in entries:
        if cum >= target:
            break
        cum += s
        qual.append((p, s, mine))
        if ref is None and cum >= target / 5:
            ref = p
    if cum < target or ref is None:
        return dict(meets_target=False, ref_price=ref, my_share=0.0, total_raw=0.0, depth=cum)

    def mult(p):
        ticks = round((ref - p) / TICK)
        return 1.0 if ticks <= 0 else discount ** ticks

    raw = [(s * mult(p), mine) for p, s, mine in qual]
    total = sum(r for r, _ in raw)
    mine = sum(r for r, m in raw if m)
    return dict(meets_target=True, ref_price=ref, my_share=mine / total if total else 0.0,
                total_raw=total, depth=cum)


def book_levels(ob: dict) -> tuple[list, list]:
    b = ob.get("orderbook_fp") or ob.get("orderbook") or {}
    yes = [(float(p), float(s)) for p, s in (b.get("yes_dollars") or [])]
    no = [(float(p), float(s)) for p, s in (b.get("no_dollars") or [])]
    return yes, no


def active_lip_markets(top: int) -> pd.DataFrame:
    df = pd.read_parquet("data/incentives/incentive_programs.parquet")
    now = pd.Timestamp.now(tz="UTC")
    a = df[(df.start_date <= now) & (df.end_date > now)]
    return a.sort_values("reward_usd_per_day", ascending=False).head(top)


def snap(top: int, interval: float, hours: float, tickers: list[str] | None) -> None:
    c = KalshiClient(max_rps=15)
    if tickers:
        meta = pd.DataFrame({"market_ticker": tickers})
    else:
        meta = active_lip_markets(top)
        print(f"snapshotting {len(meta)} LIP markets worth ${meta.reward_usd_per_day.sum():,.0f}/day")
    stop = time.time() + hours * 3600
    buf, cur_hour = [], None
    while time.time() < stop:
        t0 = time.time()
        for t in meta.market_ticker:
            try:
                ob = c.orderbook(t)
            except Exception as e:
                print(f"{t}: {e!r}")
                continue
            recv = datetime.now(timezone.utc)
            yes, no = book_levels(ob)
            buf += [(recv, t, "yes", p, s) for p, s in yes] + [(recv, t, "no", p, s) for p, s in no]
        hour = datetime.now(timezone.utc).strftime("%Y-%m-%d/%H")
        if cur_hour and hour != cur_hour or time.time() >= stop:
            _flush(buf, cur_hour)
            buf = []
        cur_hour = hour
        time.sleep(max(0.0, interval - (time.time() - t0)))
    _flush(buf, cur_hour)


def _flush(buf: list, hour: str | None) -> None:
    if not buf or not hour:
        return
    p = Path("data/book_snapshots") / f"{hour}.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(buf, columns=["recv_ts", "ticker", "side", "price", "size"])
    if p.exists():
        df = pd.concat([pd.read_parquet(p), df])
    df.to_parquet(p, index=False)
    print(f"wrote {len(df):,} rows -> {p}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snap")
    s.add_argument("--top", type=int, default=40)
    s.add_argument("--interval", type=float, default=5.0)
    s.add_argument("--hours", type=float, default=24.0)
    s.add_argument("--tickers", nargs="*")
    a = ap.parse_args()
    if a.cmd == "snap":
        snap(a.top, a.interval, a.hours, a.tickers)


if __name__ == "__main__":
    main()
