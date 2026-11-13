"""Maker-side markouts for every public Kalshi trade, from backfilled trades + 1-minute candles.

Usage:  python -m kalshi_liq.markouts
  -> data/derived/fills_with_markouts.parquet   one row per trade
  -> reports/markout_summary.csv                per series x LIP on/off
  -> reports/markout_curves.png

Definitions (project doc, maker's point of view, d = +1 if taker bought YES):
  effective half-spread  es      = d * (p - m_t)
  adverse selection      AS(h)   = d * (m_{t+h} - m_t)
  realized half-spread   rs(h)   = es - AS(h) = d * (p - m_{t+h})
  hold to settlement     pnl_set = d * (p - settle)      settle in {0, 1}

m_t is the mid from the last 1-minute candle that CLOSED strictly before the trade
(point-in-time: nothing after recv time is used). When t+h is past the market's close
we use the settlement value instead. 1-minute candles cannot resolve the 5s/30s
horizons in the project doc; those need the live order-book logger.
"""
from __future__ import annotations

import argparse
import glob
import math
from pathlib import Path

import numpy as np
import pandas as pd

RAW, DERIVED, REPORTS = Path("data/raw"), Path("data/derived"), Path("reports")
HORIZONS_MIN = [1, 5, 15, 60, 240]
MAX_SPREAD = 0.25  # ignore quotes wider than this when forming the mid


def taker_fee(p: np.ndarray) -> np.ndarray:
    """Kalshi taker fee per contract, 0.07 * P * (1-P).

    Kalshi charges ceil(0.07 * C * P * (1-P)) rounded up to the cent per ORDER of C
    contracts, so per contract the unrounded value is the right average. Rounding up
    per contract would overstate fees (e.g. 0.44c -> 1c)."""
    return 0.07 * p * (1 - p)


def maker_fee(p: np.ndarray) -> np.ndarray:
    """Maker fee per contract on series with fee_type 'quadratic_with_maker_fees',
    0.0175 * P * (1-P), same per-order rounding note as taker_fee.
    Treat the coefficient as an assumption until checked against Kalshi's fee schedule."""
    return 0.0175 * p * (1 - p)


def lip_windows() -> pd.DataFrame:
    p = Path("data/incentives/incentive_programs.parquet")
    if not p.exists():
        return pd.DataFrame(columns=["market_ticker", "start_date", "end_date", "reward_usd_per_day"])
    return pd.read_parquet(p, columns=["market_ticker", "start_date", "end_date", "reward_usd_per_day"])


def mid_frame(c: pd.DataFrame) -> pd.DataFrame:
    c = c.sort_values("end_ts")
    ok = (c.bid_close > 0) & (c.ask_close < 1) & (c.ask_close > c.bid_close) & \
         ((c.ask_close - c.bid_close) <= MAX_SPREAD)
    return pd.DataFrame({"end_ts": c.end_ts,
                         "mid": np.where(ok, (c.bid_close + c.ask_close) / 2, np.nan),
                         "spread": np.where(ok, c.ask_close - c.bid_close, np.nan)})


def market_markouts(tr: pd.DataFrame, cd: pd.DataFrame, mkt: pd.Series) -> pd.DataFrame:
    if tr.empty or cd.empty:
        return pd.DataFrame()
    mids = mid_frame(cd)
    tr = tr.sort_values("ts").copy()
    # m_t: last candle closed strictly before the trade
    a = pd.merge_asof(tr[["ts"]], mids.rename(columns={"end_ts": "q_ts"}),
                      left_on="ts", right_on="q_ts", direction="backward", allow_exact_matches=False)
    tr["mid_t"], tr["spread_t"] = a["mid"].values, a["spread"].values
    tr["quote_age_s"] = (tr["ts"] - a["q_ts"]).dt.total_seconds().values

    settle = {"yes": 1.0, "no": 0.0}.get(mkt.get("result"), np.nan)
    close = mkt.get("close_time")
    tr["settle"] = settle
    d, p = tr["taker_dir"].astype(float), tr["yes_price"]
    tr["eff_hs"] = d * (p - tr["mid_t"])
    for h in HORIZONS_MIN:
        tgt = tr["ts"] + pd.Timedelta(minutes=h)
        b = pd.merge_asof(pd.DataFrame({"tgt": tgt}).reset_index(), mids, left_on="tgt",
                          right_on="end_ts", direction="backward").set_index("index").sort_index()
        m_h = b["mid"].values
        if pd.notna(close) and not math.isnan(settle):
            m_h = np.where(tgt.values >= np.datetime64(close.tz_convert(None)), settle, m_h)
        tr[f"as_{h}m"] = d * (m_h - tr["mid_t"])
        tr[f"rs_{h}m"] = tr["eff_hs"] - tr[f"as_{h}m"]
    tr["pnl_settle"] = d * (p - settle)
    tr["taker_fee"] = taker_fee(p.values)
    tr["maker_fee_if_charged"] = maker_fee(p.values)
    return tr


def tag_lip(df: pd.DataFrame, lip: pd.DataFrame) -> pd.Series:
    if lip.empty or df.empty:
        return pd.Series(False, index=df.index)
    l = lip[lip.market_ticker.isin(df.ticker.unique())]
    on = pd.Series(False, index=df.index)
    for t, g in l.groupby("market_ticker"):
        idx = df.index[df.ticker == t]
        ts = df.loc[idx, "ts"]
        hit = np.zeros(len(idx), dtype=bool)
        for s, e in zip(g.start_date, g.end_date):
            hit |= ((ts >= s) & (ts < e)).values
        on.loc[idx] = hit
    return on


def wavg(x: pd.Series, w: pd.Series) -> float:
    m = x.notna()
    return float(np.average(x[m], weights=w[m])) if m.any() and w[m].sum() > 0 else np.nan


def summarize(f: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (s, lip), g in f.groupby(["series_ticker", "lip_on"]):
        g = g[g.mid_t.notna()]
        if len(g) < 50:
            continue
        w = g["count"]
        r = dict(series_ticker=s, lip_on=lip, n_trades=len(g), contracts=w.sum(),
                 notional_usd=(g.yes_price * w).sum(),
                 avg_quoted_spread_c=100 * wavg(g.spread_t, w),
                 eff_half_spread_c=100 * wavg(g.eff_hs, w))
        for h in HORIZONS_MIN:
            r[f"as_{h}m_c"] = 100 * wavg(g[f"as_{h}m"], w)
        r["realized_5m_c"] = 100 * wavg(g["rs_5m"], w)
        r["pnl_to_settle_c"] = 100 * wavg(g.pnl_settle, w)
        r["taker_fee_c"] = 100 * wavg(g.taker_fee, w)
        r["maker_fee_c"] = 100 * wavg(g.maker_fee_if_charged, w)
        rows.append(r)
    return pd.DataFrame(rows).round(3)


def plot(summary: pd.DataFrame, path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(10, 6))
    xs = [0] + HORIZONS_MIN
    for _, r in summary.sort_values("contracts", ascending=False).head(14).iterrows():
        ys = [r.eff_half_spread_c] + [r.eff_half_spread_c - r[f"as_{h}m_c"] for h in HORIZONS_MIN]
        ax.plot(range(len(xs)), ys, marker="o", lw=1.4,
                ls="-" if r.lip_on else "--", label=f"{r.series_ticker}{' (LIP)' if r.lip_on else ''}")
    ax.axhline(0, color="grey", lw=0.8)
    ax.set_xticks(range(len(xs)), ["trade"] + [f"{h}m" for h in HORIZONS_MIN])
    ax.set_ylabel("maker realized half-spread (cents / contract)")
    ax.set_title("Kalshi maker markouts: what a passive maker kept after h minutes")
    ax.legend(fontsize=7, ncol=2)
    fig.tight_layout()
    fig.savefig(path, dpi=140)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", nargs="*")
    a = ap.parse_args()
    DERIVED.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)
    lip = lip_windows()

    series = a.series or sorted(Path(p).stem for p in glob.glob(str(RAW / "markets" / "*.parquet")))
    out = []
    for s in series:
        mk = pd.read_parquet(RAW / "markets" / f"{s}.parquet").set_index("ticker")
        parts = []
        for tp in glob.glob(str(RAW / "trades" / s / "*.parquet")):
            t = Path(tp).stem
            cp = RAW / "candles" / s / f"{t}.parquet"
            if not cp.exists() or t not in mk.index:
                continue
            parts.append(market_markouts(pd.read_parquet(tp), pd.read_parquet(cp), mk.loc[t]))
        parts = [p for p in parts if not p.empty]
        if not parts:
            continue
        df = pd.concat(parts, ignore_index=True)
        df.insert(0, "series_ticker", s)
        df["lip_on"] = tag_lip(df, lip)
        out.append(df)
        print(f"{s}: {len(df):,} trades, {df.mid_t.notna().mean():.0%} with a valid prior mid, "
              f"{df.lip_on.mean():.0%} during LIP")
    f = pd.concat(out, ignore_index=True)
    f.to_parquet(DERIVED / "fills_with_markouts.parquet", index=False)
    summ = summarize(f)
    summ.to_csv(REPORTS / "markout_summary.csv", index=False)
    plot(summ, REPORTS / "markout_curves.png")
    pd.set_option("display.width", 250)
    print(summ.to_string(index=False))


if __name__ == "__main__":
    main()
