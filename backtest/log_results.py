"""After final scores land in `games`, log projection accuracy into
backtest_log (framework §3.9): predicted vs. closing spread/total, ATS/O-U
result, and which adjustments fired (for the per-factor breakdown in report.py)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from db.db import connect


def _ats_result(predicted_spread, closing_spread, margin) -> str | None:
    """Grades the model's OWN pick against what actually happened, using the
    closing line as the market baseline: did picking the side the model favored
    (relative to market) win, lose, or push against the actual margin?"""
    if predicted_spread is None or closing_spread is None or margin is None:
        return None
    edge = predicted_spread - closing_spread
    if abs(edge) < 0.05:
        return None  # no real lean vs. market, nothing to grade
    picked_home = edge > 0
    cover_margin = margin - closing_spread
    if abs(cover_margin) < 0.01:
        return "push"
    home_covered = cover_margin > 0
    return "win" if (picked_home == home_covered) else "loss"


def _ou_result(predicted_total, closing_total, actual_total) -> str | None:
    if predicted_total is None or closing_total is None or actual_total is None:
        return None
    edge = predicted_total - closing_total
    if abs(edge) < 0.05:
        return None
    picked_over = edge > 0
    if abs(actual_total - closing_total) < 0.01:
        return "push"
    went_over = actual_total > closing_total
    return "win" if (picked_over == went_over) else "loss"


def log_season(season: int) -> int:
    saved = 0
    with connect() as con:
        rows = con.execute(
            """SELECT g.*, p.pre_shrink_spread, p.final_spread, p.final_total,
                      p.hfa_adj, p.weather_adj, p.rest_adj, p.rivalry_adj, p.ref_adj, p.injury_adj
               FROM games g JOIN projections p ON p.game_id = g.game_id
               WHERE g.season=? AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL""",
            (season,),
        ).fetchall()
        for g in rows:
            margin = g["home_score"] - g["away_score"]
            actual_total = g["home_score"] + g["away_score"]
            error_spread = (g["final_spread"] - margin) if g["final_spread"] is not None else None
            error_total = (g["final_total"] - actual_total) if g["final_total"] is not None else None
            ats = _ats_result(g["final_spread"], g["closing_spread"], margin)
            ou = _ou_result(g["final_total"], g["closing_total"], actual_total)
            fired = {name: g[col] for name, col in (
                ("hfa", "hfa_adj"), ("weather", "weather_adj"),
                ("rest", "rest_adj"), ("rivalry", "rivalry_adj"), ("ref", "ref_adj"),
                ("injury", "injury_adj"),
            ) if g[col] is not None and abs(g[col]) > 0.01}
            con.execute(
                """INSERT INTO backtest_log(game_id, season, week, predicted_spread, closing_spread,
                       error_spread, predicted_total, closing_total, error_total, ats_result, ou_result,
                       adjustments_fired_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(game_id) DO UPDATE SET
                       predicted_spread=excluded.predicted_spread, closing_spread=excluded.closing_spread,
                       error_spread=excluded.error_spread, predicted_total=excluded.predicted_total,
                       closing_total=excluded.closing_total, error_total=excluded.error_total,
                       ats_result=excluded.ats_result, ou_result=excluded.ou_result,
                       adjustments_fired_json=excluded.adjustments_fired_json""",
                (g["game_id"], season, g["week"], g["final_spread"], g["closing_spread"],
                 error_spread, g["final_total"], g["closing_total"], error_total, ats, ou,
                 json.dumps(fired)),
            )
            saved += 1
    return saved


if __name__ == "__main__":
    years = [int(x) for x in (sys.argv[1:] or [2024])]
    for y in years:
        n = log_season(y)
        print(f"{y}: logged {n} games")
