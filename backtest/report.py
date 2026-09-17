"""ATS/O-U record and MAE from backtest_log, broken out per adjustment factor
so it's clear which ones are earning their complexity (framework §3.9)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from db.db import connect


def overall_record(seasons: list[int]) -> dict:
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM backtest_log WHERE season IN ({})".format(",".join("?" * len(seasons))),
            seasons,
        ).fetchall()
    ats_wins = sum(1 for r in rows if r["ats_result"] == "win")
    ats_losses = sum(1 for r in rows if r["ats_result"] == "loss")
    ats_pushes = sum(1 for r in rows if r["ats_result"] == "push")
    ou_wins = sum(1 for r in rows if r["ou_result"] == "win")
    ou_losses = sum(1 for r in rows if r["ou_result"] == "loss")
    spread_errors = [abs(r["error_spread"]) for r in rows if r["error_spread"] is not None]
    total_errors = [abs(r["error_total"]) for r in rows if r["error_total"] is not None]
    return dict(
        games=len(rows),
        ats_record=f"{ats_wins}-{ats_losses}-{ats_pushes}",
        ats_pct=round(100 * ats_wins / (ats_wins + ats_losses), 1) if (ats_wins + ats_losses) else None,
        ou_record=f"{ou_wins}-{ou_losses}",
        ou_pct=round(100 * ou_wins / (ou_wins + ou_losses), 1) if (ou_wins + ou_losses) else None,
        spread_mae=round(sum(spread_errors) / len(spread_errors), 2) if spread_errors else None,
        total_mae=round(sum(total_errors) / len(total_errors), 2) if total_errors else None,
    )


def per_adjustment_breakdown(seasons: list[int]) -> dict:
    """For each adjustment, compares spread MAE on games where it fired
    (non-zero) vs. games where it didn't -- a lower MAE when it fires than
    when it doesn't is evidence the factor is earning its keep."""
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM backtest_log WHERE season IN ({})".format(",".join("?" * len(seasons))),
            seasons,
        ).fetchall()
    factors = ("hfa", "weather", "rest", "rivalry", "ref", "injury")
    result = {}
    for factor in factors:
        fired_errors, not_fired_errors = [], []
        for r in rows:
            if r["error_spread"] is None:
                continue
            fired = json.loads(r["adjustments_fired_json"] or "{}")
            (fired_errors if factor in fired else not_fired_errors).append(abs(r["error_spread"]))
        result[factor] = dict(
            fired_n=len(fired_errors),
            fired_mae=round(sum(fired_errors) / len(fired_errors), 2) if fired_errors else None,
            not_fired_n=len(not_fired_errors),
            not_fired_mae=round(sum(not_fired_errors) / len(not_fired_errors), 2) if not_fired_errors else None,
        )
    return result


if __name__ == "__main__":
    seasons = [int(x) for x in (sys.argv[1:] or [2024])]
    print("=== Overall ===")
    for k, v in overall_record(seasons).items():
        print(f"  {k}: {v}")
    print("=== Per-adjustment (spread MAE, fired vs. not) ===")
    for factor, stats in per_adjustment_breakdown(seasons).items():
        print(f"  {factor}: fired n={stats['fired_n']} MAE={stats['fired_mae']}  |  "
              f"not-fired n={stats['not_fired_n']} MAE={stats['not_fired_mae']}")
