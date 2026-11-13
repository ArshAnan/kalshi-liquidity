"""Pull every Liquidity Incentive Program (LIP) reward period from Kalshi's public API.

Usage:  python -m kalshi_liq.incentives
  -> data/incentives/incentive_programs.parquet   one row per (market, reward period)
  -> data/incentives/lip_by_series.csv             rewards rolled up by series

Program rules (help.kalshi.com/en/articles/13823851): the book is sampled once a
second; per side, qualifying depth is everything needed to reach `target_size`,
orders behind the reference price are discounted by discount_factor**ticks, your
score is your share of qualifying size, and the period reward is split by share
(times the fraction of snapshots where BOTH sides reached target size).

Units, as observed in the API: `period_reward` is in 1/10,000 of a dollar
(3,000,000 -> $300), `discount_factor_bps` in basis points (5000 -> 0.50).
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from .client import KalshiClient

REWARD_UNIT = 10_000  # period_reward units per dollar


def fetch_all(c: KalshiClient) -> pd.DataFrame:
    rows = list(c.paginate("/incentive_programs", "incentive_programs", status="all"))
    if not rows:  # older API versions may not accept status=all
        rows = list(c.paginate("/incentive_programs", "incentive_programs"))
    df = pd.DataFrame(rows).drop_duplicates("id")
    df["start_date"] = pd.to_datetime(df["start_date"], utc=True, format="ISO8601")
    df["end_date"] = pd.to_datetime(df["end_date"], utc=True, format="ISO8601")
    df["reward_usd"] = df["period_reward"] / REWARD_UNIT
    df["discount_factor"] = df["discount_factor_bps"] / 10_000
    df["target_size"] = df["target_size_fp"].astype(float)
    df["days"] = (df["end_date"] - df["start_date"]).dt.total_seconds() / 86400
    df["reward_usd_per_day"] = df["reward_usd"] / df["days"].clip(lower=1 / 24)
    return df


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/incentives")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    df = fetch_all(KalshiClient())
    df.to_parquet(out / "incentive_programs.parquet", index=False)
    now = pd.Timestamp.now(tz="UTC")
    df["active_now"] = (df.start_date <= now) & (df.end_date > now)

    by = (df.groupby("series_ticker")
            .agg(n_periods=("id", "size"),
                 n_markets=("market_ticker", "nunique"),
                 total_reward_usd=("reward_usd", "sum"),
                 active_reward_usd_per_day=("reward_usd_per_day", lambda s: s[df.loc[s.index, "active_now"]].sum()),
                 median_target_size=("target_size", "median"),
                 median_discount=("discount_factor", "median"),
                 first_start=("start_date", "min"),
                 last_end=("end_date", "max"))
            .sort_values("total_reward_usd", ascending=False))
    by.to_csv(out / "lip_by_series.csv")
    print(f"{len(df)} reward periods, {df.market_ticker.nunique()} markets, "
          f"{df.series_ticker.nunique()} series, ${df.reward_usd.sum():,.0f} total; "
          f"{df.active_now.sum()} active now")
    print(f"date range {df.start_date.min()} -> {df.end_date.max()}")
    print(by.head(30).to_string())


if __name__ == "__main__":
    main()
