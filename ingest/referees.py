"""Referee module (framework §3.7, late-binding): crew-per-game assignments
are scraped from Rotowire (new article each week); per-crew career Home ATS%/
Over% trends come straight from nflverse's own game-by-game history, which
can't drift or break when a stats site redesigns itself. Ported from the old
app's _referee_worker/_compute_referee_stats."""
from __future__ import annotations

import html
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from common import TEAM_NAMES, fetch_text
from db.db import connect
from ingest.nflverse_games import load_games_csv

ROTOWIRE_REFS = "https://www.rotowire.com/football/article/nfl-referee-assignments-betting-trends-by-crew-133202"
NICK_TO_ABBR = {name.split()[-1]: abbr for abbr, name in TEAM_NAMES.items()}


def compute_referee_stats() -> dict[str, dict]:
    rows = load_games_csv()
    agg: dict[str, dict] = {}
    for r in rows:
        referee = (r.get("referee") or "").strip()
        if not referee or r.get("game_type") != "REG" or not r.get("home_score") or not r.get("away_score"):
            continue
        d = agg.setdefault(referee, dict(home_covers=0, ats_games=0, overs=0, ou_games=0))
        if r.get("spread_line"):
            result = int(r["home_score"]) - int(r["away_score"]) - float(r["spread_line"])
            if result != 0:
                d["ats_games"] += 1
                if result > 0:
                    d["home_covers"] += 1
        if r.get("total_line") and r.get("total"):
            total_result = float(r["total"]) - float(r["total_line"])
            if total_result != 0:
                d["ou_games"] += 1
                if total_result > 0:
                    d["overs"] += 1
    stats = {}
    for referee, d in agg.items():
        if d["ats_games"] < 10:
            continue
        stats[referee] = dict(
            games=d["ats_games"],
            home_ats_pct=round(d["home_covers"] / d["ats_games"] * 100, 1),
            over_pct=round(d["overs"] / d["ou_games"] * 100, 1) if d["ou_games"] else None,
        )
    return stats


def scrape_assignments() -> dict[tuple[str, str], str]:
    """Returns {(home_abbr, away_abbr): referee_name}."""
    assignments = {}
    try:
        raw = fetch_text(ROTOWIRE_REFS, timeout=30)
    except Exception:
        return assignments
    match = re.search(
        r"<th><strong>Matchup</strong></th><th><strong>Referee</strong></th></tr></thead><tbody>(.*?)</tbody>",
        raw, re.S,
    )
    if not match:
        return assignments
    for away_nick, home_nick, referee in re.findall(
        r'<td[^>]*>([^<@]+?)\s*@\s*([^<]+?)</td><td[^>]*>([^<]+)</td>', match.group(1)
    ):
        home = NICK_TO_ABBR.get(home_nick.strip())
        away = NICK_TO_ABBR.get(away_nick.strip())
        if home and away:
            assignments[(home, away)] = html.unescape(referee.strip())
    return assignments


def refresh_week(season: int, week: int) -> int:
    stats = compute_referee_stats()
    checked = datetime.now(timezone.utc).isoformat(timespec="minutes")
    updated = 0
    with connect() as con:
        games = con.execute(
            "SELECT game_id, home_abbr, away_abbr, referee FROM games WHERE season=? AND week=?",
            (season, week),
        ).fetchall()
        # nflverse fills games.referee once a game is final; only hit Rotowire
        # for the upcoming week's still-unassigned games.
        assignments = scrape_assignments() if any(not g["referee"] for g in games) else {}
        for g in games:
            referee = g["referee"] or assignments.get((g["home_abbr"], g["away_abbr"]))
            if not referee:
                continue
            crew = stats.get(referee)
            con.execute(
                """INSERT INTO officiating(game_id, referee_name, crew_home_ats_pct, crew_over_pct,
                       crew_games, assigned_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(game_id) DO UPDATE SET
                       referee_name=excluded.referee_name, crew_home_ats_pct=excluded.crew_home_ats_pct,
                       crew_over_pct=excluded.crew_over_pct, crew_games=excluded.crew_games,
                       assigned_at=excluded.assigned_at""",
                (g["game_id"], referee, crew["home_ats_pct"] if crew else None,
                 crew["over_pct"] if crew else None, crew["games"] if crew else None, checked),
            )
            updated += 1
    return updated


if __name__ == "__main__":
    from db.db import init_db
    init_db()
    season, week = int(sys.argv[1]), int(sys.argv[2])
    print(f"Updated officiating for {refresh_week(season, week)} games")
