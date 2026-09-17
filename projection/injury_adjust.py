"""Injury adjustment: our own fixed scale, in spread points, applied straight
to the number and itemized per player in the Game Report.

Each listed player is worth INJURY_SCALE[position] points if he's a full-time
starter ruled Out, times STATUS_WEIGHT for his status, times his share of his
unit's snaps (max over his team's last few games, so a starter who sat one
week still counts as a starter and a 30% rotational player counts 30%). A
team's total is capped at MAX_TEAM_POINTS. project.py adds
(away_points - home_points) to the spread before the market blend, so the
points shown next to a player in the report are exactly what he cost his
team's side of the number.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from common import POSITION_GROUP
from grading.position_groups import _unit_share

# Spread points for a full-time starter ruled Out, by nflverse position code.
INJURY_SCALE = {
    "QB": 5.0,
    "RB": 1.0, "FB": 0.25, "WR": 1.0, "TE": 0.75,
    "T": 1.0, "OT": 1.0, "G": 0.5, "OG": 0.5, "C": 0.5, "OL": 0.5,
    "DE": 1.0, "EDGE": 1.0, "OLB": 0.75, "DT": 0.5, "NT": 0.5, "DL": 0.5,
    "LB": 0.5, "ILB": 0.5, "MLB": 0.5,
    "CB": 1.0, "DB": 0.5, "S": 0.5, "SS": 0.5, "FS": 0.5, "SAF": 0.5,
}
# Fraction of the Out value each status is worth.
STATUS_WEIGHT = {
    "out": 1.0, "injured reserve": 1.0, "ir": 1.0, "pup": 1.0,
    "suspended": 1.0, "suspension": 1.0,
    "doubtful": 0.9,
    "questionable": 0.4,
}
ROSTER_LOOKBACK_WEEKS = 3   # how many recent games define a player's snap share
MAX_TEAM_POINTS = 7.0       # cap on one team's total injury hit
MIN_POINTS_TO_NOTE = 0.1    # players below this still count, but aren't named

_SUFFIX_RE = re.compile(r"\s+(jr|sr|ii|iii|iv)\.?$")


def _norm(name: str) -> str:
    """ESPN and nflverse spell most names identically, but not punctuation
    or suffixes -- 'Michael Penix Jr.' vs 'Michael Penix'."""
    n = name.lower().replace(".", "").replace("'", "").strip()
    return _SUFFIX_RE.sub("", n)


def _roster_weeks(con, team: str, season: int, week: int) -> tuple[int, list[int]]:
    """(season, [weeks]) of the team's most recent games on file: the last few
    of this season before `week`, else last season's finale (Week 1)."""
    weeks = [r["week"] for r in con.execute(
        "SELECT DISTINCT week FROM player_game_stats WHERE team_abbr=? AND season=? AND week<? "
        "ORDER BY week DESC LIMIT ?", (team, season, week, ROSTER_LOOKBACK_WEEKS))]
    if weeks:
        return season, weeks
    row = con.execute(
        "SELECT MAX(week) w FROM player_game_stats WHERE team_abbr=? AND season=?", (team, season - 1)
    ).fetchone()
    return season - 1, ([row["w"]] if row and row["w"] is not None else [])


def team_injury_impact(con, team: str, season: int, week: int) -> dict:
    """Returns {"points": total spread points this team's injuries cost it,
    "players": [{name, position, status, share, points}, ...] sorted by points}."""
    src_season, weeks = _roster_weeks(con, team, season, week)
    if not weeks:
        return {"points": 0.0, "players": []}
    listed = {
        _norm(r["player_name"]): r["status"]
        for r in con.execute("SELECT player_name, status FROM injuries WHERE team_abbr=?", (team,))
    }
    if not listed:
        return {"points": 0.0, "players": []}

    rows = con.execute(
        f"""SELECT p.name, p.position, pgs.snap_pct, pgs.offense_pct, pgs.defense_pct, pgs.st_pct
            FROM player_game_stats pgs JOIN players p ON p.player_id = pgs.player_id
            WHERE pgs.team_abbr=? AND pgs.season=? AND pgs.week IN ({",".join("?" * len(weeks))})""",
        (team, src_season, *weeks),
    ).fetchall()
    share_by_player: dict[str, tuple[str, str, float]] = {}  # norm name -> (name, position, max share)
    for r in rows:
        pos_group = POSITION_GROUP.get(r["position"])
        if not pos_group or pos_group == "ST":
            continue
        share = _unit_share(r, pos_group) or 0.0
        key = _norm(r["name"])
        if key not in share_by_player or share > share_by_player[key][2]:
            share_by_player[key] = (r["name"], r["position"], share)

    players = []
    for key, status in listed.items():
        entry = share_by_player.get(key)
        if not entry:
            continue
        name, position, share = entry
        base = INJURY_SCALE.get(position)
        weight = STATUS_WEIGHT.get((status or "").lower())
        if base is None or weight is None or share <= 0:
            continue
        points = round(base * weight * share, 2)
        if points < 0.05:
            continue
        players.append({"name": name, "position": position, "status": status,
                        "share": share, "points": points})
    players.sort(key=lambda p: -p["points"])
    total = min(MAX_TEAM_POINTS, round(sum(p["points"] for p in players), 2))
    return {"points": total, "players": players}


if __name__ == "__main__":
    from db.db import connect
    season, week = int(sys.argv[1]), int(sys.argv[2])
    with connect() as con:
        for team in [r["team_id"] for r in con.execute("SELECT team_id FROM teams ORDER BY team_id")]:
            impact = team_injury_impact(con, team, season, week)
            if impact["players"]:
                names = ", ".join(f"{p['position']} {p['name']} ({p['status']}) -{p['points']:.1f}"
                                  for p in impact["players"])
                print(f"{team}: -{impact['points']:.1f} pts -- {names}")
