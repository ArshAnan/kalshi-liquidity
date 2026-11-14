"""Thin client for Kalshi's public (no-auth) REST market-data API.

Kalshi splits data at a rolling "historical cutoff" (see /historical/cutoff):
markets/trades older than the cutoff only come back from /historical/* endpoints.
This client routes to the right endpoint so callers don't have to care.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Iterator

import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class KalshiClient:
    def __init__(self, max_rps: float = 10.0, timeout: float = 30.0):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "kalshi-liquidity-research/0.1"
        self.min_gap = 1.0 / max_rps
        self.timeout = timeout
        self._last = 0.0
        self._cutoff: dict | None = None

    # ---- low level -------------------------------------------------------
    def get(self, path: str, **params) -> dict:
        params = {k: v for k, v in params.items() if v is not None}
        last_err = None
        # ~15 min of retries in total, so a Wi-Fi drop or short API outage doesn't kill a long backfill
        for attempt in range(20):
            wait = self.min_gap - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.s.get(BASE + path, params=params, timeout=self.timeout)
            except requests.RequestException as e:
                last_err = repr(e)
                time.sleep(min(60, 2**attempt))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last_err = f"HTTP {r.status_code}"
                time.sleep(min(60, 2**attempt))
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"gave up on {path} {params}: last error {last_err}")

    def paginate(self, path: str, key: str, limit: int = 1000, **params) -> Iterator[dict]:
        cursor = None
        while True:
            d = self.get(path, limit=limit, cursor=cursor, **params)
            yield from d.get(key) or []
            cursor = d.get("cursor") or d.get("next_cursor")
            if not cursor:
                return

    # ---- reference data --------------------------------------------------
    def cutoff(self) -> dict:
        if self._cutoff is None:
            self._cutoff = self.get("/historical/cutoff")
        return self._cutoff

    def trades_cutoff(self) -> datetime:
        return parse_ts(self.cutoff()["trades_created_ts"])

    def market_cutoff(self) -> datetime:
        return parse_ts(self.cutoff()["market_settled_ts"])

    def series(self, ticker: str) -> dict:
        return self.get(f"/series/{ticker}")["series"]

    def all_series(self) -> list[dict]:
        return self.get("/series", limit=None)["series"]

    def markets(self, series_ticker: str, include_historical: bool = True, **params) -> list[dict]:
        """All markets in a series: live (open/closed/settled since cutoff) + historical."""
        out = {m["ticker"]: m for m in self.paginate("/markets", "markets", series_ticker=series_ticker, **params)}
        if include_historical:
            for m in self.paginate("/historical/markets", "markets", series_ticker=series_ticker, **params):
                out.setdefault(m["ticker"], m)
        return list(out.values())

    # ---- time series -----------------------------------------------------
    def trades(self, ticker: str, min_ts: int | None = None, max_ts: int | None = None) -> list[dict]:
        """Every public fill for one market. Pulls both live and historical stores."""
        seen, out = set(), []
        for path in ("/markets/trades", "/historical/trades"):
            for t in self.paginate(path, "trades", ticker=ticker, min_ts=min_ts, max_ts=max_ts):
                if t["trade_id"] not in seen:
                    seen.add(t["trade_id"])
                    out.append(t)
        return out

    def candlesticks(self, series_ticker: str, ticker: str, start_ts: int, end_ts: int,
                     period: int = 1) -> list[dict]:
        """1-minute (period=1), hourly (60) or daily (1440) candles incl. yes_bid / yes_ask OHLC.

        The API caps one request at 5000 candles, so we chunk.
        """
        span = 5000 * 60 * period - 60
        out, seen = [], set()
        for path in (f"/series/{series_ticker}/markets/{ticker}/candlesticks",
                     f"/historical/markets/{ticker}/candlesticks"):
            s = start_ts
            while s < end_ts:
                e = min(end_ts, s + span)
                try:
                    d = self.get(path, start_ts=s, end_ts=e, period_interval=period)
                except requests.HTTPError:
                    break
                for c in d.get("candlesticks") or []:
                    if c["end_period_ts"] not in seen:
                        seen.add(c["end_period_ts"])
                        out.append(c)
                s = e
            if out:
                break
        out.sort(key=lambda c: c["end_period_ts"])
        return out

    def orderbook(self, ticker: str, depth: int | None = None) -> dict:
        return self.get(f"/markets/{ticker}/orderbook", depth=depth)
