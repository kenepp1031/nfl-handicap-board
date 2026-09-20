"""NFL 2.0 weekly pipeline entrypoint.

Usage:
    python main.py                                Auto-detects season/week and runs
                                                    the normal weekly pipeline
    python main.py --week 3                       Normal weekly run for the current season
    python main.py --ingest-seasons 2023,2024,2025   One-time backfill (also run for
                                                       the current season once at the
                                                       start of each year)
    python main.py --week 3 --skip-scrape         Re-run grading/projection only,
                                                    no network calls (fast iteration)
    python main.py --no-publish                   Render locally only; skip the commit +
                                                    push that updates the Streamlit site
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from db.db import connect, init_db
from ingest import nflverse_games, nflverse_players, weather, referees, dk_lines, kalshi, injuries
from grading import player_grades, position_groups, team_score
from projection.base_model import training_pairs, fit_coefficient, DEFAULT_COEFFICIENT
from projection.project import project_week
from backtest.log_results import log_season
from dashboard.render import render_week
from publish import publish_dashboard


def current_season_week(today: date | None = None, refresh_schedule: bool = False) -> tuple[int, int]:
    """(season, week) for today's date, so a double-clicked shortcut needs no
    arguments. Week 1 kicks off the Tuesday after Labor Day; before that date
    we're still in the prior season (playoffs/offseason), so roll back a year
    and clamp to the 18-week regular season.

    The date math is only a fallback for picking the season and for weeks we
    haven't ingested a schedule for yet -- once games exist in the DB for a
    season, roll over to the next week the moment the current one's games are
    all final rather than waiting for a fixed 7-day clock to catch up. With
    refresh_schedule=True the schedule/results are pulled first, so a
    Tuesday-morning run sees Monday night's final score instead of re-rendering
    the week that just finished."""
    today = today or date.today()
    year = today.year
    season_start = date(year, 9, 9)
    if today < season_start:
        year -= 1
        season_start = date(year, 9, 9)
    date_week = max(1, min(18, (today - season_start).days // 7 + 1))

    if refresh_schedule:
        try:
            nflverse_games.ingest([year - 1, year])
        except Exception as ex:
            print(f"Schedule refresh failed ({ex}); detecting the week from what's already in the DB")
    try:
        with connect() as con:
            rows = con.execute(
                """SELECT week, SUM(home_score IS NULL) AS unplayed FROM games
                   WHERE season=? AND game_type='REG' GROUP BY week ORDER BY week""",
                (year,),
            ).fetchall()
    except Exception:
        rows = []

    for r in rows:
        if r["unplayed"] > 0:
            return year, r["week"]
    if rows:
        return year, min(18, rows[-1]["week"] + 1)
    return year, date_week


def _fit_coefficient(seasons: list[int]) -> tuple[float, int]:
    pairs = training_pairs(seasons)
    return (fit_coefficient(pairs) if pairs else DEFAULT_COEFFICIENT), len(pairs)


def backfill(seasons: list[int]) -> None:
    """Note: safe to call in separate batches across process runs (e.g. 2015-16,
    then 2017-18, ...) -- prior_scores is seeded from whatever team_scores
    already exists in the DB for the season before this batch, not assumed empty,
    so the early-season team-level blend still has real data even when a batch
    doesn't include its own prior season."""
    print(f"Backfilling seasons {seasons}...")
    nflverse_games.ingest(seasons)
    prior_scores: dict[str, float] = team_score.prior_exit_scores(min(seasons) - 1)
    for season in sorted(seasons):
        n_players, n_rows = nflverse_players.ingest_season(season)
        if n_rows > 0:
            player_grades.grade_season(season)
            n_blend = player_grades.blend_prior_season(season)
            position_groups.roll_up_season(season)
            team_score.compute_season(season, prior_season_scores=prior_scores)
            print(f"  {season}: player-driven grading ({n_players} players, {n_rows} rows, "
                  f"{n_blend} rows blended with {season - 1})")
        else:
            team_score.compute_from_team_units_season(season, prior_season_scores=prior_scores)
            print(f"  {season}: player_stats/snap_counts not published yet -- used team-unit fallback")
        prior_scores = team_score.prior_exit_scores(season)
        # Project every week so the season lands in the backtest log. The
        # coefficient is fit on the PRIOR season only -- fitting on the season
        # being backfilled would let its own results leak into its projections.
        coefficient, n_pairs = _fit_coefficient([season - 1])
        n_projected = sum(project_week(season, w, coefficient=coefficient) for w in range(1, 19))
        n_logged = log_season(season)
        print(f"  {season}: projected {n_projected} games (coefficient {coefficient:.3f} from "
              f"{n_pairs} {season - 1} games), {n_logged} logged to backtest")


def weekly_run(season: int, week: int, skip_scrape: bool = False) -> Path:
    if not skip_scrape:
        print("Refreshing schedule/lines/refs/weather/injuries/players...")
        nflverse_games.ingest([season - 1, season])
        n_players, n_rows = nflverse_players.ingest_season(season)
        if n_rows > 0:
            player_grades.grade_season(season)
            player_grades.blend_prior_season(season)
            position_groups.roll_up_season(season)
        weather.refresh_week(season, week)
        referees.refresh_week(season, week)
        dk_lines.refresh_week(season, week)
        kalshi.refresh_week(season, week)
        injuries.refresh()

    prior_scores = team_score.prior_exit_scores(season - 1)
    with connect() as con:
        has_groups = con.execute(
            "SELECT 1 FROM position_group_scores WHERE season=? LIMIT 1", (season,)
        ).fetchone() is not None
    if has_groups:
        team_score.compute_season(season, prior_season_scores=prior_scores)
    else:
        team_score.compute_from_team_units_season(season, prior_season_scores=prior_scores)

    coefficient, n_pairs = _fit_coefficient([season - 1, season])
    if not n_pairs:
        coefficient, n_pairs = _fit_coefficient([season - 2, season - 1])
    print(f"Base model coefficient: {coefficient:.4f} ({n_pairs} training games)")

    project_week(season, week, coefficient=coefficient)
    log_season(season)
    return render_week(season, week)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NFL 2.0 weekly pipeline")
    parser.add_argument("--week", type=int, help="Week number to project/render (default: auto-detected from today's date)")
    parser.add_argument("--season", type=int, help="Season year (default: auto-detected from today's date)")
    parser.add_argument("--ingest-seasons", type=str, help="Comma-separated seasons to backfill, e.g. 2023,2024,2025")
    parser.add_argument("--skip-scrape", action="store_true", help="Skip network refresh, just re-grade/project/render")
    parser.add_argument("--no-open", action="store_true", help="Don't open the dashboard in a browser (for scheduled/background runs)")
    parser.add_argument("--no-publish", action="store_true", help="Don't commit + push the dashboard to the Streamlit site")
    args = parser.parse_args()

    init_db()

    if args.ingest_seasons:
        backfill([int(s) for s in args.ingest_seasons.split(",")])

    if args.week or not args.ingest_seasons:
        season, week = args.season, args.week
        if season is None or week is None:
            auto_season, auto_week = current_season_week(refresh_schedule=not args.skip_scrape)
            season, week = season or auto_season, week or auto_week
        path = weekly_run(season, week, skip_scrape=args.skip_scrape)
        print(f"Dashboard ready: {path}")
        if not args.no_publish:
            publish_dashboard(season, week)
        if not args.no_open:
            import webbrowser
            webbrowser.open(path.as_uri())
