"""v3 backtest: offense-only EPA rating (v1 was best of v1/v2) plus a
prior-season blend to fix early-season noise, plus optional rest-day and
divisional-game terms. Each addition is toggleable so we can measure its
isolated effect out-of-sample.

Run:  python backtest_v3.py
"""
from __future__ import annotations
import csv, io, statistics as stats
from pathlib import Path
from urllib.request import Request, urlopen

APP_DIR = Path(__file__).parent
CACHE_DIR = APP_DIR / "stats_cache"
CACHE_DIR.mkdir(exist_ok=True)

SEASONS = [2020, 2021, 2022, 2023, 2024, 2025]
GAMES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
GAMES_CACHE = CACHE_DIR / "games_all.csv"
STATS_URL_TMPL = "https://github.com/nflverse/nflverse-data/releases/download/stats_team/stats_team_week_{season}.csv"

HOME_FIELD = 1.5
RATING_SCALE = 1.15
MIN_GAMES_FOR_RATING = 2
PASS_THRESHOLD = 1.0
PRIOR_PSEUDO_PLAYS = 250   # ~ 4 games worth of plays, weight given to prior-season rating early on
REST_WEIGHT = 0.0          # points of margin per day of rest advantage (set below in sweep)
DIV_SHRINK = 0.0           # fraction to shrink projected margin toward 0 in division games


def fetch_text(url: str) -> str:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urlopen(req, timeout=30).read().decode("utf-8")


def load_schedule_all():
    if GAMES_CACHE.exists():
        text = GAMES_CACHE.read_text(encoding="utf-8")
    else:
        text = fetch_text(GAMES_URL)
        GAMES_CACHE.write_text(text, encoding="utf-8")
    by_season = {}
    for row in csv.DictReader(io.StringIO(text)):
        season = row["season"]
        if row["game_type"] != "REG":
            continue
        if not row["home_score"] or not row["away_score"] or not row["spread_line"]:
            continue
        by_season.setdefault(season, []).append({
            "week": int(row["week"]),
            "home": row["home_team"], "away": row["away_team"],
            "home_score": int(row["home_score"]), "away_score": int(row["away_score"]),
            "spread_line": float(row["spread_line"]),
            "home_rest": float(row["home_rest"]) if row["home_rest"] else None,
            "away_rest": float(row["away_rest"]) if row["away_rest"] else None,
            "div_game": row["div_game"] == "1",
        })
    for s in by_season:
        by_season[s].sort(key=lambda r: r["week"])
    return by_season


def load_team_stats(season: int):
    cache = CACHE_DIR / f"team_{season}.csv"
    if cache.exists():
        text = cache.read_text(encoding="utf-8")
    else:
        text = fetch_text(STATS_URL_TMPL.format(season=season))
        cache.write_text(text, encoding="utf-8")
    per_team_week = {}
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("season") != str(season) or row.get("season_type") != "REG":
            continue
        team = row["team"]; week = int(row["week"])
        try:
            plays = float(row["attempts"] or 0) + float(row["sacks_suffered"] or 0) + float(row["carries"] or 0)
            pass_epa = float(row["passing_epa"] or 0); rush_epa = float(row["rushing_epa"] or 0)
        except ValueError:
            continue
        if plays <= 0:
            continue
        off = (pass_epa + rush_epa) / plays
        per_team_week.setdefault(team, {})[week] = (off, plays)
    return per_team_week


def full_season_rating(per_team_week):
    """team -> full-season play-weighted off EPA/play (used as prior for next season)."""
    out = {}
    for team, weeks in per_team_week.items():
        total_plays = sum(p for _, p in weeks.values())
        if total_plays <= 0:
            continue
        out[team] = sum(o * p for o, p in weeks.values()) / total_plays
    return out


def rolling_rating_blended(per_team_week, team, before_week, prior_rating):
    games = [v for wk, v in per_team_week.get(team, {}).items() if wk < before_week]
    total_plays = sum(p for _, p in games)
    prior = prior_rating.get(team)
    if prior is None:
        if len(games) < MIN_GAMES_FOR_RATING or total_plays <= 0:
            return None
        return sum(o * p for o, p in games) / total_plays
    current_sum = sum(o * p for o, p in games)
    return (current_sum + PRIOR_PSEUDO_PLAYS * prior) / (total_plays + PRIOR_PSEUDO_PLAYS)


def zscore_dict(values: dict):
    vals = list(values.values())
    if len(vals) < 8:
        return {}
    mean = stats.mean(vals); sd = stats.pstdev(vals) or 1e-9
    return {k: (v - mean) / sd for k, v in values.items()}


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def run_season(season: int, all_schedules, all_stats, detail_rows, use_prior, rest_weight, div_shrink):
    games = all_schedules[str(season)]
    per_team_week = all_stats[season]
    prior_rating = full_season_rating(all_stats[season - 1]) if (use_prior and (season - 1) in all_stats) else {}

    weeks = sorted({g["week"] for g in games})
    rating_by_week = {}
    for wk in weeks:
        raw = {}
        for team in per_team_week:
            r = rolling_rating_blended(per_team_week, team, wk, prior_rating)
            if r is not None:
                raw[team] = r
        rating_by_week[wk] = zscore_dict(raw)

    tiers = {"LEAN": [], "LARGE LEAN": []}
    skipped_no_rating = skipped_pass = 0

    for g in games:
        z = rating_by_week.get(g["week"], {})
        if g["home"] not in z or g["away"] not in z:
            skipped_no_rating += 1
            continue
        rating_margin = (z[g["home"]] - z[g["away"]]) * RATING_SCALE + HOME_FIELD
        if rest_weight and g["home_rest"] is not None and g["away_rest"] is not None:
            rating_margin += rest_weight * (g["home_rest"] - g["away_rest"])
        if div_shrink and g["div_game"]:
            rating_margin *= (1 - div_shrink)
        edge = g["spread_line"] - rating_margin
        power = clamp(edge * (2 / 3), -2, 2)
        score = clamp(5 + power, 1, 10)
        diff = score - 5
        if abs(diff) < PASS_THRESHOLD:
            skipped_pass += 1
            continue
        pick_side = "away" if power > 0 else "home"
        tier = "LARGE LEAN" if abs(diff) >= 3 else "LEAN"

        home_margin = g["home_score"] - g["away_score"]
        result = home_margin - g["spread_line"]
        outcome = "PUSH" if result == 0 else ("WON" if (("home" if result > 0 else "away") == pick_side) else "LOST")
        tiers[tier].append(outcome)
        detail_rows.append({"season": season, "week": g["week"], "outcome": outcome})

    return games, tiers, skipped_no_rating, skipped_pass


def combined(tiers_by_season):
    w = l = p = 0
    for tiers in tiers_by_season:
        for tier in ("LEAN", "LARGE LEAN"):
            w += tiers[tier].count("WON"); l += tiers[tier].count("LOST"); p += tiers[tier].count("PUSH")
    d = w + l
    pct = w / d * 100 if d else float("nan")
    return w, l, p, pct


def run_variant(name, use_prior, rest_weight, div_shrink):
    all_schedules = load_schedule_all()
    all_stats = {}
    for season in [2019] + SEASONS:
        all_stats[season] = load_team_stats(season)

    detail = []
    tiers_by_season = []
    for season in SEASONS:
        _, tiers, _, _ = run_season(season, all_schedules, all_stats, detail, use_prior, rest_weight, div_shrink)
        tiers_by_season.append(tiers)
    w, l, p, pct = combined(tiers_by_season)
    print(f"{name:<40} n={w+l+p:<5} W-L-P {w}-{l}-{p:<4} ATS%={pct:.1f}%")
    return pct


def main():
    run_variant("baseline (offense-only, no prior)", False, 0.0, 0.0)
    run_variant("+ prior-season blend", True, 0.0, 0.0)
    run_variant("+ prior-season blend + rest(0.05/day)", True, 0.05, 0.0)
    run_variant("+ prior-season blend + rest(0.1/day)", True, 0.10, 0.0)
    run_variant("+ prior-season blend + div-shrink(15%)", True, 0.0, 0.15)
    run_variant("+ prior blend + rest(0.1) + div-shrink(15%)", True, 0.10, 0.15)


if __name__ == "__main__":
    main()
