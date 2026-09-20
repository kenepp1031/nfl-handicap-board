"""Combine base + adjustments -> final spread/total/win-prob/confidence
(framework §3.8), blended toward the market line (framework §6)."""
from __future__ import annotations

import json
import math
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from db.db import connect
from projection import adjustments as adj
from projection.base_model import training_pairs, fit_coefficient, DEFAULT_COEFFICIENT
from projection.injury_adjust import team_injury_impact
from projection import confidence as conf
from projection import score_model

# final = (1 - w) * model + w * market. Was 0.30 (70% model). Measured over the
# 2,654 graded games in this DB, every point of model weight makes the printed
# number worse -- spread MAE runs 9.80 at pure market, 9.84 at 20% model, 10.13
# at the old 70%, rising monotonically -- because the model's incremental
# coefficient against the closing line is -0.06. The model number is still worth
# showing (dashboard prints it beside the line as "our own read"), but it should
# not be most of the headline spread. ATS is flat at ~49% for every setting, so
# this is an accuracy change, not an edge claim.
MARKET_BLEND_WEIGHT = 0.80  # final = 20% model + 80% market, framework §6

# A spread pick used to fire at |edge| >= 0.05, which put a pick on essentially
# every game on the board -- 16 picks a week, none of them meaning anything. The
# model's own sd is 4.82 against the market's 6.12, so sub-point disagreements
# are shrinkage artifacts, not opinions. Require a real gap, and require the
# game's inputs to be worth trusting before it earns a slot.
# Thresholds are on the UN-BLENDED lean (our number vs the line), whose sd is 3.91
# over the 2,654 games in this DB.
#
# This is a BAND, not a floor, and the ceiling is the important half. A plain floor
# is the intuitive gate -- bigger disagreement, better bet -- and it is measurably
# backwards here. Graded across all seasons:
#
#     everything (the old 0.05 gate)   49.3%   n=2654   ~13.7 picks/wk
#     |lean| >= 4                      48.2%   n=795    ~4.1 picks/wk
#     |lean| >= 4 and conf >= 50       47.2%   n=676    ~3.5 picks/wk
#     2 <= |lean| <= 6                 49.6%   n=1329   ~6.8 picks/wk
#
# The mechanism is the same one that sank edge-scaled confidence: a least-squares
# model is shrunk toward zero in proportion to its own weakness (our sd 4.82 vs the
# market's 6.12), so the largest leans are mostly shrinkage artifact rather than
# information. Raising a floor walks straight into that tail. The ceiling keeps it out.
#
# None of these rules clears the 52.4% break-even and they are all within ~2 standard
# errors of each other -- the band is chosen because it cuts volume as asked WITHOUT
# deliberately selecting the band the mechanism says is worst, not because it wins.
MIN_SPREAD_EDGE = 2.0
MAX_SPREAD_LEAN = 6.0
MIN_TOTAL_EDGE = 2.0
MAX_TOTAL_LEAN = 6.0
# There is deliberately no confidence threshold. Requiring conf >= 50 was measured
# to make the board worse, not safer: over 2,851 graded games our MAE is 10.42 in
# the lowest confidence bucket and 10.78 in the highest, ATS runs 52.5% at 40-50
# against 48.1% at 80+, and because the old score rose with the week of the season
# the threshold acted as "do not bet before week 7" -- it discarded 60% of
# early-season band games (131 of 330 survived) and the survivors graded 48.9%
# against 50.2% for the full band. Confidence describes inputs; it does not rank
# bets. See projection/confidence.py.
# Week 1 has no "before this week" data within the season by definition, so it
# always falls back to last season's exit score. That's a full prior season of
# real games, not nothing. Weeks 2+ use the season's own (increasingly blended,
# per grading.player_grades.blend_prior_season) rolling data and don't need this.
# This stays as the nominal game count on the RatingSource for that path; the
# certainty it actually earns is set by confidence.PRIOR_SEASON_EQUIV_GAMES,
# which also charges the row full staleness for being last year's roster.
PRIOR_SEASON_FALLBACK_CREDIT = 6

# Used only when a team has no rating to read at all, which in practice means an
# expansion/relocation abbr or a DB with no graded history. 45.7 is the mean real
# total over the 2,851 graded games in this DB -- the old 44.0 anchor was 1.6
# points low, which biased every projected score in the league downward.
FALLBACK_TOTAL = 45.7

BEST_BET_UPSERT = """INSERT INTO best_bets(game_id, season, week, bet_type, pick, confidence_score, reasoning_text)
                     VALUES(?,?,?,?,?,?,?)
                     ON CONFLICT(game_id, bet_type) DO UPDATE SET
                         pick=excluded.pick, confidence_score=excluded.confidence_score,
                         reasoning_text=excluded.reasoning_text"""


def _team_score_before(con, team: str, season: int, week: int):
    """Returns (overall_score, RatingSource). The source carries WHICH week's row
    was read and whether it is a prior-season carryover, so projection.confidence
    can price both the sample size behind the rating and how stale it is (a team
    coming off a bye is read from a two-week-old row)."""
    row = con.execute(
        """SELECT week, overall_score, rolling_window_games FROM team_scores
           WHERE team_abbr=? AND season=? AND week < ?
           ORDER BY week DESC LIMIT 1""",
        (team, season, week),
    ).fetchone()
    if row:
        return row["overall_score"], conf.RatingSource(row["rolling_window_games"], row["week"])
    prior = con.execute(
        """SELECT overall_score FROM team_scores t
           WHERE team_abbr=? AND season=? AND week = (
               SELECT MAX(week) FROM team_scores t2 WHERE t2.team_abbr=t.team_abbr AND t2.season=?)""",
        (team, season - 1, season - 1),
    ).fetchone()
    if prior:
        return prior["overall_score"], conf.RatingSource(PRIOR_SEASON_FALLBACK_CREDIT, None, True)
    return None, conf.RatingSource(0, None)


VOLATILITY_LOOKBACK_WEEKS = 8
MIN_WEEKS_FOR_VOLATILITY = 3


def _rating_volatility(con, team: str, season: int, week: int) -> float | None:
    """Sd of the team's weekly overall_score over its last few graded weeks, walking
    back into the prior season when the current one is too young to measure. Returns
    None below MIN_WEEKS_FOR_VOLATILITY so projection.confidence can fall back to the
    league median instead of reading a 2-point sample as gospel."""
    rows = con.execute(
        """SELECT overall_score FROM team_scores
           WHERE team_abbr=? AND overall_score IS NOT NULL
             AND (season < ? OR (season = ? AND week < ?))
           ORDER BY season DESC, week DESC LIMIT ?""",
        (team, season, season, week, VOLATILITY_LOOKBACK_WEEKS),
    ).fetchall()
    values = [r["overall_score"] for r in rows]
    if len(values) < MIN_WEEKS_FOR_VOLATILITY:
        return None
    return statistics.pstdev(values)


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
            home_score, home_src = _team_score_before(con, g["home_abbr"], season, week)
            away_score, away_src = _team_score_before(con, g["away_abbr"], season, week)
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
            # `injury` is the NET that moves the spread; `injury_gross` is both
            # teams' charges added, which is what projection.confidence needs --
            # netting hides a game where both rosters are gutted (they cancel).
            # Games already played keep whatever was stored pre-kickoff; gross is
            # NULL on rows projected before that column existed, and confidence
            # falls back to |net| there rather than claiming a clean game.
            injury = 0.0
            injury_gross = None
            if g["home_score"] is not None:
                prev = con.execute("SELECT injury_adj, injury_gross FROM projections WHERE game_id=?",
                                   (g["game_id"],)).fetchone()
                injury = prev["injury_adj"] if prev and prev["injury_adj"] is not None else 0.0
                injury_gross = prev["injury_gross"] if prev else None
            elif enabled["injury"]:
                home_points = team_injury_impact(con, g["home_abbr"], season, week)["points"]
                away_points = team_injury_impact(con, g["away_abbr"], season, week)["points"]
                injury = round(away_points - home_points, 2)  # + favors home
                injury_gross = round(home_points + away_points, 2)

            pre_shrink = base_diff + hfa + rest + weather_spread + rivalry_spread + ref + injury
            if enabled["rivalry"]:
                compressed = adj.rivalry_compress_spread(pre_shrink, bool(g["is_divisional"]))
                # The compression is the ONLY way rivalry reaches the spread --
                # adj.rivalry_adjust() returns 0.0 on its spread leg by design. Fold the
                # delta into rivalry_spread so `rivalry_adj` stores the real effect:
                # backtest/log_results.py reads that column to decide whether a factor
                # "fired", and it was reporting rivalry as never firing across 2,654
                # games while quietly shaving 10% off every divisional spread.
                rivalry_spread += compressed - pre_shrink
                pre_shrink = compressed

            # nflverse's spread_line (stored as closing_spread) uses the same convention
            # as pre_shrink here: positive = home favored by that many points. Confirmed
            # against real results (e.g. 2024 Wk1 KC -3 home favorite, spread_line=3).
            market_spread = g["closing_spread"]
            if market_spread is not None:
                final_spread = pre_shrink * (1 - MARKET_BLEND_WEIGHT) + market_spread * MARKET_BLEND_WEIGHT
            else:
                final_spread = pre_shrink

            # OUR OWN expected total for this matchup, from projection.score_model's
            # points regression (each team's offense rating against the other's
            # defense rating), then moved by the two factors that genuinely change
            # how much scoring a game supports.
            #
            # This replaces a flat 44.0 league-average anchor. That anchor gave the
            # stored total a standard deviation of 1.39 against the market's 4.41 --
            # on the 2026 week-2 board it printed 42, 43 or 44 on all sixteen games,
            # including 44.0 for DET@BUF against a market total of 54.5 -- so the
            # "projected score" the dashboard derived from it was the same score
            # sixteen times with the margin written on it.
            #
            # Read score_model's docstring before treating this as an upgrade in
            # accuracy: walk-forward it is 0.07 points of MAE better than the
            # constant and half a point worse than the closing total. What it buys
            # is a number that MOVES per game and that is unbiased (the anchor sat
            # 1.6 points below the average real total every season).
            base_total = score_model.game_total(con, season, week, g["home_abbr"], g["away_abbr"])
            if base_total is None:
                base_total = FALLBACK_TOTAL
            final_total_adj = weather_total + rivalry_total
            market_total = g["closing_total"]
            model_total = base_total + final_total_adj
            final_total = (model_total * (1 - MARKET_BLEND_WEIGHT) + market_total * MARKET_BLEND_WEIGHT
                           if market_total is not None else model_total)

            # The projected SCORE: our own total split by our own margin. Both legs
            # are the un-blended model numbers on purpose -- deriving the score from
            # final_spread/final_total would print the market's score back at the
            # reader with a 20% nudge on it and call it our prediction.
            pred_home, pred_away = score_model.split_score(model_total, pre_shrink)

            win_prob = _win_prob(final_spread)

            # Count only the legs that can actually be non-zero. weather's spread leg is
            # structurally 0.0 (see adjustments.py) and rivalry now reaches the spread
            # solely through the compression delta folded in above, so the old tuple was
            # counting two terms that never moved.
            n_adjustments_fired = sum(1 for v in (hfa, rest, rivalry_spread, ref, injury) if abs(v) > 0.01)

            # The lean is measured on our OWN number (pre_shrink / model_total), not on
            # the blended one. `final_spread - market` is identically
            # (1 - MARKET_BLEND_WEIGHT) * (pre_shrink - market) -- blending and then
            # measuring the gap off the blend just reports a fifth of the disagreement
            # and rescales every threshold silently whenever the blend weight moves.
            # Sign is unchanged either way, so the pick direction (and anything in
            # backtest/log_results.py grading off final_spread) is unaffected.
            spread_lean = (pre_shrink - market_spread) if market_spread is not None else None
            total_lean = (model_total - market_total) if market_total is not None else None

            # Confidence is an INPUT-QUALITY figure, not a bet grade -- see
            # projection/confidence.py for the full reasoning and for why |edge| is
            # deliberately kept out of it. The old formula here was
            # `100 * (home_n + away_n)/16 * (1 - 0.08*n_fired)`, which could not reach
            # its own ceiling before week 9 (the rolling window caps at 8 per team) and
            # gave every game in a week the same certainty. It printed 8.5-10.5 across
            # all sixteen 2026 week-2 games.
            scored = conf.score_game(
                game_week=week,
                home=home_src,
                away=away_src,
                # None = unmeasurable (already-kicked-off game, gross never stored),
                # which confidence drops and renormalizes rather than scoring off the net.
                injury_gross=injury_gross,
                n_adjustments_fired=n_adjustments_fired,
                has_market_line=market_spread is not None,
                has_weather=wrow is not None,
                is_indoors=g["roof_type"] in ("dome", "closed"),
                has_rest=g["rest_days_home"] is not None and g["rest_days_away"] is not None,
                home_sd=_rating_volatility(con, g["home_abbr"], season, week),
                away_sd=_rating_volatility(con, g["away_abbr"], season, week),
                spread_edge=spread_lean,
            )
            confidence = scored["confidence"]
            components = scored["components"]
            agreement = scored["market_agreement"]

            con.execute(
                """INSERT INTO projections(game_id, base_score_diff, hfa_adj, weather_adj, rest_adj,
                       rivalry_adj, ref_adj, injury_adj, injury_gross,
                       pre_shrink_spread, pre_shrink_total,
                       final_spread, final_total,
                       home_win_prob, pred_home_score, pred_away_score,
                       confidence_score, confidence_parts_json, market_agreement,
                       generated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(game_id) DO UPDATE SET
                       base_score_diff=excluded.base_score_diff, hfa_adj=excluded.hfa_adj,
                       weather_adj=excluded.weather_adj, rest_adj=excluded.rest_adj,
                       rivalry_adj=excluded.rivalry_adj, ref_adj=excluded.ref_adj,
                       injury_adj=excluded.injury_adj, injury_gross=excluded.injury_gross,
                       pre_shrink_spread=excluded.pre_shrink_spread,
                       pre_shrink_total=excluded.pre_shrink_total, final_spread=excluded.final_spread,
                       final_total=excluded.final_total, home_win_prob=excluded.home_win_prob,
                       pred_home_score=excluded.pred_home_score,
                       pred_away_score=excluded.pred_away_score,
                       confidence_score=excluded.confidence_score,
                       confidence_parts_json=excluded.confidence_parts_json,
                       market_agreement=excluded.market_agreement,
                       generated_at=excluded.generated_at""",
                (g["game_id"], base_diff, hfa, weather_spread, rest, rivalry_spread, ref, injury,
                 injury_gross, pre_shrink, model_total, final_spread, final_total, win_prob,
                 pred_home, pred_away, confidence,
                 json.dumps(components), agreement,
                 datetime.now(timezone.utc).isoformat(timespec="minutes")),
            )

            # A pick needs a gap big enough to be an opinion rather than shrinkage.
            # At the old 0.05 threshold a spread pick fired on all 16 games every
            # week. A pick that no longer qualifies (market moved onto our number,
            # the line just arrived, a starter's status gutted the inputs) must
            # clear its old best_bets row, not leave it up.
            spread_qualifies = (spread_lean is not None
                                and MIN_SPREAD_EDGE <= abs(spread_lean) <= MAX_SPREAD_LEAN)
            if spread_qualifies:
                pick = g["home_abbr"] if spread_lean > 0 else g["away_abbr"]
                reasoning = (f"Our own number favors {'home' if spread_lean > 0 else 'away'} "
                             f"by {abs(spread_lean):.1f} pts vs. the market line. "
                             f"{conf.weakest_link_text(components).capitalize()}.")
                con.execute(BEST_BET_UPSERT, (g["game_id"], season, week, "spread", pick, confidence, reasoning))
            else:
                con.execute("DELETE FROM best_bets WHERE game_id=? AND bet_type='spread'", (g["game_id"],))

            # The total leg is now a real per-game opinion, and it still does not win.
            #
            # It used to be worse than that: with the flat 44.0 anchor, corr(total_lean,
            # market_total) was -0.994, so the "lean" was arithmetically just 44 minus
            # the market total. Raising the threshold did not select better-informed
            # picks, it selected the highest market totals and called UNDER on them --
            # 63% UNDER at any lean, 80% at >=6. That leg was a disguised bias.
            #
            # With projection.score_model supplying the total, measured over the same
            # 2,851 graded games: corr(total_lean, market_total) = -0.613, corr(our
            # total, the market's) = +0.561, and the UNDER share is 47.6% overall and
            # 49.2% inside the 2-6 band. It is a two-sided opinion now.
            #
            # But it grades 47.1% in that band against a 52.4% break-even, and 49.5%
            # across every game. Being an honest model did not make it a profitable
            # one -- the closing total is efficient (actual ~= 0.52 + 0.998 * market)
            # and our MAE is 10.90 against the market's 10.45. Print these picks as
            # what the model thinks, not as bets that are expected to clear the vig.
            total_qualifies = (total_lean is not None
                               and MIN_TOTAL_EDGE <= abs(total_lean) <= MAX_TOTAL_LEAN)
            if total_qualifies:
                pick = "OVER" if total_lean > 0 else "UNDER"
                reasoning = (f"Model total is {abs(total_lean):.1f} pts "
                             f"{'above' if total_lean > 0 else 'below'} the market total.")
                con.execute(BEST_BET_UPSERT, (g["game_id"], season, week, "total", pick, confidence, reasoning))
            else:
                con.execute("DELETE FROM best_bets WHERE game_id=? AND bet_type='total'", (g["game_id"],))
            saved += 1
    return saved


if __name__ == "__main__":
    season = int(sys.argv[1]) if len(sys.argv) > 1 else 2025
    week = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    n = project_week(season, week)
    print(f"Projected {n} games for {season} week {week}")
