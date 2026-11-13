"""Backfill markets, trades and 1-minute bid/ask candles for a list of series.

Usage:
  python -m kalshi_liq.backfill --start 2026-10-14 --end 2026-11-13            # all of config/series.csv
  python -m kalshi_liq.backfill --series KXHIGHNY KXRAIN --start 2026-10-01

Output (Parquet, one file per market, so the job resumes where it stopped):
  data/raw/markets/{series}.parquet
  data/raw/trades/{series}/{market}.parquet
  data/raw/candles/{series}/{market}.parquet

Re-running skips markets already on disk (pass --force to refetch). Markets that
closed with zero volume are skipped.
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .client import KalshiClient, parse_ts

RAW = Path("data/raw")


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def trades_frame(trades: list[dict]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    df = pd.DataFrame(trades)
    df["ts"] = pd.to_datetime(df["created_time"], utc=True, format="ISO8601")
    df["yes_price"] = df["yes_price_dollars"].astype(float)
    df["count"] = df["count_fp"].astype(float)
    # d = +1 if the taker bought YES, -1 if the taker sold YES (= bought NO)
    df["taker_dir"] = df["taker_side"].map({"yes": 1, "no": -1}).astype("int8")
    keep = ["ts", "ticker", "trade_id", "yes_price", "count", "taker_side", "taker_dir", "is_block_trade"]
    return df[keep].sort_values("ts").reset_index(drop=True)


def candles_frame(candles: list[dict]) -> pd.DataFrame:
    rows = []
    for c in candles:
        yb, ya, p = c.get("yes_bid") or {}, c.get("yes_ask") or {}, c.get("price") or {}
        rows.append(dict(
            end_ts=pd.Timestamp(c["end_period_ts"], unit="s", tz="UTC"),
            bid_open=_f(yb.get("open_dollars")), bid_close=_f(yb.get("close_dollars")),
            bid_low=_f(yb.get("low_dollars")), bid_high=_f(yb.get("high_dollars")),
            ask_open=_f(ya.get("open_dollars")), ask_close=_f(ya.get("close_dollars")),
            ask_low=_f(ya.get("low_dollars")), ask_high=_f(ya.get("high_dollars")),
            last_close=_f(p.get("close_dollars")),
            volume=_f(c.get("volume_fp")), open_interest=_f(c.get("open_interest_fp")),
        ))
    return pd.DataFrame(rows)


def markets_frame(markets: list[dict], series: str) -> pd.DataFrame:
    df = pd.DataFrame(markets)
    keep = ["ticker", "event_ticker", "title", "subtitle", "yes_sub_title", "status", "result",
            "open_time", "close_time", "settlement_ts", "expiration_value", "floor_strike",
            "cap_strike", "strike_type", "volume_fp", "open_interest_fp", "settlement_value_dollars",
            "price_level_structure", "rules_primary"]
    df = df[[c for c in keep if c in df.columns]].copy()
    df.insert(0, "series_ticker", series)
    for c in ("open_time", "close_time", "settlement_ts"):
        if c in df:
            df[c] = pd.to_datetime(df[c], utc=True, format="ISO8601")
    df["volume"] = df["volume_fp"].astype(float)
    return df


def _readable(p: Path) -> bool:
    """False for a file left half-written by an interrupted run, so it gets refetched."""
    try:
        pd.read_parquet(p, columns=[])
        return True
    except Exception:
        return False


def backfill_series(c: KalshiClient, series: str, start: datetime, end: datetime, force: bool) -> None:
    t0 = time.time()
    s_ts, e_ts = int(start.timestamp()), int(end.timestamp())
    mk = c.markets(series, min_close_ts=s_ts, max_close_ts=e_ts)
    # also include markets still open (they have not closed yet, so close_ts filter drops them)
    if end >= datetime.now(timezone.utc):
        live = {m["ticker"] for m in mk}
        mk += [m for m in c.paginate("/markets", "markets", series_ticker=series, status="open")
               if m["ticker"] not in live]
    if not mk:
        print(f"[{series}] no markets in window")
        return
    mdf = markets_frame(mk, series)
    # /historical/markets ignores min/max_close_ts, so filter here
    mdf = mdf[mdf.close_time.isna() | ((mdf.close_time >= start) &
              ((mdf.close_time < end) | mdf.status.isin(["open", "active", "initialized"])))]
    mdf = mdf.reset_index(drop=True)
    (RAW / "markets").mkdir(parents=True, exist_ok=True)
    mdf.to_parquet(RAW / "markets" / f"{series}.parquet", index=False)

    todo = mdf[mdf.volume > 0]
    print(f"[{series}] {len(mdf)} markets, {len(todo)} with volume")
    tdir, cdir = RAW / "trades" / series, RAW / "candles" / series
    tdir.mkdir(parents=True, exist_ok=True)
    cdir.mkdir(parents=True, exist_ok=True)
    n_tr = 0
    for i, m in enumerate(todo.itertuples(index=False), 1):
        tp, cp = tdir / f"{m.ticker}.parquet", cdir / f"{m.ticker}.parquet"
        if tp.exists() and cp.exists() and not force and _readable(tp) and _readable(cp):
            continue
        tr = trades_frame(c.trades(m.ticker))
        tr.to_parquet(tp, index=False)
        n_tr += len(tr)
        o = m.open_time if pd.notna(m.open_time) else start
        cl = m.close_time if pd.notna(m.close_time) else end
        lo = max(int(pd.Timestamp(o).timestamp()), s_ts - 86400 * 30)
        hi = min(int(pd.Timestamp(cl).timestamp()) + 120, int(time.time()))
        cd = candles_frame(c.candlesticks(series, m.ticker, lo, hi, period=1))
        cd.to_parquet(cp, index=False)
        if i % 25 == 0:
            print(f"  [{series}] {i}/{len(todo)} markets, {n_tr:,} trades so far, {time.time()-t0:.0f}s")
    print(f"[{series}] done: {n_tr:,} new trades in {time.time()-t0:.0f}s")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", nargs="*")
    ap.add_argument("--config", default="config/series.csv")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD, markets closing on/after")
    ap.add_argument("--end", default=None, help="YYYY-MM-DD, markets closing before (default now)")
    ap.add_argument("--rps", type=float, default=10.0)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    series = a.series or pd.read_csv(a.config)["series_ticker"].tolist()
    start = datetime.fromisoformat(a.start).replace(tzinfo=timezone.utc)
    end = (datetime.fromisoformat(a.end).replace(tzinfo=timezone.utc) if a.end
           else datetime.now(timezone.utc))
    c = KalshiClient(max_rps=a.rps)
    for s in series:
        try:
            backfill_series(c, s, start, end, a.force)
        except Exception as e:  # keep going; one bad series shouldn't kill the run
            print(f"[{s}] FAILED: {e!r}")


if __name__ == "__main__":
    main()
