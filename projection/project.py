"""Combine base + adjustments -> final spread/total/win-prob/confidence
(framework §3.8), blended toward the market line (framework §6)."""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from db.db import connect
from projection import adjustments as adj
from projection.base_model import training_pairs, fit_coefficient, DEFAULT_COEFFICIENT
from projection.injury_adjust import team_injury_impact

MARKET_BLEND_WEIGHT = 0.30  # final = 70% model + 30% market, framework §6
# Week 1 has no "before this week" data within the season by definition, so it
# always falls back to last season's exit score. That's a full prior season of
# real games, not nothing -- give it partial certainty credit (out of the
# 16-game normalizer below) rather than the hard zero a literal game count
# would produce. Weeks 2+ use the season's own (increasingly blended, per
# grading.player_grades.blend_prior_season) rolling data and don't need this.
PRIOR_SEASON_FALLBACK_CREDIT = 6

BEST_BET_UPSERT = """INSERT INTO best_bets(game_id, season, week, bet_type, pick, confidence_score, reasoning_text)
                     VALUES(?,?,?,?,?,?,?)
                     ON CONFLICT(game_id, bet_type) DO UPDATE SET
                         pick=excluded.pick, confidence_score=excluded.confidence_score,
                         reasoning_text=excluded.reasoning_text"""


def _team_score_before(con, team: str, season: int, week: int):
    row = con.execute(
        """SELECT overall_score, rolling_window_games FROM team_scores
           WHERE team_abbr=? AND season=? AND week < ?
           ORDER BY week DESC LIMIT 1""",
        (team, season, week),
    ).fetchone()
    if row:
        return row["overall_score"], row["rolling_window_games"]
    prior = con.execute(
        """SELECT overall_score FROM team_scores t
           WHERE team_abbr=? AND season=? AND week = (
               SELECT MAX(week) FROM team_scores t2 WHERE t2.team_abbr=t.team_abbr AND t2.season=?)""",
        (team, season - 1, season - 1),
    ).fetchone()
    return (prior["overall_score"], PRIOR_SEASON_FALLBACK_CREDIT) if prior else (None, 0)


def _win_prob(spread_home: float) -> float:
    """spread_home: positive means home favored by that many points."""
    return 1.0 / (1.0 + 10 ** (-spread_home / 14.0))


def project_week(season: int, week: int, coefficient: float | None = None,
                  enabled: dict | None = None) -> int:
    enabled = {**adj.DEFAULT_ENABLED, **(enabled or {})}
    if coefficient is None:
        pairs = training_pairs([season - 1, season]) or training_pairs([season])
        coefficient = fit_coefficient(pairs) if pairs else DEFAULT_COEFFICIENT

    saved = 0
    with connect() as con:
        games = con.execute(
            "SELECT * FROM games WHERE season=? AND week=? AND game_type='REG'",
            (season, week),
        ).fetchall()
        for g in games:
            home_score, home_n = _team_score_before(con, g["home_abbr"], season, week)
            away_score, away_n = _team_score_before(con, g["away_abbr"], season, week)
            if home_score is None or away_score is None:
                continue
            base_diff = coefficient * (home_score - away_score)

            hfa = adj.hfa_adjust(g["home_abbr"]) if enabled["hfa"] else 0.0

            rest = adj.rest_adjust(g["rest_days_home"], g["rest_days_away"]) if enabled["rest"] else 0.0

            wrow = con.execute("SELECT * FROM weather WHERE game_id=?", (g["game_id"],)).fetchone()
            weather_spread, weather_total = (0.0, 0.0)
            if enabled["weather"] and wrow:
                weather_spread, weather_total = adj.weather_adjust(
                    wrow["wind_mph"], wrow["precip_type"], wrow["temp_f"])

            rivalry_spread, rivalry_total = (0.0, 0.0)
            if enabled["rivalry"]:
                rivalry_spread, rivalry_total = adj.rivalry_adjust(bool(g["is_divisional"]))

            orow = con.execute("SELECT * FROM officiating WHERE game_id=?", (g["game_id"],)).fetchone()
            ref = 0.0
            if enabled["ref"] and orow:
                ref = adj.ref_adjust(orow["crew_home_ats_pct"], orow["crew_games"])

            # Injuries (projection/injury_adjust.py's fixed scale, already in spread
            # points) only get computed for games not yet played: the injuries table
            # is a live snapshot, so a finished/backfilled game keeps whatever was
            # stored when it was projected before kickoff (or 0 if never).
            injury = 0.0
            if g["home_score"] is not None:
                prev = con.execute("SELECT injury_adj FROM projections WHERE game_id=?", (g["game_id"],)).fetchone()
                injury = prev["injury_adj"] if prev and prev["injury_adj"] is not None else 0.0
            elif enabled["injury"]:
                home_points = team_injury_impact(con, g["home_abbr"], season, week)["points"]
                away_points = team_injury_impact(con, g["away_abbr"], season, week)["points"]
                injury = round(away_points - home_points, 2)  # + favors home

            pre_shrink = base_diff + hfa + rest + weather_spread + rivalry_spread + ref + injury
            if enabled["rivalry"]:
                pre_shrink = adj.rivalry_compress_spread(pre_shrink, bool(g["is_divisional"]))

            # nflverse's spread_line (stored as closing_spread) uses the same convention
            # as pre_shrink here: positive = home favored by that many points. Confirmed
            # against real results (e.g. 2024 Wk1 KC -3 home favorite, spread_line=3).
            market_spread = g["closing_spread"]
            if market_spread is not None:
                final_spread = pre_shrink * (1 - MARKET_BLEND_WEIGHT) + market_spread * MARKET_BLEND_WEIGHT
            else:
                final_spread = pre_shrink

            base_total = 44.0  # league-average total as a neutral anchor; refined by adjustments only
            final_total_adj = weather_total + rivalry_total
            market_total = g["closing_total"]
            model_total = base_total + final_total_adj
            final_total = (model_total * (1 - MARKET_BLEND_WEIGHT) + market_total * MARKET_BLEND_WEIGHT
                           if market_total is not None else model_total)

            win_prob = _win_prob(final_spread)

            certainty = min(1.0, ((home_n or 0) + (away_n or 0)) / 16.0)
            n_adjustments_fired = sum(1 for v in (hfa, rest, weather_spread, rivalry_spread, ref, injury) if abs(v) > 0.01)
            stack_penalty = max(0.0, 1.0 - 0.08 * n_adjustments_fired)

            # Confidence is driven by how much we DISAGREE with the market (the edge),
            # not by how lopsided the pick itself looks -- a 10-point favorite the market
            # also has at 10 is not an actionable edge, a 1-point disagreement backed by
            # a full sample is more interesting than a big number built on no games.
            spread_edge = (final_spread - market_spread) if market_spread is not None else None
            spread_confidence = (
                round(100 * certainty * stack_penalty * min(1.0, abs(spread_edge) / 4.0 + 0.2), 1)
                if spread_edge is not None else None
            )
            total_edge = (final_total - market_total) if market_total is not None else None
            total_stack_penalty = max(0.0, 1.0 - 0.08 * sum(1 for v in (weather_total, rivalry_total) if abs(v) > 0.01))
            total_confidence = (
                round(100 * certainty * total_stack_penalty * min(1.0, abs(total_edge) / 4.0 + 0.2), 1)
                if total_edge is not None else None
            )
            confidence = spread_confidence  # stored on `projections` as the headline number

            con.execute(
                """INSERT INTO projections(game_id, base_score_diff, hfa_adj, weather_adj, rest_adj,
                       rivalry_adj, ref_adj, injury_adj, pre_shrink_spread, final_spread, final_total,
                       home_win_prob, confidence_score, generated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(game_id) DO UPDATE SET
                       base_score_diff=excluded.base_score_diff, hfa_adj=excluded.hfa_adj,
                       weather_adj=excluded.weather_adj, rest_adj=excluded.rest_adj,
                       rivalry_adj=excluded.rivalry_adj, ref_adj=excluded.ref_adj,
                       injury_adj=excluded.injury_adj,
                       pre_shrink_spread=excluded.pre_shrink_spread, final_spread=excluded.final_spread,
                       final_total=excluded.final_total, home_win_prob=excluded.home_win_prob,
                       confidence_score=excluded.confidence_score, generated_at=excluded.generated_at""",
                (g["game_id"], base_diff, hfa, weather_spread, rest, rivalry_spread, ref, injury,
                 pre_shrink, final_spread, final_total, win_prob, confidence,
                 datetime.now(timezone.utc).isoformat(timespec="minutes")),
            )

            # A pick that no longer has an edge (market moved onto our number, or the
            # line just arrived) must clear its old best_bets row, not leave it up.
            if spread_edge is not None and abs(spread_edge) >= 0.05:
                pick = g["home_abbr"] if spread_edge > 0 else g["away_abbr"]
                reasoning = (f"Model favors {'home' if spread_edge > 0 else 'away'} "
                             f"by {abs(spread_edge):.1f} pts vs. the market line.")
                con.execute(BEST_BET_UPSERT, (g["game_id"], season, week, "spread", pick, spread_confidence, reasoning))
            else:
                con.execute("DELETE FROM best_bets WHERE game_id=? AND bet_type='spread'", (g["game_id"],))

            if total_edge is not None and abs(total_edge) >= 0.05:
                pick = "OVER" if total_edge > 0 else "UNDER"
                reasoning = (f"Model total is {abs(total_edge):.1f} pts "
                             f"{'above' if total_edge > 0 else 'below'} the market total.")
                con.execute(BEST_BET_UPSERT, (g["game_id"], season, week, "total", pick, total_confidence, reasoning))
            else:
                con.execute("DELETE FROM best_bets WHERE game_id=? AND bet_type='total'", (g["game_id"],))
            saved += 1
    return saved


if __name__ == "__main__":
    season = int(sys.argv[1]) if len(sys.argv) > 1 else 2025
    week = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    n = project_week(season, week)
    print(f"Projected {n} games for {season} week {week}")
