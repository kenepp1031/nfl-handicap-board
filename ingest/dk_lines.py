"""Public betting splits + live lines (framework §3.11), scraped from
DraftKings Network's betting-splits page. Market-sentiment input only --
never fed into grading, stored raw so the divergence threshold stays
adjustable without a backfill. Also updates games.closing_spread/total for
the current week (nflverse's own line only lands after the game is over), and
logs every snapshot to line_history so the dashboard can show line movement.

Page structure (confirmed against a live fetch -- this is what actually
breaks when DK redesigns the page, not the URL): each game is one
`<div class="tb-se ...">` block. Its title has two `<img src='.../teams/nfl/
{CODE}.png'>` tags around "AWAY_CODE Mascot @ HOME_CODE Mascot". Below that
are three market blocks (Moneyline, Spread, Total) in a fixed order, each
with two rows (home row first, away row second for Moneyline/Spread; Over
then Under for Total) giving "<label> <a>odds</a> <handle%> <bets%>" -- Handle
comes before Bets in the DOM, matching the "% Handle / % Bets" column headers.
The page also paginates via &tb_page=N; there were 2 pages for a normal NFL
week when this was last checked, so a few pages are fetched defensively.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from common import fetch_text
from db.db import connect

DK_SPLITS_URL = "https://dknetwork.draftkings.com/draftkings-sportsbook-betting-splits/?tb_eg=NFL&tb_edate=n7days&itm_content=NFL"
MAX_PAGES = 4

# DK's own team-logo codes differ from nflverse's in the same two spots ESPN's
# do -- Rams and Washington -- assume the same mapping until proven otherwise.
DK_CODE_TO_ABBR = {"LAR": "LA", "WSH": "WAS"}

GAME_BLOCK_RE = re.compile(r'<div class="tb-se border-b.*?(?=<div class="tb-se border-b|\Z)', re.S)
TITLE_RE = re.compile(r"teams/nfl/([A-Z]+)\.png'.*?@\s*<img[^>]*teams/nfl/([A-Z]+)\.png", re.S)
MARKET_HEAD_RE = re.compile(r'<div class="flex-1">(Moneyline|Spread|Total)</div>')
ROW_RE = re.compile(
    r'tb-slipline flex-1 font-medium">([^<]+)</div>.*?>\s*([+−-]?\d+)\s*</a>'
    r'.*?flex-1">(\d+)%.*?flex-1">(\d+)%',
    re.S,
)


def _dk_abbr(code: str) -> str:
    return DK_CODE_TO_ABBR.get(code, code)


def _parse_market_blocks(block_text: str) -> dict[str, list[tuple[str, str, int, int]]]:
    """Splits one game block by its market headers, returns
    {market_name: [(label, odds, handle_pct, bets_pct), ...]} with up to 2 rows per market."""
    heads = list(MARKET_HEAD_RE.finditer(block_text))
    markets: dict[str, list] = {}
    for i, m in enumerate(heads):
        name = m.group(1)
        start = m.end()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(block_text)
        rows = ROW_RE.findall(block_text[start:end])
        markets[name] = [(label, odds, int(handle), int(bets)) for label, odds, handle, bets in rows]
    return markets


def fetch_games() -> list[dict]:
    """Returns a list of parsed game dicts: {home_abbr, away_abbr, home_spread,
    total, home_bets_pct, home_handle_pct, away_bets_pct, away_handle_pct,
    over_bets_pct, over_handle_pct, under_bets_pct, under_handle_pct}. Any
    field it couldn't parse for a game is left out of that game's dict rather
    than guessed."""
    seen_titles = set()
    games = []
    for page in range(1, MAX_PAGES + 1):
        url = DK_SPLITS_URL + f"&tb_page={page}"
        try:
            raw = fetch_text(url, timeout=30)
        except Exception:
            break
        blocks = GAME_BLOCK_RE.findall(raw)
        if not blocks:
            break
        new_this_page = 0
        for block in blocks:
            title = TITLE_RE.search(block)
            if not title:
                continue
            away_abbr, home_abbr = _dk_abbr(title.group(1)), _dk_abbr(title.group(2))
            key = (away_abbr, home_abbr)
            if key in seen_titles:
                continue
            seen_titles.add(key)
            new_this_page += 1
            markets = _parse_market_blocks(block)
            game = {"home_abbr": home_abbr, "away_abbr": away_abbr}

            spread_rows = markets.get("Spread", [])
            if len(spread_rows) == 2:
                home_label, _odds, home_handle, home_bets = spread_rows[0]
                _away_label, _odds2, away_handle, away_bets = spread_rows[1]
                num = re.search(r"([+−-]?\d+\.?\d*)\s*$", home_label)
                if num:
                    # Label is the HOME team's own posted number (favorite negative);
                    # our storage convention is positive = home favored, so flip sign.
                    game["home_spread"] = -float(num.group(1).replace("−", "-"))
                    game["home_bets_pct"] = home_bets
                    game["home_handle_pct"] = home_handle
                    game["away_bets_pct"] = away_bets
                    game["away_handle_pct"] = away_handle

            total_rows = markets.get("Total", [])
            if len(total_rows) == 2:
                over_label, _odds, over_handle, over_bets = total_rows[0]
                _under_label, _odds2, under_handle, under_bets = total_rows[1]
                num = re.search(r"(\d+\.?\d*)", over_label)
                if num:
                    game["total"] = float(num.group(1))
                    game["over_bets_pct"] = over_bets
                    game["over_handle_pct"] = over_handle
                    game["under_bets_pct"] = under_bets
                    game["under_handle_pct"] = under_handle

            games.append(game)
        if new_this_page == 0:
            break
    return games


def refresh_week(season: int, week: int) -> int:
    try:
        parsed_games = fetch_games()
    except Exception:
        return 0
    by_matchup = {(g["away_abbr"], g["home_abbr"]): g for g in parsed_games}
    checked = datetime.now(timezone.utc).isoformat(timespec="minutes")
    updated = 0
    with connect() as con:
        games = con.execute(
            "SELECT game_id, home_abbr, away_abbr, home_score FROM games WHERE season=? AND week=?",
            (season, week),
        ).fetchall()
        for g in games:
            values = by_matchup.get((g["away_abbr"], g["home_abbr"]))
            if not values or "home_spread" not in values and "total" not in values:
                continue
            spread = values.get("home_spread")
            total = values.get("total")
            con.execute(
                """INSERT INTO line_history(game_id, checked_at, spread, total) VALUES(?,?,?,?)
                   ON CONFLICT(game_id, checked_at) DO UPDATE SET spread=excluded.spread, total=excluded.total""",
                (g["game_id"], checked, spread, total),
            )
            # Only let the live scrape move games.closing_spread/total while the game
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
    print(f"Updated DK lines/splits for {refresh_week(season, week)} games")
