"""Player grading (framework §3.2).

QB/RB/WR/TE get a real individual 0-100 grade from EPA-based fields in
player_stats, z-scored against every other player at that position in that
same week, whenever they clear a minimum-plays threshold. Everyone else for
that week -- OL and DEF always (no per-player EPA data exists for those
positions in nflverse's public releases), plus any skill player who didn't
clear the plays threshold -- gets a team-unit proxy grade: they inherit their
team's offense (QB/RB/WR/TE/OL) or defense (DEF) EPA/play grade for the week.
Snap-share weighting (position_groups.py) still does real work even under the
proxy -- who's credited for the group score shifts with playing time even
though the per-player number itself is shared.

blend_prior_season() then carries a RETURNING player's own grade forward from
last season into the first few weeks of a new one (framework §3.4's "blend in
last season's exit-value score" idea, applied at the player level instead of
just team level). This is keyed by player_id, not by team, so a trade or a
free-agent move doesn't break the carryover -- the player's own past
performance follows them, credited to whichever team snap_counts / player_stats
says they're playing for this season.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from common import POSITION_GROUP
from db.db import connect
from grading.team_units import load_team_week_epa

MIN_PLAYS = {"QB": 10, "RB": 5, "WR": 3, "TE": 3}
MIN_LEAGUE_SAMPLE = 6   # min players at a position in a week before z-scoring it
MIN_TEAM_SAMPLE = 16    # min teams with a valid unit EPA in a week before z-scoring it

# Defensive box-count formula (ingest/nflverse_pbp.py's def_stats): no public
# EPA attribution exists for individual defenders, so this weights the counting
# stats pbp actually gives us -- big plays (sacks, INTs, forced fumbles) count
# for more than incremental ones (assisted tackles), and everything is compared
# only within its own sub-group since a CB's box score looks nothing like a
# LB's or DL's for reasons that have nothing to do with play quality.
DEF_STAT_WEIGHTS = {
    "sacks": 3.0, "tfl": 1.5, "qb_hits": 1.0, "ints": 4.0,
    "pass_defensed": 1.5, "forced_fumbles": 2.0, "solo_tackles": 0.75, "assist_tackles": 0.4,
}
DEF_SUBGROUP = {
    "DE": "DL", "EDGE": "DL", "DT": "DL", "NT": "DL", "DL": "DL",
    "LB": "LB", "ILB": "LB", "OLB": "LB", "MLB": "LB",
    "CB": "CB", "DB": "CB",
    "S": "S", "SS": "S", "FS": "S", "SAF": "S",
}
MIN_DEF_SNAP_PCT = 0.30  # ignore token-snap defensive appearances

EARLY_SEASON_WEEKS = 6       # how many weeks of the new season carry a prior-season blend
PRIOR_WEIGHT_WEEK1 = 0.55    # blend weight given to last season's grade at week 1
PRIOR_WEIGHT_LAST = 0.10     # blend weight remaining by the last blended week


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _epa_per_play(position_group: str, stat: dict):
    if position_group == "QB":
        epa = _num(stat.get("passing_epa")) + _num(stat.get("rushing_epa"))
        plays = _num(stat.get("attempts")) + _num(stat.get("sacks")) + _num(stat.get("carries"))
    elif position_group == "RB":
        epa = _num(stat.get("rushing_epa")) + _num(stat.get("receiving_epa"))
        plays = _num(stat.get("carries")) + _num(stat.get("targets"))
    else:  # WR, TE
        epa = _num(stat.get("receiving_epa"))
        plays = _num(stat.get("targets"))
    if plays <= 0 or not math.isfinite(epa):
        return None
    return epa / plays, plays


def _scale(z: float) -> float:
    return max(1.0, min(99.0, 50.0 + z * 15.0))


def _zscore(value, pool, min_sample):
    if value is None or len(pool) < min_sample:
        return None
    avg = sum(pool) / len(pool)
    sd = (sum((x - avg) ** 2 for x in pool) / len(pool)) ** 0.5
    return (value - avg) / sd if sd else 0.0


def _upsert_grade(con, player_id: str, season: int, week: int, grade: float, source: str, components: dict) -> None:
    con.execute(
        """INSERT INTO player_grades(player_id, season, week, grade, source, grade_components_json)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(player_id, season, week) DO UPDATE SET
               grade=excluded.grade, source=excluded.source,
               grade_components_json=excluded.grade_components_json""",
        (player_id, season, week, grade, source, json.dumps(components)),
    )


def grade_season(season: int) -> int:
    graded = 0
    already_graded: set[tuple[str, int]] = set()  # (player_id, week) that got an individual grade
    with connect() as con:
        rows = con.execute(
            "SELECT pgs.player_id, pgs.week, pgs.team_abbr, pgs.stat_json, pgs.defense_pct, p.position "
            "FROM player_game_stats pgs JOIN players p ON p.player_id = pgs.player_id "
            "WHERE pgs.season = ?", (season,)
        ).fetchall()

    # -- QB/RB/WR/TE: individual grade, z-scored within (week, position) --
    parsed = []
    by_week_pos: dict[tuple[int, str], list[float]] = {}
    for r in rows:
        pos_group = POSITION_GROUP.get(r["position"])
        if pos_group not in ("QB", "RB", "WR", "TE") or not r["stat_json"]:
            continue
        try:
            stat = json.loads(r["stat_json"])
        except (TypeError, ValueError):
            continue
        result = _epa_per_play(pos_group, stat)
        if result is None:
            continue
        epa_play, plays = result
        if plays < MIN_PLAYS.get(pos_group, 3):
            continue
        parsed.append((r["player_id"], r["week"], pos_group, epa_play, plays))
        by_week_pos.setdefault((r["week"], pos_group), []).append(epa_play)

    with connect() as con:
        for player_id, week, pos_group, epa_play, plays in parsed:
            pool = by_week_pos.get((week, pos_group), [])
            z = _zscore(epa_play, pool, MIN_LEAGUE_SAMPLE)
            if z is None:
                continue
            _upsert_grade(con, player_id, season, week, _scale(z), "individual", {
                "epa_per_play": round(epa_play, 4), "plays": plays, "z": round(z, 3),
            })
            already_graded.add((player_id, week))
            graded += 1

    team_epa = load_team_week_epa(season)
    off_pool: dict[int, list[float]] = {}
    def_pool: dict[int, list[float]] = {}
    for (_team, week), v in team_epa.items():
        if v["off_epa_play"] is not None:
            off_pool.setdefault(week, []).append(v["off_epa_play"])
        if v["def_epa_play"] is not None:
            def_pool.setdefault(week, []).append(v["def_epa_play"])

    # -- DEF: individual grade from box-count stats (stat_json's def_stats),
    # per-snap-normalized and z-scored within (week, DL/LB/CB/S sub-group).
    # These now arrive for every season from nflverse's stats_player_week
    # release, which carries defensive counts in the same rows as offense.
    # Before that release was wired up they only existed for seasons that fell
    # back to play-by-play, so 2015-2024 defenders got the team-unit proxy and
    # 2025-2026 defenders got this -- the same player was graded two different
    # ways depending on which side of the rename his season fell on.
    def_parsed = []
    def_by_week_subgroup: dict[tuple[int, str], list[float]] = {}
    for r in rows:
        subgroup = DEF_SUBGROUP.get(r["position"])
        if not subgroup or not r["stat_json"] or not r["defense_pct"] or r["defense_pct"] < MIN_DEF_SNAP_PCT:
            continue
        try:
            stat = json.loads(r["stat_json"])
        except (TypeError, ValueError):
            continue
        def_stat = stat.get("def_stats")
        if not def_stat:
            continue
        team_plays = team_epa.get((r["team_abbr"], r["week"]), {}).get("def_plays")
        if not team_plays:
            continue
        snaps = r["defense_pct"] * team_plays
        if snaps <= 0:
            continue
        raw = sum(DEF_STAT_WEIGHTS.get(k, 0.0) * _num(v) for k, v in def_stat.items())
        per_snap = raw / snaps
        def_parsed.append((r["player_id"], r["week"], subgroup, per_snap, raw, snaps))
        def_by_week_subgroup.setdefault((r["week"], subgroup), []).append(per_snap)

    with connect() as con:
        for player_id, week, subgroup, per_snap, raw, snaps in def_parsed:
            pool = def_by_week_subgroup.get((week, subgroup), [])
            z = _zscore(per_snap, pool, MIN_LEAGUE_SAMPLE)
            if z is None:
                continue
            _upsert_grade(con, player_id, season, week, _scale(z), "individual_def", {
                "def_weighted_per_snap": round(per_snap, 4), "def_weighted_raw": round(raw, 2),
                "snaps_est": round(snaps, 1), "subgroup": subgroup, "z": round(z, 3),
            })
            already_graded.add((player_id, week))
            graded += 1

    # -- team-unit proxy grade for everyone who still didn't get an individual
    # grade above: OL always (no per-player data exists for it at all), DEF in
    # seasons without pbp-derived box stats, plus any QB/RB/WR/TE that didn't
    # clear MIN_PLAYS that week or fell below the league-sample threshold --
    # better to give them their team's number than no grade at all, since a
    # no-grade player silently drops out of the snap-weighted roll-up instead
    # of being credited/debited for form.
    with connect() as con:
        for r in rows:
            pos_group = POSITION_GROUP.get(r["position"])
            if not pos_group or pos_group == "ST" or not r["team_abbr"]:
                continue
            if (r["player_id"], r["week"]) in already_graded:
                continue
            week, team = r["week"], r["team_abbr"]
            v = team_epa.get((team, week))
            if not v:
                continue
            is_offense_side = pos_group in ("QB", "RB", "WR", "TE", "OL")
            if is_offense_side:
                z = _zscore(v["off_epa_play"], off_pool.get(week, []), MIN_TEAM_SAMPLE)
            else:
                z = _zscore(v["def_epa_play"], def_pool.get(week, []), MIN_TEAM_SAMPLE)
            if z is None:
                continue
            _upsert_grade(con, r["player_id"], season, week, _scale(z), "team_unit_proxy", {
                "team_off_epa_play": v["off_epa_play"], "team_def_epa_play": v["def_epa_play"],
            })
            graded += 1

    return graded


def blend_prior_season(season: int) -> int:
    """Carries each returning player's own average grade from `season - 1`
    into their first EARLY_SEASON_WEEKS grades of `season`, tapering the
    weight down as real current-season signal accumulates. Player-keyed, so a
    trade/free-agent signing carries the grade to the player's new team
    automatically -- whatever team player_game_stats says they're on this
    season is who gets credited. A player with no current-season grade yet
    (e.g. still ungraded this week) gets one inserted from pure prior-season
    average, so they aren't invisible to the roll-up during the blend window.
    """
    with connect() as con:
        prior_avg = {
            r["player_id"]: r["avg_grade"]
            for r in con.execute(
                "SELECT player_id, AVG(grade) avg_grade FROM player_grades WHERE season=? GROUP BY player_id",
                (season - 1,),
            ).fetchall()
        }
        if not prior_avg:
            return 0
        current = {
            (r["player_id"], r["week"]): r["grade"]
            for r in con.execute(
                "SELECT player_id, week, grade FROM player_grades WHERE season=? AND week<=?",
                (season, EARLY_SEASON_WEEKS),
            ).fetchall()
        }
        roster_weeks = con.execute(
            "SELECT DISTINCT player_id, week FROM player_game_stats WHERE season=? AND week<=?",
            (season, EARLY_SEASON_WEEKS),
        ).fetchall()

    def weight_for(week: int) -> float:
        if EARLY_SEASON_WEEKS <= 1:
            return PRIOR_WEIGHT_WEEK1
        t = (week - 1) / (EARLY_SEASON_WEEKS - 1)
        return PRIOR_WEIGHT_WEEK1 + (PRIOR_WEIGHT_LAST - PRIOR_WEIGHT_WEEK1) * t

    updated = 0
    with connect() as con:
        for r in roster_weeks:
            player_id, week = r["player_id"], r["week"]
            prior = prior_avg.get(player_id)
            if prior is None:
                continue
            w = weight_for(week)
            existing = current.get((player_id, week))
            blended = existing * (1 - w) + prior * w if existing is not None else prior
            _upsert_grade(con, player_id, season, week, blended, "prior_season_blend", {
                "prior_season_avg": prior, "current_season_grade": existing, "blend_weight": round(w, 3),
            })
            updated += 1
    return updated


if __name__ == "__main__":
    from db.db import init_db
    init_db()
    years = [int(x) for x in (sys.argv[1:] or [2025])]
    for y in years:
        n = grade_season(y)
        n_blend = blend_prior_season(y)
        print(f"{y}: graded {n} player-game rows, blended {n_blend} early-season rows with {y-1}")
