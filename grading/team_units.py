"""Team-level weekly EPA/play, offense and defense. Ported from the old app's
`team_units()`. Used two ways: as the OL/DEF team-unit-proxy grade input
(player_grades.py) and as an input to the base team score (team_score.py)."""
from __future__ import annotations

import csv
import io
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from common import cached_fetch

STATS_URL_FMT = "https://github.com/nflverse/nflverse-data/releases/download/stats_team/stats_team_week_{year}.csv"
STATS_CACHE_HOURS = 12


def load_team_week_epa(year: int) -> dict[tuple[str, int], dict]:
    """Returns {(team_abbr, week): {'off_epa_play', 'off_plays', 'def_epa_play', 'def_plays'}}."""
    text = cached_fetch(f"stats_team_week_{year}.csv", STATS_URL_FMT.format(year=year), STATS_CACHE_HOURS)
    rows = csv.DictReader(io.StringIO(text))
    totals: dict[tuple[str, int], dict] = {}
    for r in rows:
        if r.get("season_type") != "REG":
            continue
        try:
            week = int(r["week"])
            epa = float(r["passing_epa"]) + float(r["rushing_epa"])
            plays = float(r["attempts"]) + float(r["sacks_suffered"]) + float(r["carries"])
        except (KeyError, ValueError, TypeError):
            continue
        if plays <= 0 or not math.isfinite(epa):
            continue
        off_team, def_team = r["team"], r["opponent_team"]
        o = totals.setdefault((off_team, week), {"off_epa": 0.0, "off_plays": 0.0, "def_epa": 0.0, "def_plays": 0.0})
        o["off_epa"] += epa
        o["off_plays"] += plays
        d = totals.setdefault((def_team, week), {"off_epa": 0.0, "off_plays": 0.0, "def_epa": 0.0, "def_plays": 0.0})
        d["def_epa"] += -epa
        d["def_plays"] += plays
    result = {}
    for key, v in totals.items():
        result[key] = {
            "off_epa_play": v["off_epa"] / v["off_plays"] if v["off_plays"] else None,
            "off_plays": v["off_plays"],
            "def_epa_play": v["def_epa"] / v["def_plays"] if v["def_plays"] else None,
            "def_plays": v["def_plays"],
        }
    return result
