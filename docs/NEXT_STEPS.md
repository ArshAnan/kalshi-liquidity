# Kalshi Liquidity Project: where we are and what's next

_Written Nov 13, 2026 by Arsh. Branch `arsh/data-pipeline-bootstrap`._

## 1. Where we actually are

The project doc describes a 24/7 logger, a 97.9%-validated book, and an Oct 25 "first
real number" gate. **None of that exists in our repo**, and both gates (Oct 25 and
Nov 15) have passed or are about to. We have **31 days to Dec 14**, so the plan
changes from "build a logger and wait for data" to **"use the history Kalshi already
publishes, and record only what history can't give us."**

What this branch adds (all runnable today, no API key):

| Done | What | Where |
|---|---|---|
| ✅ | Complete LIP history: 417k reward periods, 335k markets, 5,079 series, **$28.5M** in rewards since Sep 2025. About **$204k/day is live right now** across 10.7k markets | `kalshi_liq/incentives.py` |
| ✅ | Every active series ranked by 24h volume, spread and category | `kalshi_liq/universe.py` |
| ✅ | Backfill of markets, every trade (with taker side) and 1‑min best bid/ask for 15 series | `kalshi_liq/backfill.py` |
| ✅ | Markouts for every trade at 1/5/15/60/240 min and to settlement, with fees and an LIP-on flag | `kalshi_liq/markouts.py` |
| ✅ | LIP score calculator (checked by hand on toy books) + live order-book snapshotter | `kalshi_liq/lip.py` |

**Why the old data was a problem, and what fixes it:** Kaggle data has trades without
quotes, and our own logger only sees the future. Kalshi's API has the past in a form
we can use: every trade with **who was the aggressor**, plus **1‑minute best bid and ask**.
That is enough for effective spread, markouts and adverse selection. It is **not** enough for
sub‑minute markouts or queue position. For those we snapshot books going forward.

### First results (Nov 13, preliminary: 8 series, ~1.8M trades, Oct 14 → Nov 13)

From `reports/markout_summary.csv` and `reports/markout_curves.png`. Cents per contract,
maker's side, contract-weighted. "LIP $/contract" = LIP dollars paid on those markets in
the window ÷ contracts traded while LIP was on. It is the whole maker pool's subsidy
per contract traded, not one maker's take.

| Series | LIP | Realized spread @5m | P&L held to settlement | Maker fee | LIP $/contract |
|---|---|---|---|---|---|
| KXRT (Rotten Tomatoes) | on | −0.49 | −0.56 | 0.16 | **3.39** |
| KXTRUMPACT | on | −2.22 | −3.89 | 0.15 | **4.38** |
| KXYTVIEWSW | on | +1.02 | −2.11 | 0.05 | **2.90** |
| KXCPIYOY | on | +0.95 | −0.12 | 0.21 | 0.78 |
| KXAAAGASD | on / off | +0.29 / **−0.98** | +0.88 / **−1.61** | 0.12 | 0.31 |
| KXRAIN | on / off | +0.55 / +0.26 | +0.19 / +0.68 | 0.22 | 0.19 |
| KXHIGHNY | on / off | +0.44 / +0.63 | +0.52 / +1.37 | 0.20 | 0.07 |
| KXFEDDECISION | off | −0.04 | −1.78 | 0.23 | — |

What this suggests (to be tested properly, with CIs):
- **Where LIP spending is heavy (entertainment, politics), makers lose to adverse selection and
  the subsidy is several times bigger than the loss.** That is the likely headline:
  in these markets the incentive is the business, not the spread.
- **Where LIP is thin (weather, gas), the spread roughly pays for itself.** Daily gas is the
  one series where the maker went from positive to negative after LIP switched off. That's
  the before/after comparison to push on.
- Fed: the spread is almost exactly eaten by markouts, and held to settlement the maker loses.
  This is the information-event pattern from the hypothesis.

**Caveats.** With 1‑minute quotes, effective spread and 1‑minute markouts both include
price moves inside the minute; realized spread (their difference) is the more reliable
number. Large sweeps dominate contract weighting. Small cells (CPIYOY off: 170 trades;
KXRT off: 3.2k trades) are noise. The maker fee coefficient is still unverified (§6).

## 2. The study, re-centered on the Liquidity Incentive Program

The program ([rules](https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program),
[live list](https://kalshi.com/incentives)) pays makers for resting size near the top of
the book, sampled once per second. That gives a clean and presentable question:

> **Do LIP payments cover what makers lose to adverse selection, and in which markets?**

Two things make this answerable with the data we have:

1. **A natural experiment.** LIP was **switched off** on several high-volume recurring
   series while trading continued: weather highs and rain (Oct 29), daily gas (Oct 28),
   15‑minute gold/silver/WTI (Sep 19), and 15‑minute BTC/ETH (May 12). Comparing spreads,
   depth and markouts **before vs after**, against series where nothing changed
   (BTC hourly, NFL, NBA), shows what LIP actually buys: tighter spreads or just paid-for
   size that gets picked off.
2. **A published payout rule.** `lip.py` reproduces the score, so for any live book we can
   compute "if we rest N contracts at price X, we earn Y% of $Z/day." Combine that with
   the markout cost from (1) and you get edge per market.

Primary hypothesis from the doc still holds: adverse selection is concentrated around
information events (CPI 8:30 ET, NWS daily climate reports, game ends, 15‑minute
expiries). The `events` table from the External lane tests it.

## 3. Study universe (`config/series.csv`)

15 series in 7 categories, chosen by volume × LIP status:

- **LIP ended (before/after):** KXHIGHNY, KXRAIN, KXAAAGASD, KXGOLD15M, KXBTC15M
- **LIP active:** KXRT, KXYTVIEWSW, KXCPIYOY, KXTRUMPACT, KXHORMUZWEEKLY, KXLLM1
- **No LIP (controls):** KXBTCD, KXNFLGAME, KXNBAGAME, KXFEDDECISION

Add series with `data/universe_candidates.csv` + `data/incentives/lip_by_series.csv`. Any
series ticker works with `backfill --series`.

## 4. Roles and first tasks (by table produced)

**Agree the table formats at the next meeting**; `README.md` documents the current ones.

### Data Engineering (Aly, Selina)
1. Run the backfill over a **60‑day window** for all 15 series (`--start 2026-09-14`), plus the
   **before/after windows** for each LIP shutoff (e.g. KXGOLD15M Aug 20–Oct 19,
   KXBTC15M Apr 12–Jun 12 via `/historical`). Then check the counts: trades per day per series, % of trades with
   a valid prior mid (currently ~93% on KXHIGHNY).
2. Build `bbo` as a single DuckDB view (forward-filled 1‑min bid/ask per market) and a
   `trades` view across all series.
3. Run `lip.py snap` on a machine that stays on (Oracle free tier or a lab box) for the
   top ~40 LIP markets, and **back it up nightly** (S3/Drive). This is the only data we
   can't recreate later.
4. Stretch: get an API key and add a WebSocket `orderbook_delta` logger for ~20 markets
   so we have sub-second books for the final 3 weeks.

### External Signals (Arsh, Sam)
1. `reference_prices`: 1‑minute BTC, ETH, gold spot (Coinbase/Binance public klines;
   for gold, PAXG or a futures proxy). Join to KXBTCD/KXBTC15M/KXGOLD15M trades and test
   whether takers trade **after** spot moves (latency arbitrage = toxic flow).
2. `events`: exact timestamps for CPI releases (BLS schedule), FOMC statements,
   NWS daily climate report publish times for NYC, and NFL/NBA final-whistle times.
3. `series_labels`: category, frequency, LIP status and dates per series (mostly from
   `lip_by_series.csv`).

### Internal Signals (Sophia)
1. Own `markouts.py` → `fills_with_markouts` and the **accounting library**: per fill,
   spread captured, markout at h, maker fee, LIP credit. Backtesting imports it.
2. Headline table: adverse selection cost by series × LIP on/off, with bootstrap CIs
   clustered by market (trades within a market are not independent).
3. First toxic-flow model: logistic or gradient boosting on `as_5m > spread` using
   only point-in-time features (spread, quote age, time to close, price level,
   trade size, recent signed volume, minutes to nearest event).

### Backtesting (later, whoever frees up first)
1. Naive quoter: rest at best bid/ask on both sides, filled when a trade prints through
   our price. Price P&L with the accounting library and add LIP share from `lip.py`.
2. Model-guided quoter: pull quotes when the toxic-flow model fires.

## 5. Re-baselined gates

| Gate | Date | Must exist |
|---|---|---|
| Data v1 frozen | **Nov 18** | 60‑day backfill for 15 series + LIP history checked; snapshotter running and backed up |
| First real number | **Nov 20** | Markout curve for one series, rebuilt from scratch by someone other than Arsh (KXHIGHNY prelim already exists) |
| Headline finding | **Nov 27** | AS cost by series and LIP on/off, plus one feature that explains it (events or spot moves) |
| LIP economics | **Dec 2** | $ earned per contract-hour resting vs markout lost, per series |
| Code freeze | **Dec 7** | `make all` (or one script) rebuilds every chart in the deck |
| Present | **Dec 14** | LinkedIn NYC, one slide per role |

Scope cuts, in order, if we slip: live trading → WebSocket logger → backtesting
beyond the naive quoter.

## 6. Open questions to resolve this week

- **Maker fee formula.** `markouts.py` assumes 0.0175·P·(1−P) on `quadratic_with_maker_fees`
  series. Check this against Kalshi's current fee schedule.
- **LIP reward units.** We read `period_reward` as 1/10,000 USD (3,000,000 → $300). Check
  one market against what kalshi.com/incentives shows.
- **Who runs the always-on box**, and who is the second person with access.
- **Trade-through fills.** Public trades don't say which resting order was hit, so our "maker"
  is the average maker, not a specific queue position. Say this on the methods slide.
