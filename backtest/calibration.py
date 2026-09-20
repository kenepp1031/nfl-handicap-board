"""Confidence-vs-results check (framework §6: "watch calibration, not just win rate").

Buckets graded picks by confidence_score and prints the ACTUAL ATS win rate in
each bucket.

READ THIS BEFORE READING THE TABLE. confidence_score is NOT a probability and
this is not a calibration curve. An 80 does not mean "hits 80% of the time" and
never did -- projection/confidence.py scores how complete and settled a game's
INPUTS are, which is a different question from whether the pick wins. The
relationship has been measured over 2,654 graded games and it is flat to
slightly inverted: 52.5% ATS in the 40-50 bucket against 48.1% at 80+, with our
spread MAE running 10.42 in the lowest bucket and 10.78 in the highest.

So a non-monotonic table here is the EXPECTED result, not a defect to fix, and
the right response is never to start ranking or gating bets by confidence --
that was tried and it made the board worse (see the threshold note in
projection/project.py). What this report is still good for is catching a
REGRESSION: if confidence ever starts tracking win rate strongly in either
direction, something has leaked the market or the result into the inputs.
"""
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
    print("\nFlat or non-monotonic is the expected reading: confidence scores INPUT "
          "QUALITY, not win probability, and it has been measured as slightly "
          "INVERTED against ATS (52.5% at 40-50 vs 48.1% at 80+). Do not gate or "
          "rank bets on it. A strong trend in either direction is the thing worth "
          "investigating -- it would mean the result has leaked into the inputs.")


if __name__ == "__main__":
    seasons = [int(x) for x in (sys.argv[1:] or [2024])]
    report(seasons)
