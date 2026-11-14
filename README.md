# Kalshi Liquidity Project (Team Bad Fills Only)

**Question:** where on Kalshi does providing liquidity pay, and why? We measure maker
P&L as

```
Edge = spread captured − markout (adverse selection) − fees + Liquidity Incentive Program (LIP) payments
```

and build a quoting strategy that targets the markets where that number is positive.
We present at LinkedIn NYC on **Dec 14, 2026**. See [docs/NEXT_STEPS.md](docs/NEXT_STEPS.md) for the plan and who owns what.

## Data: what we use and where it comes from

Kalshi's public REST API (`https://api.elections.kalshi.com/trade-api/v2`) gives us
everything below **without an API key**. That replaces the old plan of relying only on
a self-run logger, which the team never got running, and the Kaggle dataset.

| Table | Endpoint | What it gives us | Range |
|---|---|---|---|
| `markets` | `/markets`, `/historical/markets` | every market in a series, open/close times, **result** (settlement) | full history |
| `trades` | `/markets/trades`, `/historical/trades` | every public fill: µs timestamp, price, size, **taker side** | full history |
| `candles` | `/series/{s}/markets/{t}/candlesticks` | 1‑minute OHLC of **best YES bid and ask**, so we have a BBO at 1‑minute resolution | full history |
| `incentive_programs` | `/incentive_programs?status=all` | **every LIP reward period**: market, start/end, $ reward, target size, discount factor | Sep 2025 → now |
| `orderbook` | `/markets/{t}/orderbook` | full depth, now only (no history) → we snapshot it ourselves | live only |

Older data moves to `/historical/*` after a rolling cutoff (`/historical/cutoff`; in Nov 2026
it was about 2 months back). `kalshi_liq/client.py` checks both stores for you.

**What this data cannot do:** history only resolves the bid/ask to 1 minute, so
5‑second and 30‑second markouts and queue position need our own live order-book
capture (`lip.py snap` now; a WebSocket logger later, which needs an API key).

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 1. Every LIP reward period ever (~45s, ~417k rows)
.venv/bin/python -m kalshi_liq.incentives

# 2. Rank all ~4.9k active series by volume (~3 min). Used to pick config/series.csv
.venv/bin/python -m kalshi_liq.universe

# 3. Trades + 1-min quotes + settlements for the study series (resumable)
.venv/bin/python -m kalshi_liq.backfill --start 2026-10-14

# 4. Markouts per trade + summary table + chart
.venv/bin/python -m kalshi_liq.markouts

# 5. (live) snapshot full order books of the top-paying LIP markets every 5s
.venv/bin/python -m kalshi_liq.lip snap --top 40 --interval 5 --hours 24
```

## Layout

```
config/series.csv          study universe: 15 series, 7 categories, LIP on / off / ended
kalshi_liq/client.py       REST client: rate limit, retries, pagination, live+historical routing
kalshi_liq/incentives.py   LIP reward periods → data/incentives/
kalshi_liq/universe.py     series ranking → data/universe_candidates.csv
kalshi_liq/backfill.py     markets/trades/candles → data/raw/ (Parquet, one file per market)
kalshi_liq/markouts.py     data/derived/fills_with_markouts/{series}.parquet + summary CSV + chart
kalshi_liq/lip.py          LIP score calculator + order-book snapshotter
docs/NEXT_STEPS.md         plan, roles, gates
```

`data/` is git-ignored; anyone can rebuild it from the commands above.

## Conventions

- Prices are YES prices in dollars (0–1). `taker_dir` = +1 if the taker bought YES.
- All markouts are from the **maker's** side, in cents per contract, weighted by contracts.
- Point-in-time: the mid used at trade time `t` comes from the last 1‑minute candle that
  closed **before** `t`. Never join on data stamped after the event.
- `period_reward` in the API is in 1/10,000 dollars; `discount_factor_bps` in basis points.
