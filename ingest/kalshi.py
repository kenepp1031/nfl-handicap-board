"""Kalshi's own NFL winner market per game -- price and traded volume per team.

Market-sentiment input only, same standing as `splits`: never fed into
grading. Where the book splits say what share of *tickets* and *handle* leaned
each way, Kalshi says what real money actually paid for each side on an
exchange, and how much of it there was. The two answer different questions, so
this is stored in its own table and shown above the book splits rather than
replacing them.

Why this source: Kalshi publishes a documented public API with no key, no
account and no quota, so unlike a scraped splits page it can't quietly rot
(see ingest/dk_lines.py for what that failure looks like).

Shape: GET markets?series_ticker=KXNFLGAME, paged by cursor, returns two
markets per game -- one per team, each a "<team> wins" binary. Ticker is
KXNFLGAME-<YYMONDD><AWAY><HOME>-<TEAM>, so the event ticker's tail gives home
vs away once you know the two team codes. Prices are dollars per $1 contract,
so `last_price_dollars` of 0.7500 reads straight off as a 75% implied win
probability. The two sides sum to a little over 100% -- that gap is the
bid/ask spread, and it's left in rather than normalised away so the number on
screen is the number Kalshi shows.
"""
from __future__ import annotations

import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from common import fetch_json
from db.db import connect

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2/"
SERIES_TICKER = "KXNFLGAME"
MAX_PAGES = 12

# Kalshi's team codes differ from nflverse's in the same two spots most feeds do.
KALSHI_CODE_TO_ABBR = {"JAC": "JAX", "LAR": "LA"}


def _abbr(code: str) -> str:
    return KALSHI_CODE_TO_ABBR.get(code, code)


def _dollars(value) -> float | None:
    """Kalshi returns its money and size fields as decimal strings."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _price(market: dict) -> float | None:
    """Implied probability for this side, 0-1. Last trade when there is one,
    otherwise the mid of the current quote -- a market that has been quoted but
    not yet traded reports last_price 0, which is a missing price, not a 0%
    chance."""
    last = _dollars(market.get("last_price_dollars"))
    if last:
        return last
    bid = _dollars(market.get("yes_bid_dollars"))
    ask = _dollars(market.get("yes_ask_dollars"))
    if bid is not None and ask is not None and (bid or ask):
        return (bid + ask) / 2
    return None


def fetch_markets() -> list[dict]:
    """Returns one dict per game: {home_abbr, away_abbr, home_ticker,
    away_ticker, home_price, away_price, home_volume, away_volume,
    home_open_interest, away_open_interest}. Games whose two sides can't be
    matched up are skipped rather than guessed at."""
    markets, cursor = [], None
    for _ in range(MAX_PAGES):
        query = {"series_ticker": SERIES_TICKER, "limit": 200}
        if cursor:
            query["cursor"] = cursor
        page = fetch_json(KALSHI_API + "markets?" + urllib.parse.urlencode(query), timeout=40)
        batch = page.get("markets") or []
        markets += batch
        cursor = page.get("cursor")
        if not cursor or not batch:
            break

    by_event: dict[str, list[dict]] = {}
    for m in markets:
        by_event.setdefault(m.get("event_ticker") or "", []).append(m)

    games = []
    for event_ticker, sides in by_event.items():
        if len(sides) != 2:
            continue
        codes = [s["ticker"].rsplit("-", 1)[-1] for s in sides]
        by_code = dict(zip(codes, sides))
        # The event ticker ends with AWAY+HOME, so whichever ordering of the two
        # codes matches the tail tells us which team is at home.
        tail = event_ticker.rsplit("-", 1)[-1]
        away_code = home_code = None
        for a, b in ((codes[0], codes[1]), (codes[1], codes[0])):
            if tail.endswith(a + b):
                away_code, home_code = a, b
                break
        if not home_code:
            continue

        home, away = by_code[home_code], by_code[away_code]
        games.append({
            "home_abbr": _abbr(home_code),
            "away_abbr": _abbr(away_code),
            "home_ticker": home["ticker"],
            "away_ticker": away["ticker"],
            "home_price": _price(home),
            "away_price": _price(away),
            "home_volume": _dollars(home.get("volume_fp")),
            "away_volume": _dollars(away.get("volume_fp")),
            "home_open_interest": _dollars(home.get("open_interest_fp")),
            "away_open_interest": _dollars(away.get("open_interest_fp")),
        })
    return games


def refresh_week(season: int, week: int) -> int:
    try:
        parsed = fetch_markets()
    except Exception:
        return 0
    by_matchup = {(g["away_abbr"], g["home_abbr"]): g for g in parsed}
    if not by_matchup:
        return 0
    checked = datetime.now(timezone.utc).isoformat(timespec="minutes")
    updated = 0
    with connect() as con:
        games = con.execute(
            "SELECT game_id, home_abbr, away_abbr FROM games WHERE season=? AND week=?",
            (season, week),
        ).fetchall()
        for g in games:
            values = by_matchup.get((g["away_abbr"], g["home_abbr"]))
            if not values:
                continue
            # A game with no price on either side has nothing to show; leave whatever
            # is already stored alone rather than blanking it out.
            if values["home_price"] is None and values["away_price"] is None:
                continue
            for side in ("home", "away"):
                con.execute(
                    """INSERT INTO kalshi_markets(game_id, side, ticker, price, volume,
                                                  open_interest, checked_at)
                       VALUES(?,?,?,?,?,?,?)
                       ON CONFLICT(game_id, side) DO UPDATE SET
                           ticker=excluded.ticker, price=excluded.price,
                           volume=excluded.volume, open_interest=excluded.open_interest,
                           checked_at=excluded.checked_at""",
                    (g["game_id"], side, values[f"{side}_ticker"], values[f"{side}_price"],
                     values[f"{side}_volume"], values[f"{side}_open_interest"], checked),
                )
            updated += 1
    return updated


if __name__ == "__main__":
    from db.db import init_db
    init_db()
    season, week = int(sys.argv[1]), int(sys.argv[2])
    print(f"Updated Kalshi markets for {refresh_week(season, week)} games")
