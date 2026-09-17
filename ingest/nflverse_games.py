"""Pull the full nflverse schedule/results/closing-lines/referee file and
upsert into `teams` and `games`. This one CSV (nfldata's games.csv) already
carries rest days and div_game, so no separate rest-day computation is needed."""
from __future__ import annotations

import csv
import io
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))
from common import TEAM_NAMES, cached_fetch
from db.db import connect

GAMES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
GAMES_CACHE_HOURS = 12  # results/lines trickle in through the week
EASTERN = ZoneInfo("America/New_York")


def _int_or_none(v):
    return int(v) if v not in (None, "") else None


def _float_or_none(v):
    return float(v) if v not in (None, "") else None


def _kickoff_utc(gameday: str | None, gametime: str | None) -> str | None:
    """nflverse's gameday/gametime are Eastern time. Convert to real UTC so the
    games.kickoff_utc column means what it says and ingest/weather.py's
    forecast window lands on the actual kickoff hour (a 1pm ET kickoff stored
    as-is used to pull the 9am forecast)."""
    if not gameday:
        return None
    local = datetime.fromisoformat(f"{gameday}T{gametime or '00:00'}").replace(tzinfo=EASTERN)
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def load_games_csv() -> list[dict]:
    text = cached_fetch("nflverse_games.csv", GAMES_URL, GAMES_CACHE_HOURS)
    return list(csv.DictReader(io.StringIO(text)))


def ingest(seasons: list[int]) -> int:
    rows = load_games_csv()
    wanted = set(seasons)
    saved = 0
    with connect() as con:
        for abbr, name in TEAM_NAMES.items():
            con.execute(
                "INSERT INTO teams(team_id, name) VALUES(?,?) "
                "ON CONFLICT(team_id) DO UPDATE SET name=excluded.name",
                (abbr, name),
            )
        for r in rows:
            season = _int_or_none(r.get("season"))
            if season not in wanted or r.get("game_type") != "REG":
                continue
            kickoff = _kickoff_utc(r.get("gameday"), r.get("gametime"))
            con.execute(
                """INSERT INTO games(game_id, season, week, game_type, kickoff_utc,
                       home_abbr, away_abbr, roof_type, is_divisional,
                       rest_days_home, rest_days_away, closing_spread, closing_total,
                       referee, home_score, away_score)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(game_id) DO UPDATE SET
                       kickoff_utc=excluded.kickoff_utc,
                       roof_type=excluded.roof_type,
                       is_divisional=excluded.is_divisional,
                       rest_days_home=excluded.rest_days_home,
                       rest_days_away=excluded.rest_days_away,
                       closing_spread=COALESCE(excluded.closing_spread, games.closing_spread),
                       closing_total=COALESCE(excluded.closing_total, games.closing_total),
                       referee=COALESCE(excluded.referee, games.referee),
                       home_score=excluded.home_score,
                       away_score=excluded.away_score""",
                (
                    r["game_id"], season, _int_or_none(r.get("week")), r.get("game_type"),
                    kickoff, r.get("home_team"), r.get("away_team"), r.get("roof"),
                    _int_or_none(r.get("div_game")),
                    _int_or_none(r.get("home_rest")), _int_or_none(r.get("away_rest")),
                    _float_or_none(r.get("spread_line")), _float_or_none(r.get("total_line")),
                    r.get("referee") or None,
                    _int_or_none(r.get("home_score")), _int_or_none(r.get("away_score")),
                ),
            )
            saved += 1
    return saved


if __name__ == "__main__":
    from db.db import init_db
    init_db()
    seasons = [int(x) for x in (sys.argv[1:] or [2025])]
    print(f"Saved {ingest(seasons)} games for seasons {seasons}")
