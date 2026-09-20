"""Per-game points model: how many points does each team actually score?

This exists so the board can print a real projected score. The total leg used to
be a flat 44.0 anchor that only weather and rivalry could move, which gave the
stored total a standard deviation of 1.39 against the market's 4.41. On the 2026
week-2 board it read 42, 43 or 44 on all sixteen games -- including 44.0 for
DET@BUF against a market total of 54.5. Splitting a constant by the spread does
not produce a prediction of the score; it produces the same score sixteen times
with the margin written on it.

THE MODEL. One least-squares fit over team-games, where each row is one team in
one game and the response is the points that team scored:

    points = a + b * (its own offense rating)
               + c * (the opponent's defense rating)
               + d * (1 if it is the home team)

Ratings are always read AS OF THE WEEK BEFORE the game (grading.team_score's
rolling window), the same no-peeking rule projection.base_model follows. Fit on
completed prior seasons only, so the coefficients track the current scoring
environment rather than an average of 2015 and today.

WHAT THIS BUYS, AND WHAT IT DOES NOT. Measured walk-forward over 2,399 games --
fit on prior seasons, predict the next, never peeking at the season being scored:

    this model            total MAE 10.98    mean error +0.06
    the old flat anchor   total MAE 11.05    mean error -1.62
    the closing total     total MAE 10.45

It is 0.07 points of MAE better than a constant and half a point worse than the
market. The accuracy gain is nil and must not be advertised as one. Two things it
does buy are real:

  1. It MOVES. A per-game number can be looked at and disagreed with; 44.0
     sixteen times cannot.
  2. It is UNBIASED. The flat anchor sat 1.6 points below the average real total
     every single season, because the league scores 45.7 and the anchor said 44.

READ THE SPREAD OF IT HONESTLY. Real game totals have a standard deviation of
13.9 points. This model predicts inside a band of about 3.6, because least
squares shrinks toward the mean in proportion to how little the inputs explain,
and that shrinkage is correct -- a model that predicted 55s and 30s at the right
frequency would be worse, not braver. The printed score is the centre of a very
wide distribution, not a forecast of the final.

TRAINING WINDOW. 1, 2, 3, 5 and all-prior-season lookbacks were measured and land
within 0.08 MAE of each other, so the choice is nearly free. Three is taken
because it is a real sample (~800 games), it keeps the mean error inside a tenth
of a point, and its prediction spread (sd 3.62) is the closest of the honest
options to the market's 4.41 without overfitting to one season's scoring climate.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import db.db
from db.db import connect

TRAIN_SEASONS = 3

# (intercept, own_offense, opponent_defense, home). Only used when the DB has no
# graded history to fit on -- the values are the 2015-2025 fit, so a cold start
# predicts something sane rather than collapsing to the intercept.
DEFAULT_COEFFS = (9.27, 0.4295, -0.178, 1.77)

_FIT_CACHE: dict[tuple, tuple[float, ...]] = {}


def _ratings_before(con, team: str, season: int, week: int):
    """(offense_score, defense_score) as of the week BEFORE this game, falling back
    to the team's last graded week of the prior season. Same no-peeking rule as
    projection.base_model._team_score_before."""
    row = con.execute(
        """SELECT offense_score, defense_score FROM team_scores
           WHERE team_abbr=? AND season=? AND week<? AND overall_score IS NOT NULL
           ORDER BY week DESC LIMIT 1""",
        (team, season, week),
    ).fetchone()
    if row is None:
        row = con.execute(
            """SELECT offense_score, defense_score FROM team_scores
               WHERE team_abbr=? AND season=? AND overall_score IS NOT NULL
               ORDER BY week DESC LIMIT 1""",
            (team, season - 1),
        ).fetchone()
    if row is None or row["offense_score"] is None or row["defense_score"] is None:
        return None
    return row["offense_score"], row["defense_score"]


def _solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Gauss-Jordan on the normal equations. numpy is not in this project's deps."""
    n = len(b)
    m = [a[i][:] + [b[i]] for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            return None
        m[col], m[piv] = m[piv], m[col]
        for r in range(n):
            if r == col:
                continue
            f = m[r][col] / m[col][col]
            for k in range(col, n + 1):
                m[r][k] -= f * m[col][k]
    return [m[i][n] / m[i][i] for i in range(n)]


def training_rows(con, seasons: list[int]) -> list[tuple[list[float], float]]:
    """One row per TEAM-GAME: ([1, own_off, opp_def, is_home], points_scored)."""
    if not seasons:
        return []
    placeholders = ",".join("?" * len(seasons))
    games = con.execute(
        "SELECT season, week, home_abbr, away_abbr, home_score, away_score "
        "FROM games WHERE season IN (" + placeholders + ") AND game_type='REG' "
        "AND home_score IS NOT NULL AND away_score IS NOT NULL",
        seasons,
    ).fetchall()
    rows = []
    for g in games:
        home = _ratings_before(con, g["home_abbr"], g["season"], g["week"])
        away = _ratings_before(con, g["away_abbr"], g["season"], g["week"])
        if home is None or away is None:
            continue
        rows.append(([1.0, home[0], away[1], 1.0], float(g["home_score"])))
        rows.append(([1.0, away[0], home[1], 0.0], float(g["away_score"])))
    return rows


def fit_coeffs(con, season: int) -> tuple[float, ...]:
    """Coefficients for a game in `season`, fit ONLY on completed prior seasons.

    Cached per training window: project_week calls this once per game, and a full
    rebuild walks 12 seasons x 18 weeks x 16 games, which would otherwise refit
    the same regression thousands of times.

    The cache key includes the DATABASE, not just the seasons. The project's
    safe-testing recipe overrides db.db.DB_PATH to point at a copy, so a process
    that projects the live DB and then a copy would otherwise get the live
    coefficients silently reused against the copy -- the exact case the recipe
    exists to keep separate."""
    seasons = [season - i for i in range(1, TRAIN_SEASONS + 1)]
    key = (str(db.db.DB_PATH),) + tuple(seasons)
    if key in _FIT_CACHE:
        return _FIT_CACHE[key]
    rows = training_rows(con, seasons)
    coeffs = DEFAULT_COEFFS
    if len(rows) >= 200:
        p = len(rows[0][0])
        ata = [[sum(x[j] * x[k] for x, _ in rows) for k in range(p)] for j in range(p)]
        atb = [sum(x[j] * y for x, y in rows) for j in range(p)]
        solved = _solve(ata, atb)
        if solved is not None:
            coeffs = tuple(solved)
    _FIT_CACHE[key] = coeffs
    return coeffs


def predict_points(coeffs, own_off: float, opp_def: float, is_home: bool) -> float:
    a, b, c, d = coeffs
    return a + b * own_off + c * opp_def + (d if is_home else 0.0)


def game_total(con, season: int, week: int, home_abbr: str, away_abbr: str) -> float | None:
    """Our own expected total for this matchup, before weather and rivalry are
    applied. None when either team has no rating to read, which project.py handles
    the same way it handles a missing overall_score: skip rather than guess."""
    home = _ratings_before(con, home_abbr, season, week)
    away = _ratings_before(con, away_abbr, season, week)
    if home is None or away is None:
        return None
    coeffs = fit_coeffs(con, season)
    return (predict_points(coeffs, home[0], away[1], True)
            + predict_points(coeffs, away[0], home[1], False))


def split_score(total: float, spread_home: float) -> tuple[int, int]:
    """(home_points, away_points) from a total and a home-favored-positive margin.

    Each side is rounded independently, so the pair can add to one point more or
    less than the printed total. That is deliberate: a real football score is two
    integers, and forcing the pair to reconcile would mean printing a margin or a
    total we did not actually predict."""
    return round((total + spread_home) / 2), round((total - spread_home) / 2)


if __name__ == "__main__":
    season = int(sys.argv[1]) if len(sys.argv) > 1 else 2026
    with connect() as con:
        c = fit_coeffs(con, season)
        print(f"{season} coefficients (fit on {TRAIN_SEASONS} prior seasons):")
        print(f"  intercept        {c[0]:+.3f}")
        print(f"  own offense      {c[1]:+.4f} pts per rating point")
        print(f"  opponent defense {c[2]:+.4f} pts per rating point")
        print(f"  home field       {c[3]:+.3f} pts")
