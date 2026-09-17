"""Calibration check (framework §6: "watch calibration, not just win rate").
An 80%-confidence pick should hit ~80% of the time -- this buckets graded
picks by confidence and checks the ACTUAL ATS win rate in each bucket. A
model that's correctly uncertain is more useful than one that's confidently
wrong; this is how you catch the difference instead of just eyeballing the
overall record."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from db.db import connect

BUCKETS = [(0, 40), (40, 50), (50, 60), (60, 70), (70, 80), (80, 90), (90, 101)]


def report(seasons: list[int]) -> None:
    with connect() as con:
        rows = con.execute(
            """SELECT p.confidence_score, b.ats_result
               FROM projections p JOIN backtest_log b ON b.game_id = p.game_id
               WHERE b.season IN ({}) AND b.ats_result IS NOT NULL
                 AND p.confidence_score IS NOT NULL""".format(",".join("?" * len(seasons))),
            seasons,
        ).fetchall()
    print(f"Graded picks: {len(rows)}")
    print(f"{'Confidence':<12}{'N':<6}{'Win%':<8}")
    for lo, hi in BUCKETS:
        sub = [r for r in rows if lo <= r["confidence_score"] < hi]
        wins = sum(1 for r in sub if r["ats_result"] == "win")
        losses = sum(1 for r in sub if r["ats_result"] == "loss")
        n = wins + losses
        pct = f"{100 * wins / n:.1f}%" if n else "n/a"
        print(f"{lo:>3}-{hi:<8}{n:<6}{pct:<8}")
    print("\nA well-calibrated model shows win% climbing roughly in step with the "
          "confidence bucket. If it doesn't (flat, or non-monotonic), confidence "
          "isn't tracking real predictive power yet -- treat the confidence number "
          "as decorative, not actionable, until this improves.")


if __name__ == "__main__":
    seasons = [int(x) for x in (sys.argv[1:] or [2024])]
    report(seasons)
