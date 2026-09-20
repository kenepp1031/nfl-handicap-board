"""Public betting splits + live lines (framework §3.11), read from Action
Network's free NFL public-betting page. Market-sentiment input only -- never
fed into grading, stored raw so the divergence threshold stays adjustable
without a backfill. Also updates games.closing_spread/total for the current
week (nflverse's own line only lands after the game is over), and logs every
snapshot to line_history so the dashboard can show line movement.

Source note (2026-09-20): this used to scrape DraftKings Network's own
betting-splits page. That page now renders its table empty -- DK's widget
gets a 403 from DK's own backend and ships a hidden "Unable to fetch data
from server. 403" in place of the rows -- so the scrape silently froze and
the dashboard kept showing splits from the last good pull. Action Network
publishes the same numbers per book, DraftKings included, so the figures
stay like-for-like; we just read them from a source that still answers.

Page structure (this is what breaks when Action Network redesigns, not the
URL): a Next.js page with one `<script id="__NEXT_DATA__">` JSON blob.
props.pageProps.scoreboardResponse.games is the current week's games -- each
has `teams` (id/abbr), home_team_id/away_team_id, season, week, and `markets`
keyed by Action Network book id. markets[book].event.spread is two entries
(side "home"/"away") and .total is two ("over"/"under"), each carrying
`value` (the posted number), `odds`, and bet_info.tickets.percent /
bet_info.money.percent -- our bets_pct and handle_pct. The page only ever
carries the current week, so refresh_week checks the page's own week before
writing anything.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from common import fetch_text
from db.db import connect

AN_SPLITS_URL = "https://www.actionnetwork.com/nfl/public-betting"

# Action Network book ids. DraftKings first so the dashboard keeps showing the
# book it always did; Consensus is the fallback for a game DK hasn't posted
# (in practice the two run within a point or two of each other).
BOOK_PREFERENCE = ("68", "15")

NEXT_DATA_RE = re.compile(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

# Action Network's abbreviations differ from nflverse's in exactly one spot.
AN_CODE_TO_ABBR = {"JAC": "JAX"}


def _abbr(code: str) -> str:
    return AN_CODE_TO_ABBR.get(code, code)


def _pick_book(markets: dict) -> dict:
    """First preferred book that actually carries splits for this game, as
    {'spread': [...], 'total': [...]}. Empty dict if none of them do."""
    for book_id in BOOK_PREFERENCE:
        event = (markets.get(book_id) or {}).get("event") or {}
        rows = (event.get("spread") or []) + (event.get("total") or [])
        if any(r.get("bet_info") for r in rows):
            return event
    return {}


def _sides(rows) -> dict[str, dict]:
    """{side: row} for the rows that carry both a posted number and splits."""
    out = {}
    for r in rows or []:
        if r.get("bet_info") and r.get("value") is not None:
            out[r.get("side")] = r
    return out


def _pcts(row: dict) -> tuple[int, int]:
    """(bets_pct, handle_pct) -- tickets is the count, money is the handle."""
    info = row["bet_info"]
    return int(info["tickets"]["percent"]), int(info["money"]["percent"])


def fetch_games() -> list[dict]:
    """Returns a list of parsed game dicts: {season, week, home_abbr, away_abbr,
    home_spread, total, home_bets_pct, home_handle_pct, away_bets_pct,
    away_handle_pct, over_bets_pct, over_handle_pct, under_bets_pct,
    under_handle_pct}. Any field it couldn't parse for a game is left out of
    that game's dict rather than guessed."""
    raw = fetch_text(AN_SPLITS_URL, timeout=40)
    blob = NEXT_DATA_RE.search(raw)
    if not blob:
        return []
    page = json.loads(blob.group(1))
    scoreboard = page["props"]["pageProps"]["scoreboardResponse"]["games"]

    games = []
    for g in scoreboard:
        by_id = {t["id"]: _abbr(t["abbr"]) for t in g.get("teams") or []}
        home, away = by_id.get(g.get("home_team_id")), by_id.get(g.get("away_team_id"))
        if not home or not away:
            continue
        game = {
            "season": g.get("season"),
            "week": g.get("week"),
            "home_abbr": home,
            "away_abbr": away,
        }
        event = _pick_book(g.get("markets") or {})

        spread = _sides(event.get("spread"))
        if "home" in spread and "away" in spread:
            # `value` is each side's own posted number (favorite negative);
            # our storage convention is positive = home favored, so flip sign.
            game["home_spread"] = -float(spread["home"]["value"])
            game["home_bets_pct"], game["home_handle_pct"] = _pcts(spread["home"])
            game["away_bets_pct"], game["away_handle_pct"] = _pcts(spread["away"])

        total = _sides(event.get("total"))
        if "over" in total and "under" in total:
            game["total"] = float(total["over"]["value"])
            game["over_bets_pct"], game["over_handle_pct"] = _pcts(total["over"])
            game["under_bets_pct"], game["under_handle_pct"] = _pcts(total["under"])

        games.append(game)
    return games


def refresh_week(season: int, week: int) -> int:
    try:
        parsed_games = fetch_games()
    except Exception:
        return 0
    # The page only ever carries the current week. Writing its numbers onto a
    # different week would silently mislabel them, so drop anything that isn't
    # the week we were asked for.
    by_matchup = {
        (g["away_abbr"], g["home_abbr"]): g
        for g in parsed_games
        if g.get("season") == season and g.get("week") == week
    }
    if not by_matchup:
        return 0
    checked = datetime.now(timezone.utc).isoformat(timespec="minutes")
    updated = 0
    with connect() as con:
        games = con.execute(
            "SELECT game_id, home_abbr, away_abbr, home_score FROM games WHERE season=? AND week=?",
            (season, week),
        ).fetchall()
        for g in games:
            values = by_matchup.get((g["away_abbr"], g["home_abbr"]))
            if not values or ("home_spread" not in values and "total" not in values):
                continue
            spread = values.get("home_spread")
            total = values.get("total")
            con.execute(
                """INSERT INTO line_history(game_id, checked_at, spread, total) VALUES(?,?,?,?)
                   ON CONFLICT(game_id, checked_at) DO UPDATE SET spread=excluded.spread, total=excluded.total""",
                (g["game_id"], checked, spread, total),
            )
            # Only let the live pull move games.closing_spread/total while the game
            # hasn't been played -- once final, nflverse's own historical closing line
            # (ingest/nflverse_games.py) is authoritative and shouldn't be clobbered.
            if g["home_score"] is None and (spread is not None or total is not None):
                con.execute(
                    "UPDATE games SET closing_spread=COALESCE(?, closing_spread), "
                    "closing_total=COALESCE(?, closing_total) WHERE game_id=?",
                    (spread, total, g["game_id"]),
                )
            if "home_bets_pct" in values:
                for bet_type, side, bets_pct, handle_pct in (
                    ("spread", "home", values["home_bets_pct"], values["home_handle_pct"]),
                    ("spread", "away", values["away_bets_pct"], values["away_handle_pct"]),
                ):
                    con.execute(
                        """INSERT INTO splits(game_id, bet_type, side, bets_pct, handle_pct, checked_at)
                           VALUES(?,?,?,?,?,?)
                           ON CONFLICT(game_id, bet_type, side) DO UPDATE SET
                               bets_pct=excluded.bets_pct, handle_pct=excluded.handle_pct,
                               checked_at=excluded.checked_at""",
                        (g["game_id"], bet_type, side, bets_pct, handle_pct, checked),
                    )
            if "over_bets_pct" in values:
                for bet_type, side, bets_pct, handle_pct in (
                    ("total", "over", values["over_bets_pct"], values["over_handle_pct"]),
                    ("total", "under", values["under_bets_pct"], values["under_handle_pct"]),
                ):
                    con.execute(
                        """INSERT INTO splits(game_id, bet_type, side, bets_pct, handle_pct, checked_at)
                           VALUES(?,?,?,?,?,?)
                           ON CONFLICT(game_id, bet_type, side) DO UPDATE SET
                               bets_pct=excluded.bets_pct, handle_pct=excluded.handle_pct,
                               checked_at=excluded.checked_at""",
                        (g["game_id"], bet_type, side, bets_pct, handle_pct, checked),
                    )
            updated += 1
    return updated


if __name__ == "__main__":
    from db.db import init_db
    init_db()
    season, week = int(sys.argv[1]), int(sys.argv[2])
    print(f"Updated lines/splits for {refresh_week(season, week)} games")
