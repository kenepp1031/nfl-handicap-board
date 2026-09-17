"""Team power score (framework §3.4): weighted combination of position-group
scores into offense/defense/overall, using a recency-weighted rolling window
(last 4 games x2 vs. games 5-8 back x1) instead of a flat season average.

Early season (weeks 1-6) blends in the team's own prior-season exit score so
projections aren't starting from zero signal -- "exit score" here is just that
team's final-available overall_score from the prior season. This is a
team-level backstop on top of the player-level carryover already done in
grading.player_grades.blend_prior_season -- it still matters for any team
whose roster continuity the player-level blend can't fully cover (new
coordinator/scheme, a wave of new signings with no prior_avg of their own,
etc.), so both layers run.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from db.db import connect
from grading.player_grades import _scale, _zscore
from grading.team_units import load_team_week_epa

OFFENSE_WEIGHTS = {"QB": 0.45, "RB": 0.20, "WR": 0.20, "TE": 0.15}
OL_WEIGHT_INTO_OFFENSE = 0.25  # OL blended in as a modifier on the skill-position composite
RECENT_GAMES = 4
OLDER_GAMES = 4
RECENT_WEIGHT = 2.0
OLDER_WEIGHT = 1.0
EARLY_SEASON_WEEKS = 6
PRIOR_SEASON_BLEND = 0.5  # how much of week<=6's score comes from prior-season exit, tapering across the window
MIN_TEAM_SAMPLE = 16  # min teams with a valid unit EPA in a week before z-scoring it (fallback path)


def _weighted_avg(items: list[tuple[float, float]]) -> float | None:
    """items: [(value, weight), ...]"""
    total_w = sum(w for _v, w in items)
    if total_w <= 0:
        return None
    return sum(v * w for v, w in items) / total_w


def _rolling(values_by_week: dict[int, float], upto_week: int) -> tuple[float | None, int]:
    weeks = sorted(w for w in values_by_week if w <= upto_week)
    if not weeks:
        return None, 0
    weeks = weeks[::-1]  # most recent first
    recent = weeks[:RECENT_GAMES]
    older = weeks[RECENT_GAMES:RECENT_GAMES + OLDER_GAMES]
    items = ([(values_by_week[w], RECENT_WEIGHT) for w in recent]
             + [(values_by_week[w], OLDER_WEIGHT) for w in older])
    return _weighted_avg(items), len(recent) + len(older)


def _offense_composite(group_scores: dict[str, float]) -> float | None:
    skill_items = [(group_scores[g], w) for g, w in OFFENSE_WEIGHTS.items() if g in group_scores]
    if not skill_items:
        return None
    skill = _weighted_avg(skill_items)
    if "OL" in group_scores:
        return skill * (1 - OL_WEIGHT_INTO_OFFENSE) + group_scores["OL"] * OL_WEIGHT_INTO_OFFENSE
    return skill


def _save_team_scores(con, season: int, team: str, weeks, offense_by_week: dict[int, float],
                      defense_by_week: dict[int, float], prior: float | None) -> int:
    """Rolling window + early-season prior blend + upsert for one team. Shared
    by the player-driven and team-unit-fallback paths so they can't drift."""
    saved = 0
    for week in sorted(weeks):
        off_score, off_n = _rolling(offense_by_week, week)
        def_score, def_n = _rolling(defense_by_week, week)
        if week <= EARLY_SEASON_WEEKS and prior is not None:
            blend = PRIOR_SEASON_BLEND * (EARLY_SEASON_WEEKS - week + 1) / EARLY_SEASON_WEEKS
            if off_score is not None:
                off_score = off_score * (1 - blend) + prior * blend
            if def_score is not None:
                def_score = def_score * (1 - blend) + prior * blend
        if off_score is None and def_score is None:
            continue
        overall = _weighted_avg([(v, 1.0) for v in (off_score, def_score) if v is not None])
        con.execute(
            """INSERT INTO team_scores(team_abbr, season, week, offense_score, defense_score,
                   st_score, overall_score, rolling_window_games)
               VALUES(?,?,?,?,?,NULL,?,?)
               ON CONFLICT(team_abbr, season, week) DO UPDATE SET
                   offense_score=excluded.offense_score, defense_score=excluded.defense_score,
                   overall_score=excluded.overall_score,
                   rolling_window_games=excluded.rolling_window_games""",
            (team, season, week, off_score, def_score, overall, max(off_n, def_n)),
        )
        saved += 1
    return saved


def compute_season(season: int, prior_season_scores: dict[str, float] | None = None) -> int:
    with connect() as con:
        rows = con.execute(
            "SELECT team_abbr, week, position_group, score FROM position_group_scores WHERE season=?",
            (season,),
        ).fetchall()

    per_team: dict[str, dict[int, dict[str, float]]] = {}
    for r in rows:
        per_team.setdefault(r["team_abbr"], {}).setdefault(r["week"], {})[r["position_group"]] = r["score"]

    prior_season_scores = prior_season_scores or {}
    saved = 0
    with connect() as con:
        for team, weeks_data in per_team.items():
            offense_by_week = {}
            defense_by_week = {}
            for week, groups in weeks_data.items():
                off = _offense_composite(groups)
                if off is not None:
                    offense_by_week[week] = off
                if "DEF" in groups:
                    defense_by_week[week] = groups["DEF"]
            saved += _save_team_scores(con, season, team, weeks_data.keys(), offense_by_week,
                                       defense_by_week, prior_season_scores.get(team))
    return saved


def compute_from_team_units_season(season: int, prior_season_scores: dict[str, float] | None = None) -> int:
    """Fallback path for a season nflverse hasn't published player_stats/snap_counts
    for yet (confirmed true for 2025/2026 as of this build -- team-level weekly EPA
    keeps publishing live even when the player-level releases lag behind). Grades
    team offense/defense directly off team_units EPA/play, z-scored across the
    league each week, same 0-100 scale as the player-driven path so the two stay
    comparable game to game."""
    team_epa = load_team_week_epa(season)
    off_pool: dict[int, list[float]] = {}
    def_pool: dict[int, list[float]] = {}
    for (_team, week), v in team_epa.items():
        if v["off_epa_play"] is not None:
            off_pool.setdefault(week, []).append(v["off_epa_play"])
        if v["def_epa_play"] is not None:
            def_pool.setdefault(week, []).append(v["def_epa_play"])

    per_team_week: dict[str, dict[int, tuple[float | None, float | None]]] = {}
    for (team, week), v in team_epa.items():
        off_z = _zscore(v["off_epa_play"], off_pool.get(week, []), MIN_TEAM_SAMPLE)
        def_z = _zscore(v["def_epa_play"], def_pool.get(week, []), MIN_TEAM_SAMPLE)
        off_grade = _scale(off_z) if off_z is not None else None
        def_grade = _scale(def_z) if def_z is not None else None
        per_team_week.setdefault(team, {})[week] = (off_grade, def_grade)

    prior_season_scores = prior_season_scores or {}
    saved = 0
    with connect() as con:
        for team, weeks_data in per_team_week.items():
            offense_by_week = {w: og for w, (og, _dg) in weeks_data.items() if og is not None}
            defense_by_week = {w: dg for w, (_og, dg) in weeks_data.items() if dg is not None}
            saved += _save_team_scores(con, season, team, weeks_data.keys(), offense_by_week,
                                       defense_by_week, prior_season_scores.get(team))
    return saved


def prior_exit_scores(season: int) -> dict[str, float]:
    """Each team's overall_score from its last available week of `season`."""
    with connect() as con:
        rows = con.execute(
            """SELECT team_abbr, overall_score FROM team_scores t
               WHERE season=? AND week = (SELECT MAX(week) FROM team_scores t2
                                           WHERE t2.team_abbr=t.team_abbr AND t2.season=?)""",
            (season, season),
        ).fetchall()
    return {r["team_abbr"]: r["overall_score"] for r in rows if r["overall_score"] is not None}


if __name__ == "__main__":
    years = [int(x) for x in (sys.argv[1:] or [2025])]
    prior = {}
    for y in years:
        n = compute_season(y, prior_season_scores=prior)
        print(f"{y}: {n} team-week scores")
        prior = prior_exit_scores(y)
