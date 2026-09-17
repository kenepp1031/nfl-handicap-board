"""Base projection (framework §3.5): linear score-diff -> point-spread mapping.

Calibrated by regressing (home_overall_score - away_overall_score), using each
team's rolling score AS OF THE WEEK BEFORE the game (so calibration never
peeks at the outcome it's predicting), against the actual final-score margin.
One coefficient, fit through the origin -- home-field advantage is deliberately
left out here and applied later as its own adjustment (framework §3.6), so this
step stays exactly the "one linear coefficient" the framework calls for.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from db.db import connect

DEFAULT_COEFFICIENT = 0.35  # points of margin per point of score_diff, until calibrated


def _team_score_before(con, team: str, season: int, week: int) -> float | None:
    row = con.execute(
        """SELECT overall_score FROM team_scores
           WHERE team_abbr=? AND season=? AND week < ?
           ORDER BY week DESC LIMIT 1""",
        (team, season, week),
    ).fetchone()
    return row["overall_score"] if row else None


def training_pairs(seasons: list[int]) -> list[tuple[float, float]]:
    pairs = []
    with connect() as con:
        games = con.execute(
            """SELECT season, week, home_abbr, away_abbr, home_score, away_score
               FROM games WHERE season IN ({}) AND game_type='REG'
                 AND home_score IS NOT NULL AND away_score IS NOT NULL AND week > 1""".format(
                ",".join("?" * len(seasons))
            ),
            seasons,
        ).fetchall()
        for g in games:
            home = _team_score_before(con, g["home_abbr"], g["season"], g["week"])
            away = _team_score_before(con, g["away_abbr"], g["season"], g["week"])
            if home is None or away is None:
                continue
            diff = home - away
            margin = g["home_score"] - g["away_score"]
            pairs.append((diff, margin))
    return pairs


def fit_coefficient(pairs: list[tuple[float, float]]) -> float:
    """Least squares through the origin: k = sum(x*y) / sum(x*x)."""
    num = sum(x * y for x, y in pairs)
    den = sum(x * x for x, y in pairs)
    if den <= 0:
        return DEFAULT_COEFFICIENT
    return num / den


def mean_absolute_error(pairs: list[tuple[float, float]], k: float) -> float:
    if not pairs:
        return float("nan")
    errors = [abs(k * x - y) for x, y in pairs]
    return sum(errors) / len(errors)


if __name__ == "__main__":
    years = [int(x) for x in (sys.argv[1:] or [2023, 2024, 2025])]
    pairs = training_pairs(years)
    k = fit_coefficient(pairs)
    mae = mean_absolute_error(pairs, k)
    print(f"Seasons {years}: {len(pairs)} games, coefficient={k:.4f}, MAE={mae:.2f} pts")
