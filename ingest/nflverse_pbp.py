"""Per-player weekly stats computed directly from nflverse's play-by-play feed,
for use when the aggregated `player_stats` release hasn't caught up to the
current season yet (confirmed: player_stats tops out at 2024 while pbp already
publishes 2025/2026 live -- see ingest/nflverse_players.py). Two outputs:

- offense_stats: {(gsis_id, week): {passing_epa, rushing_epa, receiving_epa,
  attempts, sacks, carries, targets}} -- same field names player_grades._epa_per_play
  already reads out of player_stats.csv, so no grading-code change is needed
  for QB/RB/WR/TE once this is wired into the ingest path.
- defense_stats: {(gsis_id, week): {sacks, tfl, qb_hits, ints, pass_defensed,
  forced_fumbles, solo_tackles, assist_tackles}} -- box-count stats for the new
  individual defensive grade in player_grades.py (no defensive EPA attribution
  exists anywhere in public nflverse data, offense or defense).
"""
from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from common import cached_fetch

PBP_URL_FMT = "https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{year}.csv.gz"
PBP_CACHE_HOURS = 12


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def load_pbp_player_week_stats(year: int) -> tuple[dict[tuple[str, int], dict], dict[tuple[str, int], dict]]:
    """Returns (offense_stats, defense_stats) keyed by (gsis_id, week)."""
    text = cached_fetch(f"play_by_play_{year}.csv", PBP_URL_FMT.format(year=year), PBP_CACHE_HOURS, gzipped=True)
    reader = csv.DictReader(io.StringIO(text))

    offense: dict[tuple[str, int], dict] = {}
    defense: dict[tuple[str, int], dict] = {}

    def off_row(pid: str, week: int) -> dict:
        return offense.setdefault((pid, week), {
            "passing_epa": 0.0, "rushing_epa": 0.0, "receiving_epa": 0.0,
            "attempts": 0.0, "sacks": 0.0, "carries": 0.0, "targets": 0.0,
            "player_display_name": None, "recent_team": None, "opponent_team": None,
        })

    def def_row(pid: str, week: int) -> dict:
        return defense.setdefault((pid, week), {
            "sacks": 0.0, "tfl": 0, "qb_hits": 0, "ints": 0,
            "pass_defensed": 0, "forced_fumbles": 0, "solo_tackles": 0, "assist_tackles": 0,
        })

    for r in reader:
        if r.get("season_type") != "REG":
            continue
        week = _int(r.get("week"))
        if week <= 0:
            continue
        epa = _num(r.get("epa"))
        posteam, defteam = r.get("posteam"), r.get("defteam")

        # -- offense: passer / rusher / receiver EPA attribution --
        passer = r.get("passer_player_id")
        if passer and (r.get("pass_attempt") == "1" or r.get("sack") == "1"):
            row = off_row(passer, week)
            row["passing_epa"] += epa
            if r.get("pass_attempt") == "1":
                row["attempts"] += 1
            if r.get("sack") == "1":
                row["sacks"] += 1
            row["player_display_name"] = r.get("passer_player_name") or row["player_display_name"]
            row["recent_team"], row["opponent_team"] = posteam, defteam

        rusher = r.get("rusher_player_id")
        if rusher and r.get("rush_attempt") == "1":
            row = off_row(rusher, week)
            row["rushing_epa"] += epa
            row["carries"] += 1
            row["player_display_name"] = r.get("rusher_player_name") or row["player_display_name"]
            row["recent_team"], row["opponent_team"] = posteam, defteam

        receiver = r.get("receiver_player_id")
        if receiver and r.get("pass_attempt") == "1":
            row = off_row(receiver, week)
            row["receiving_epa"] += epa
            row["targets"] += 1
            row["player_display_name"] = r.get("receiver_player_name") or row["player_display_name"]
            row["recent_team"], row["opponent_team"] = posteam, defteam

        # -- defense: box-count stats, no EPA attribution available --
        if r.get("sack") == "1":
            for key in ("sack_player_id", "half_sack_1_player_id", "half_sack_2_player_id"):
                pid = r.get(key)
                if pid:
                    def_row(pid, week)["sacks"] += 0.5 if "half_sack" in key else 1.0

        if r.get("tackled_for_loss") == "1":
            for key in ("tackle_for_loss_1_player_id", "tackle_for_loss_2_player_id"):
                pid = r.get(key)
                if pid:
                    def_row(pid, week)["tfl"] += 1

        for key in ("qb_hit_1_player_id", "qb_hit_2_player_id"):
            pid = r.get(key)
            if pid:
                def_row(pid, week)["qb_hits"] += 1

        pid = r.get("interception_player_id")
        if pid:
            def_row(pid, week)["ints"] += 1

        for key in ("pass_defense_1_player_id", "pass_defense_2_player_id"):
            pid = r.get(key)
            if pid:
                def_row(pid, week)["pass_defensed"] += 1

        for key in ("forced_fumble_player_1_player_id", "forced_fumble_player_2_player_id"):
            pid = r.get(key)
            if pid:
                def_row(pid, week)["forced_fumbles"] += 1

        for key in ("solo_tackle_1_player_id", "solo_tackle_2_player_id"):
            pid = r.get(key)
            if pid:
                def_row(pid, week)["solo_tackles"] += 1

        for key in ("assist_tackle_1_player_id", "assist_tackle_2_player_id",
                    "assist_tackle_3_player_id", "assist_tackle_4_player_id"):
            pid = r.get(key)
            if pid:
                def_row(pid, week)["assist_tackles"] += 1

    return offense, defense


if __name__ == "__main__":
    years = [int(x) for x in (sys.argv[1:] or [2025])]
    for y in years:
        off, dfn = load_pbp_player_week_stats(y)
        print(f"{y}: {len(off)} offense player-weeks, {len(dfn)} defense player-weeks")
