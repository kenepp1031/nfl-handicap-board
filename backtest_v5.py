"""v5 backtest: does looking further back than one prior season actually make the
offense/defense EPA rating better, or does roster turnover (trades, the draft, coaching
changes) wash out anything older than last season?

Same offense-EPA + rest + div-shrink model as v3/v4's best variant. The only thing that
changes between variants is how the "prior" rating is built:
  - prior1  : current live app -- last season only (see grades() in the main app)
  - prior3  : weighted blend of the last 3 seasons (0.6 / 0.3 / 0.1)
  - prior5  : weighted blend of the last 5 seasons (0.45 / 0.25 / 0.15 / 0.10 / 0.05)

Run:  python backtest_v5.py
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
PRIOR_PSEUDO_PLAYS = 250
REST_WEIGHT = 0.10
DIV_SHRINK = 0.15

WEIGHTS_BY_VARIANT = {
    "prior1": [1.0],
    "prior3": [0.6, 0.3, 0.1],
    "prior5": [0.45, 0.25, 0.15, 0.10, 0.05],
}


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
        season = int(row["season"])
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
    out = {}
    for team, weeks in per_team_week.items():
        total_plays = sum(p for _, p in weeks.values())
        if total_plays <= 0:
            continue
        out[team] = sum(o * p for o, p in weeks.values()) / total_plays
    return out


def blended_prior(all_stats, season, weights):
    """team -> weighted average of full-season ratings from the `len(weights)` seasons
    immediately before `season`, most-recent-first. A team missing an older season
    (didn't exist under that name, or file gap) just gets its weight renormalized
    across whatever seasons it does have."""
    per_season_ratings = []
    for i in range(len(weights)):
        yr = season - 1 - i
        if yr in all_stats:
            per_season_ratings.append(full_season_rating(all_stats[yr]))
        else:
            per_season_ratings.append({})
    teams = set()
    for r in per_season_ratings:
        teams |= r.keys()
    out = {}
    for team in teams:
        num = 0.0; den = 0.0
        for w, r in zip(weights, per_season_ratings):
            if team in r:
                num += w * r[team]; den += w
        if den > 0:
            out[team] = num / den
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


def run_season(season, all_schedules, all_stats, weights):
    games = all_schedules[season]
    per_team_week = all_stats[season]
    prior_rating = blended_prior(all_stats, season, weights)

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

    for g in games:
        z = rating_by_week.get(g["week"], {})
        if g["home"] not in z or g["away"] not in z:
            continue
        rating_margin = (z[g["home"]] - z[g["away"]]) * RATING_SCALE + HOME_FIELD
        if g["home_rest"] is not None and g["away_rest"] is not None:
            rating_margin += REST_WEIGHT * (g["home_rest"] - g["away_rest"])
        if g["div_game"]:
            rating_margin *= (1 - DIV_SHRINK)
        edge = g["spread_line"] - rating_margin
        power = clamp(edge * (2 / 3), -2, 2)
        score = clamp(5 + power, 1, 10)
        diff = score - 5
        if abs(diff) < PASS_THRESHOLD:
            continue
        pick_side = "away" if power > 0 else "home"
        tier = "LARGE LEAN" if abs(diff) >= 3 else "LEAN"

        home_margin = g["home_score"] - g["away_score"]
        result = home_margin - g["spread_line"]
        outcome = "PUSH" if result == 0 else ("WON" if (("home" if result > 0 else "away") == pick_side) else "LOST")
        tiers[tier].append(outcome)

    return tiers


def combined(tiers_by_season):
    w = l = p = 0
    for tiers in tiers_by_season:
        for tier in ("LEAN", "LARGE LEAN"):
            w += tiers[tier].count("WON"); l += tiers[tier].count("LOST"); p += tiers[tier].count("PUSH")
    d = w + l
    pct = w / d * 100 if d else float("nan")
    return w, l, p, pct


def run_variant(name, weights, all_schedules, all_stats):
    tiers_by_season = [run_season(season, all_schedules, all_stats, weights) for season in SEASONS]
    w, l, p, pct = combined(tiers_by_season)
    print(f"{name:<12} n={w+l+p:<5} W-L-P {w}-{l}-{p:<4} ATS%={pct:.1f}%")
    for season, tiers in zip(SEASONS, tiers_by_season):
        sw = sum(tiers[t].count("WON") for t in ("LEAN", "LARGE LEAN"))
        sl = sum(tiers[t].count("LOST") for t in ("LEAN", "LARGE LEAN"))
        sd = sw + sl
        spct = sw / sd * 100 if sd else float("nan")
        print(f"    {season}  {sw}-{sl}  ATS%={spct:.1f}%")
    return pct


def main():
    all_schedules = load_schedule_all()
    all_stats = {}
    for season in range(2015, 2026):
        all_stats[season] = load_team_stats(season)

    results = {}
    for name, weights in WEIGHTS_BY_VARIANT.items():
        results[name] = run_variant(name, weights, all_schedules, all_stats)
        print()

    print("Summary (ATS%, 2020-2025 combined):")
    for name, pct in results.items():
        print(f"  {name}: {pct:.1f}%")


if __name__ == "__main__":
    main()
