"""Pull player weekly stats + snap counts + the player ID crosswalk, join them,
and upsert into `players` / `player_game_stats`. No nfl_data_py/pandas -- plain
urllib + stdlib csv, same as the rest of this app."""
from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from common import cached_fetch
from db.db import connect
from ingest.nflverse_pbp import load_pbp_player_week_stats

PLAYERS_URL = "https://github.com/nflverse/nflverse-data/releases/download/players/players.csv"
PLAYERS_CACHE_HOURS = 24 * 7  # roster identity churns slowly; a week-old crosswalk is fine

STATS_URL_FMT = "https://github.com/nflverse/nflverse-data/releases/download/player_stats/player_stats_{year}.csv"
SNAPS_URL_FMT = "https://github.com/nflverse/nflverse-data/releases/download/snap_counts/snap_counts_{year}.csv"
SEASON_CACHE_HOURS = 12  # current season's weekly files update after games are played


def _load_crosswalk() -> dict[str, dict]:
    """Returns {pfr_id: {gsis_id, name, position}} for joining snap_counts (pfr_id)
    to player_stats (gsis id)."""
    text = cached_fetch("players.csv", PLAYERS_URL, PLAYERS_CACHE_HOURS)
    rows = csv.DictReader(io.StringIO(text))
    by_pfr = {}
    for r in rows:
        pfr = r.get("pfr_id")
        if not pfr:
            continue
        by_pfr[pfr] = {
            "gsis_id": r.get("gsis_id") or None,
            "name": r.get("display_name") or "",
            "position": r.get("position") or "",
        }
    return by_pfr


def _load_player_stats(year: int) -> list[dict]:
    text = cached_fetch(f"player_stats_{year}.csv", STATS_URL_FMT.format(year=year), SEASON_CACHE_HOURS)
    return list(csv.DictReader(io.StringIO(text)))


def _load_snap_counts(year: int) -> list[dict]:
    text = cached_fetch(f"snap_counts_{year}.csv", SNAPS_URL_FMT.format(year=year), SEASON_CACHE_HOURS)
    return list(csv.DictReader(io.StringIO(text)))


def _pct(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def ingest_season(year: int) -> tuple[int, int]:
    """Upserts players + player_game_stats for one season. Returns
    (players_upserted, player_game_rows_upserted). player_stats and
    snap_counts are fetched independently -- nflverse doesn't always publish
    them on the same schedule (confirmed: snap_counts_2025.csv already exists
    while player_stats_2025.csv doesn't yet), so a missing one shouldn't throw
    away the other. Without snap_counts there's no roster/snap data to ingest
    at all, so that's the one that determines whether this returns real rows;
    callers should fall back to grading.team_score.compute_from_team_units_season
    only when this returns (0, 0)."""
    crosswalk = _load_crosswalk()
    pbp_defense: dict[tuple[str, int], dict] = {}
    try:
        stats_rows = _load_player_stats(year)
    except Exception as ex:
        print(f"  player_stats unavailable for {year} ({ex}); deriving individual offense/defense "
              f"grades from play-by-play instead")
        stats_rows = []
        by_gsis = {v["gsis_id"]: v for v in crosswalk.values() if v["gsis_id"]}
        try:
            pbp_offense, pbp_defense = load_pbp_player_week_stats(year)
        except Exception as pbp_ex:
            print(f"  play-by-play also unavailable for {year} ({pbp_ex}); "
                  f"QB/RB/WR/TE will use the team-unit proxy instead")
            pbp_offense = {}
        for (gsis, week), stat in pbp_offense.items():
            entry = by_gsis.get(gsis)
            position = entry["position"] if entry else ""
            # pbp only carries abbreviated names ("J.Allen"); prefer the crosswalk's
            # full display name so the injury report (full names) can be matched.
            name = (entry["name"] if entry else None) or stat["player_display_name"] or gsis
            stats_rows.append({
                "season_type": "REG", "player_id": gsis, "week": week,
                "position": position, "player_display_name": name,
                "recent_team": stat["recent_team"], "opponent_team": stat["opponent_team"],
                "passing_epa": stat["passing_epa"], "rushing_epa": stat["rushing_epa"],
                "receiving_epa": stat["receiving_epa"], "attempts": stat["attempts"],
                "sacks": stat["sacks"], "carries": stat["carries"], "targets": stat["targets"],
            })
    try:
        snap_rows = _load_snap_counts(year)
    except Exception as ex:
        print(f"  snap_counts unavailable for {year} ({ex}); no roster/snap data to ingest this season")
        return 0, 0

    # snap_counts is keyed by pfr_id; player_stats is keyed by gsis player_id.
    # Build gsis_id -> snap rows (by season/week) via the crosswalk.
    snaps_by_gsis_week: dict[tuple[str, int], dict] = {}
    for row in snap_rows:
        pfr = row.get("pfr_player_id")
        entry = crosswalk.get(pfr)
        gsis = entry["gsis_id"] if entry else None
        if not gsis:
            continue
        week = int(row["week"]) if row.get("week") else None
        if week is None:
            continue
        snaps_by_gsis_week[(gsis, week)] = row

    players_seen: dict[str, dict] = {}
    offense_written: dict[tuple[str, int], dict] = {}
    game_rows = 0
    with connect() as con:
        for row in stats_rows:
            if row.get("season_type") != "REG":
                continue
            gsis = row.get("player_id")
            week = int(row["week"]) if row.get("week") else None
            if not gsis or week is None:
                continue
            position = row.get("position") or ""
            name = row.get("player_display_name") or row.get("player_name") or gsis
            team = row.get("recent_team") or None
            players_seen[gsis] = {
                "gsis_id": gsis, "pfr_id": None, "name": name,
                "position": position, "team_abbr": team,
            }
            snap_row = snaps_by_gsis_week.get((gsis, week))
            offense_pct = _pct(snap_row.get("offense_pct")) if snap_row else None
            defense_pct = _pct(snap_row.get("defense_pct")) if snap_row else None
            st_pct = _pct(snap_row.get("st_pct")) if snap_row else None
            snap_pct = max(v for v in (offense_pct, defense_pct, st_pct, 0.0) if v is not None)
            offense_written[(gsis, week)] = row
            con.execute(
                """INSERT INTO player_game_stats(player_id, season, week, team_abbr,
                       opponent_abbr, snap_pct, offense_pct, defense_pct, st_pct, stat_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(player_id, season, week) DO UPDATE SET
                       team_abbr=excluded.team_abbr, opponent_abbr=excluded.opponent_abbr,
                       snap_pct=excluded.snap_pct, offense_pct=excluded.offense_pct,
                       defense_pct=excluded.defense_pct, st_pct=excluded.st_pct,
                       stat_json=excluded.stat_json""",
                (
                    gsis, year, week, team, row.get("opponent_team"),
                    snap_pct, offense_pct, defense_pct, st_pct,
                    json.dumps(row),
                ),
            )
            game_rows += 1

        # Also add defense/OL players from snap_counts even though they have no
        # row in player_stats (which only covers offensive skill positions) --
        # their snap_pct still needs to exist for the team-unit-proxy grading pass.
        # player_game_stats PK is (player_id, season, week); ON CONFLICT DO NOTHING
        # below means an offense row already written above (with real stat_json)
        # is never clobbered by the thinner snap-only row for the same player/week.
        for row in snap_rows:
            week = int(row["week"]) if row.get("week") else None
            if week is None:
                continue
            pfr = row.get("pfr_player_id")
            entry = crosswalk.get(pfr)
            gsis = entry["gsis_id"] if entry else None
            player_id = gsis or pfr
            if not player_id:
                continue
            if gsis and gsis in players_seen:
                players_seen[gsis]["pfr_id"] = pfr
            else:
                name = (entry["name"] if entry else None) or row.get("player") or player_id
                position = (entry["position"] if entry else None) or row.get("position") or ""
                players_seen.setdefault(player_id, {
                    "gsis_id": gsis, "pfr_id": pfr, "name": name,
                    "position": position, "team_abbr": row.get("team"),
                })
            offense_pct = _pct(row.get("offense_pct"))
            defense_pct = _pct(row.get("defense_pct"))
            st_pct = _pct(row.get("st_pct"))
            snap_pct = max(offense_pct, defense_pct, st_pct)
            def_stat = pbp_defense.get((gsis, week)) if gsis else None
            stat_json = None
            if def_stat:
                existing_offense = offense_written.get((gsis, week)) if gsis else None
                merged = dict(existing_offense) if existing_offense else {}
                merged["def_stats"] = def_stat
                stat_json = json.dumps(merged)
            con.execute(
                """INSERT INTO player_game_stats(player_id, season, week, team_abbr,
                       opponent_abbr, snap_pct, offense_pct, defense_pct, st_pct, stat_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(player_id, season, week) DO UPDATE SET
                       stat_json=excluded.stat_json
                       WHERE excluded.stat_json IS NOT NULL""",
                (player_id, year, week, row.get("team"), row.get("opponent"), snap_pct,
                 offense_pct, defense_pct, st_pct, stat_json),
            )
            game_rows += 1

        for p in players_seen.values():
            player_id = p["gsis_id"] or p["pfr_id"]
            con.execute(
                """INSERT INTO players(player_id, gsis_id, pfr_id, name, position, team_abbr)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(player_id) DO UPDATE SET
                       pfr_id=COALESCE(excluded.pfr_id, players.pfr_id),
                       name=excluded.name, position=excluded.position,
                       team_abbr=excluded.team_abbr""",
                (player_id, p["gsis_id"], p["pfr_id"], p["name"], p["position"], p["team_abbr"]),
            )
    return len(players_seen), game_rows


if __name__ == "__main__":
    from db.db import init_db
    init_db()
    years = [int(x) for x in (sys.argv[1:] or [2025])]
    for y in years:
        n_players, n_rows = ingest_season(y)
        print(f"{y}: {n_players} players, {n_rows} player-game rows")
