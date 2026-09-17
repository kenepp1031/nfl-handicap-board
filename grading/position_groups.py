"""Snap-share weighted position-group roll-up (framework §3.3), with a
position-value multiplier for injury-severity awareness (framework §6):

    group_score = Σ(grade_i × snap_pct_i × value_i) / Σ(snap_pct_i × value_i)

A starter's snap_pct dropping to 0 and a backup's rising shifts the group
score on its own -- and a collapse at a premium position (QB, OT, CB1, WR1,
EDGE1) moves the score more than the same collapse at a depth spot.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from common import POSITION_GROUP, POSITION_VALUE, DEFAULT_POSITION_VALUE
from db.db import connect

OFFENSE_GROUPS = {"QB", "RB", "WR", "TE", "OL"}


def _unit_share(row, pos_group: str):
    """Share of the player's OWN unit's snaps (offense for QB/RB/WR/TE/OL,
    defense for DEF, special teams for ST). player_game_stats.snap_pct is the
    max across all three units, which would let a WR4 who plays 80% of
    special-teams snaps and 5% of offensive ones count as an 80% WR."""
    if pos_group in OFFENSE_GROUPS:
        share = row["offense_pct"]
    elif pos_group == "DEF":
        share = row["defense_pct"]
    else:
        share = row["st_pct"]
    return row["snap_pct"] if share is None else share


def roll_up_season(season: int) -> int:
    with connect() as con:
        rows = con.execute(
            """SELECT pgs.team_abbr, pgs.week, pgs.snap_pct, pgs.offense_pct, pgs.defense_pct,
                      pgs.st_pct, pg.grade, p.position
               FROM player_game_stats pgs
               JOIN players p ON p.player_id = pgs.player_id
               JOIN player_grades pg ON pg.player_id = pgs.player_id
                   AND pg.season = pgs.season AND pg.week = pgs.week
               WHERE pgs.season = ? AND pgs.team_abbr IS NOT NULL""",
            (season,),
        ).fetchall()

    buckets: dict[tuple[str, int, str], list[tuple[float, float, float]]] = {}
    for r in rows:
        pos_group = POSITION_GROUP.get(r["position"])
        if not pos_group or r["grade"] is None:
            continue
        share = _unit_share(r, pos_group)
        if not share or share <= 0:
            continue
        value = POSITION_VALUE.get(r["position"], DEFAULT_POSITION_VALUE)
        key = (r["team_abbr"], r["week"], pos_group)
        buckets.setdefault(key, []).append((r["grade"], share, value))

    saved = 0
    with connect() as con:
        for (team, week, pos_group), entries in buckets.items():
            weight_sum = sum(snap * value for _grade, snap, value in entries)
            if weight_sum <= 0:
                continue
            score = sum(grade * snap * value for grade, snap, value in entries) / weight_sum
            con.execute(
                """INSERT INTO position_group_scores(team_abbr, season, week, position_group, score, snap_weighted_n)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(team_abbr, season, week, position_group) DO UPDATE SET
                       score=excluded.score, snap_weighted_n=excluded.snap_weighted_n""",
                (team, season, week, pos_group, score, weight_sum),
            )
            saved += 1
    return saved


if __name__ == "__main__":
    years = [int(x) for x in (sys.argv[1:] or [2025])]
    for y in years:
        n = roll_up_season(y)
        print(f"{y}: {n} position-group-week rows")
