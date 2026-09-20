"""Per-game confidence (framework §3.8) -- an INPUT-QUALITY score, not a bet grade.

Confidence used to be `100 * certainty * stack_penalty`, where
`certainty = (home_games + away_games) / 16`. Two structural faults made that
number useless:

1. `rolling_window_games` caps at 8 per team (grading.team_score.RECENT_GAMES +
   OLDER_GAMES), so the /16 normalizer was unreachable before week 9, and every
   team in a given week carried the same game count. Confidence was therefore a
   week-number clock, identical across the slate -- all 16 games of 2026 week 2
   scored between 8.5 and 10.5, three distinct values on the whole board.

2. It ignored the prior-season carryover. Through
   grading.team_score.EARLY_SEASON_WEEKS the stored score is blended with the
   team's prior-season exit score, so a week-2 rating is not "one game of data"
   -- it is roughly half a full prior season. `effective_games` prices that
   carryover in, discounted for roster turnover.

The score is a weighted MEAN of five per-game quality terms. It deliberately
does NOT include how much data stands behind the two ratings, and it is not a
bet grade. Both of those are measured positions, not preferences:

1. `data_volume` was the whole score's season clock. Across 2,851 graded games
   it ran 0.47 in weeks 1-3 and 1.00 by week 15 while varying almost not at all
   inside a single week (sd 0.021). It moved the entire board up as the year went
   on and separated nothing, which made a fixed threshold on confidence behave as
   "do not bet before week 7". Dropping it flattens the season profile to
   75.2 / 75.8 / 76.1 for early / mid / late while WIDENING the within-week
   spread -- 2026 week 2 goes from 42-52 to 70-87. It is still returned as a
   reported component, because how much data backs a rating is a true and useful
   thing to show; it just cannot be part of the number.

2. Nothing here predicts whether our spread will be right. Correlations between
   each component and our absolute error are all within +-0.05 (rating_stability,
   built specifically to be the per-game separator, comes in at +0.020), and our
   MAE is 10.42 in the lowest confidence bucket against 10.78 in the highest.
   ATS runs the same way, 52.5% at 40-50 and 48.1% at 80+. So this score must
   never gate or rank a bet -- projection.project used to require conf >= 50 and
   that threw away 60% of early-season picks while making them worse (48.9% vs
   50.2%). What it honestly describes is how complete and settled the INPUTS to a
   game are, which is worth printing beside a number and nothing more.

`rating_stability` is the term that most separates games inside a week: weekly
rating volatility ranges from 0.86 to 4.32 points of sd across the league (2025).
`injury_certainty` reads 1.00 on every historical row only because injuries are
computed for unplayed games; on a live board it runs 0.45-0.98.

Deliberately absent for the same reason: |edge| vs the market. Scaling confidence
by how far we sit from the line was measured backwards over 2,654 graded games --
the 80-100 bucket went 47.3% ATS while the 0-20 bucket went 51.1%.
`market_agreement` is returned alongside as its own figure so the lean stays
visible without ranking the board by it.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).parent.parent))
from grading.team_score import (
    RECENT_GAMES, OLDER_GAMES, EARLY_SEASON_WEEKS, PRIOR_SEASON_BLEND,
)


class RatingSource(NamedTuple):
    """Where one team's rating for this game came from. `week` is the week of the
    team_scores row that was read; `from_prior_season` marks the week-1 path where
    no in-season row exists yet and last season's exit score is standing in."""
    n_games: int | None
    week: int | None
    from_prior_season: bool = False

ROLLING_CAP = RECENT_GAMES + OLDER_GAMES  # 8 -- the most games a rating can rest on

# A prior-season exit score rests on a full 8-game rolling window, but it is last
# year's roster and scheme. Count it as most of a window, not all of one.
PRIOR_SEASON_EQUIV_GAMES = 5.0

# A rating row is read as "the latest week strictly before this game". Off a bye
# (or a postponed game) that row is 2+ weeks stale and the team has changed since.
STALENESS_PER_WEEK = 0.12
MAX_STALENESS_WEEKS = 3

# Injury points are priced off a listed-players-at-replacement-level estimate --
# the softest input in the stack. The more of the number that rests on it, the
# less the number is worth.
#
# This reads the GROSS injury points on the game (both teams' charges added),
# not the net adjustment that reaches the spread. Netting was the bug: the two
# sides subtract, so a game where both teams are gutted cancels to nearly zero
# and scored as almost perfectly certain. Measured on the 2026 week-2 board,
# IND@KC carried 4.76 points of injury pricing across the two rosters and a net
# of 0.12, which the old term read as 0.98 -- "barely any of this number is
# injury-priced" -- about a game missing two starting-caliber units. The net is
# what moves the spread; the gross is what the spread is RESTING on, and this
# component is about how much weight sits on the softest input.
#
# Scaled against both teams at injury_adjust.MAX_TEAM_POINTS (7.0 each), i.e.
# the most injury pricing a game can carry, so the term is a clean linear "how
# much of this game is NOT resting on the injury estimate".
#
# No floor. A floor of 0.45 against a 14-point scale clamped roughly half the
# live board to exactly 0.45 (2026 week 2 gross ran 4.4 to 11.5 with a median
# near 10), which turns a measurement into a constant -- the same fault that made
# rating_stability useless as a discriminator. If a game really is priced at the
# whole injury budget, the honest reading is zero, and it costs the score the
# 0.20 this term is weighted at rather than being quietly held up.
INJURY_FULL_DISCOUNT = 14.0

# Each fired adjustment is another hand-set constant standing between the ratings
# and the printed number. Gentler than the old 0.08/factor, and floored.
# Only five legs can fire (hfa, rest, rivalry, ref, injury -- see where project.py
# sets n_adjustments_fired), so this bottoms out
# at 0.70 in practice; the floor is a guard against a future sixth leg, not a
# bound anything currently reaches.
STACK_PER_FACTOR = 0.06
STACK_FLOOR = 0.50

# Weekly sd of a team's overall_score. Across 2025 the league ran 0.86 (CLE) to
# 4.32 (BAL) with a median of 1.83, so 4.0 is roughly "as unstable as it gets".
STABILITY_FULL_DISCOUNT = 4.0
STABILITY_FLOOR = 0.30

# How far our read has to sit from the line before agreement is scored at zero.
DISAGREEMENT_FULL = 7.0

QUALITY_WEIGHTS = {
    "rating_stability": 0.35,
    "freshness": 0.20,
    "injury_certainty": 0.20,
    "input_completeness": 0.15,
    "stack_cleanliness": 0.10,
}


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


# What each term reads on an ORDINARY game: the MEDIAN over 1,088 projected games
# across 2023-2026. injury_certainty is measured on the 255 of those that had not
# kicked off, since it does not exist for the rest.
#
# These are the baselines weakest_component compares against. They are
# descriptive, not targets, and they must be RE-MEASURED whenever a term's scale
# changes -- a stale baseline here does not break the score (nothing feeds off
# it), it just makes the one-line reason on the card point at the wrong input.
TYPICAL = {
    "rating_stability": 0.51,
    "freshness": 1.00,
    "injury_certainty": 0.33,
    "input_completeness": 0.75,
    "stack_cleanliness": 0.88,
}


def weakest_component(quality_parts: dict) -> str | None:
    """The input that is most UNUSUALLY weak for this game, or None if nothing is
    below its normal level.

    Two earlier versions of this both printed the same sentence on nearly every
    card, which is the one thing the line must not do:

    1. `min(parts)` -- the lowest raw value. The terms do not share a scale, so
       this just named whichever term has the lowest scale. rating_stability
       divides by STABILITY_FULL_DISCOUNT and stack_cleanliness cannot drop below
       0.70 by construction, so it said "graded erratically" on 2,828 of 3,106
       games (91%) no matter what was actually wrong.
    2. The largest weight * (1 - value), i.e. the biggest cost in points. Fairer,
       but it lands the same way for a structural reason: rating_stability's
       MEDIAN cost is 0.172 against 0.037 for the next term, because its median
       value is 0.508. A term whose typical game already scores half-bad is a
       constant offset, not a discriminator, so it wins the contest on the median
       game and the line was still ~97% one sentence.

    Comparing each term against what it reads on an ordinary game is what makes
    the sentence informative: it answers "what is worse than usual HERE", which is
    the only version of the question a reader can act on. A game with nothing
    below its baseline gets None and says so, rather than being made to nominate
    a culprit."""
    measured = {k: v for k, v in quality_parts.items()
                if k in QUALITY_WEIGHTS and v is not None}
    shortfalls = {k: QUALITY_WEIGHTS[k] * (TYPICAL.get(k, 1.0) - v)
                  for k, v in measured.items()}
    worst = max(shortfalls, key=shortfalls.get, default=None)
    if worst is None or shortfalls[worst] <= 0.01:
        return None
    return worst


def prior_blend_at(week: int) -> float:
    """The prior-season weight grading.team_score applied to a score row for `week`.
    Mirrors _save_team_scores exactly -- if that taper changes, this follows it."""
    if week > EARLY_SEASON_WEEKS:
        return 0.0
    return PRIOR_SEASON_BLEND * (EARLY_SEASON_WEEKS - week + 1) / EARLY_SEASON_WEEKS


def effective_games(src: RatingSource) -> float:
    """Games-equivalent behind one team's rating, counting the prior-season blend.

    A week-1 rating (n=1, blend=0.5) is worth 1*0.5 + 5*0.5 = 3.0 games, not 1 --
    half of it is a full prior season. By week 7 the blend is off and the answer
    is just the capped game count. A pure prior-season carryover (no in-season row
    at all) is worth exactly one discounted window."""
    if src.from_prior_season:
        return PRIOR_SEASON_EQUIV_GAMES
    n = min(src.n_games or 0, ROLLING_CAP)
    if src.week is None:
        return float(n)
    blend = prior_blend_at(src.week)
    return n * (1 - blend) + PRIOR_SEASON_EQUIV_GAMES * blend


def data_strength(home: RatingSource, away: RatingSource) -> float:
    """Mean of the two teams' effective samples against a full window. Averaged
    rather than summed so one thin side docks the game instead of being masked."""
    return _clamp(((effective_games(home) + effective_games(away)) / 2.0) / ROLLING_CAP)


def _staleness_weeks(game_week: int, src: RatingSource) -> int:
    """How many weeks behind the game the rating row sits. A prior-season
    carryover is charged the full penalty -- it is last year's team."""
    if src.from_prior_season or src.week is None:
        return MAX_STALENESS_WEEKS
    return min(MAX_STALENESS_WEEKS, max(0, game_week - src.week - 1))


def freshness(game_week: int, home: RatingSource, away: RatingSource) -> float:
    """1.0 when both ratings come from the week immediately before kickoff.
    Byes, postponements and a fallback to last season push it down."""
    worst = max(_staleness_weeks(game_week, home), _staleness_weeks(game_week, away))
    return _clamp(1.0 - STALENESS_PER_WEEK * worst)


def injury_certainty(injury_gross: float | None) -> float | None:
    """Docks the game in proportion to how much of it is injury-priced.

    `injury_gross` is both teams' injury charges ADDED, never the net that
    reaches the spread -- see INJURY_FULL_DISCOUNT for why netting read two
    gutted rosters as a clean game.

    None means UNMEASURABLE, not "no injuries", and score_game drops the term and
    renormalizes rather than scoring it. The injuries table is a live snapshot
    with no history, so a game that has already kicked off keeps only the net that
    was stored before kickoff; there is no way to recover its gross. Scoring those
    rows off the net would put them on a different scale from the live board --
    net runs about a quarter of gross -- and quietly hand every historical game a
    higher injury_certainty than any game we actually price today."""
    if injury_gross is None:
        return None
    return _clamp(1.0 - abs(injury_gross) / INJURY_FULL_DISCOUNT)


def input_completeness(has_market_line: bool, has_weather: bool, is_indoors: bool,
                       has_rest: bool) -> float:
    """Fraction of the per-game inputs that actually arrived. A dome needs no
    forecast, so it scores the weather slot as satisfied rather than missing."""
    slots = [
        (0.55, has_market_line),
        (0.25, has_weather or is_indoors),
        (0.20, has_rest),
    ]
    return sum(w for w, ok in slots if ok)


def stack_cleanliness(n_adjustments_fired: int) -> float:
    return _clamp(1.0 - STACK_PER_FACTOR * n_adjustments_fired, STACK_FLOOR, 1.0)


def rating_stability(home_sd: float | None, away_sd: float | None) -> float:
    """How steady the two teams' weekly grades have been. A rating averaged out of
    wildly swinging weeks describes the team less well than the same average out of
    consistent ones, whatever the sample size. Unknown volatility (too few weeks to
    measure) scores at the league median rather than optimistically at 1.0."""
    sds = [s for s in (home_sd, away_sd) if s is not None]
    if not sds:
        return _clamp(1.0 - 1.83 / STABILITY_FULL_DISCOUNT, STABILITY_FLOOR, 1.0)
    worst = max(sds)  # the shakier team sets the ceiling for the game
    return _clamp(1.0 - worst / STABILITY_FULL_DISCOUNT, STABILITY_FLOOR, 1.0)


def market_agreement(spread_edge: float | None) -> float | None:
    """Reported, NOT folded into confidence. 1.0 means our number sits on the
    line. Kept separate because edge-ranked confidence measured backwards (see
    the module docstring) -- this is context for a reader, not a bet grade."""
    if spread_edge is None:
        return None
    return _clamp(1.0 - abs(spread_edge) / DISAGREEMENT_FULL)


def score_game(*, game_week: int, home: RatingSource, away: RatingSource,
               injury_gross: float | None, n_adjustments_fired: int,
               has_market_line: bool, has_weather: bool, is_indoors: bool,
               has_rest: bool, home_sd: float | None = None, away_sd: float | None = None,
               spread_edge: float | None = None) -> dict:
    """Returns the 0-100 confidence plus every component that produced it, so the
    dashboard can say WHY a game scored what it did instead of printing a bare number."""
    quality_parts = {
        "rating_stability": rating_stability(home_sd, away_sd),
        "freshness": freshness(game_week, home, away),
        "injury_certainty": injury_certainty(injury_gross),
        "input_completeness": input_completeness(has_market_line, has_weather, is_indoors, has_rest),
        "stack_cleanliness": stack_cleanliness(n_adjustments_fired),
    }
    # A component that could not be measured is dropped and its weight shared out
    # over the rest, so every game's score is a mean of the terms that exist for it
    # rather than one silently penalised (or, as here, silently flattered) for a
    # missing input.
    measured = {k: v for k, v in quality_parts.items() if v is not None}
    total_weight = sum(QUALITY_WEIGHTS[k] for k in measured) or 1.0
    quality = sum(QUALITY_WEIGHTS[k] * v for k, v in measured.items()) / total_weight
    # data_volume is reported but NOT scored, and NOT eligible to be the weakest
    # link: it is near-identical for every game in a week, so naming it as the
    # limiting factor would print the same sentence on all sixteen cards.
    parts = {"data_volume": data_strength(home, away), **quality_parts}
    return {
        "confidence": round(100 * _clamp(quality), 1),
        "components": {k: (round(v, 3) if v is not None else None) for k, v in parts.items()},
        "market_agreement": market_agreement(spread_edge),
        "weakest": weakest_component(quality_parts),
    }


# Phrased as comparisons, because that is what weakest_component now returns --
# the input furthest below its OWN normal level, not the lowest raw number.
WEAKEST_LABELS = {
    "data_volume": "fewer games than usual sit behind the two ratings",
    "rating_stability": "at least one of these teams has been graded more erratically than most",
    "freshness": "a rating is staler than usual (bye or postponement)",
    "injury_certainty": "more of this game than usual rests on the injury estimate",
    "input_completeness": "an input is missing (line, forecast or rest days)",
    "stack_cleanliness": "this number leans on more adjustments than most",
}
NOTHING_UNUSUAL = "nothing unusual — every input is in normal shape for this game"


def weakest_link_text(components: dict) -> str:
    """Plain-language reason for the score, for the game card."""
    worst = weakest_component(components)
    if worst is None:
        return NOTHING_UNUSUAL
    return WEAKEST_LABELS.get(worst, "")
