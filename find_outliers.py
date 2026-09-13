"""Find the games where the model's projected margin missed actual results by the
widest margin, and check whether "way off" games have gotten more common since
legalized mobile sports betting scaled up (~2021-2023) -- a fair thing to check
empirically rather than assume."""
from __future__ import annotations
import csv, io, statistics as stats
from pathlib import Path
from urllib.request import Request, urlopen

APP_DIR = Path(__file__).parent
CACHE_DIR = APP_DIR / "stats_cache"
SEASONS = list(range(2015, 2026))
GAMES_CACHE = CACHE_DIR / "games_all.csv"
STATS_URL_TMPL = "https://github.com/nflverse/nflverse-data/releases/download/stats_team/stats_team_week_{season}.csv"

HOME_FIELD = 1.5
RATING_SCALE = 1.15
MIN_GAMES_FOR_RATING = 2
PRIOR_PSEUDO_PLAYS = 250
REST_WEIGHT = 0.10
DIV_SHRINK = 0.15

def fetch_text(url):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urlopen(req, timeout=30).read().decode("utf-8")

def load_schedule_all():
    text = GAMES_CACHE.read_text(encoding="utf-8")
    by_season = {}
    for row in csv.DictReader(io.StringIO(text)):
        season = int(row["season"])
        if row["game_type"] != "REG": continue
        if not row["home_score"] or not row["away_score"] or not row["spread_line"]: continue
        by_season.setdefault(season, []).append({
            "week": int(row["week"]), "home": row["home_team"], "away": row["away_team"],
            "home_score": int(row["home_score"]), "away_score": int(row["away_score"]),
            "spread_line": float(row["spread_line"]),
            "home_rest": float(row["home_rest"]) if row["home_rest"] else None,
            "away_rest": float(row["away_rest"]) if row["away_rest"] else None,
            "div_game": row["div_game"] == "1",
        })
    for s in by_season: by_season[s].sort(key=lambda r: r["week"])
    return by_season

def load_team_stats(season):
    cache = CACHE_DIR / f"team_{season}.csv"
    if cache.exists():
        text = cache.read_text(encoding="utf-8")
    else:
        text = fetch_text(STATS_URL_TMPL.format(season=season))
        cache.write_text(text, encoding="utf-8")
    per_team_week = {}
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("season") != str(season) or row.get("season_type") != "REG": continue
        team = row["team"]; week = int(row["week"])
        try:
            plays = float(row["attempts"] or 0) + float(row["sacks_suffered"] or 0) + float(row["carries"] or 0)
            pass_epa = float(row["passing_epa"] or 0); rush_epa = float(row["rushing_epa"] or 0)
        except ValueError:
            continue
        if plays <= 0: continue
        off = (pass_epa + rush_epa) / plays
        per_team_week.setdefault(team, {})[week] = (off, plays)
    return per_team_week

def full_season_rating(per_team_week):
    out = {}
    for team, weeks in per_team_week.items():
        total_plays = sum(p for _, p in weeks.values())
        if total_plays <= 0: continue
        out[team] = sum(o * p for o, p in weeks.values()) / total_plays
    return out

def rolling_rating_blended(per_team_week, team, before_week, prior_rating):
    games = [v for wk, v in per_team_week.get(team, {}).items() if wk < before_week]
    total_plays = sum(p for _, p in games)
    prior = prior_rating.get(team)
    if prior is None:
        if len(games) < MIN_GAMES_FOR_RATING or total_plays <= 0: return None
        return sum(o * p for o, p in games) / total_plays
    current_sum = sum(o * p for o, p in games)
    return (current_sum + PRIOR_PSEUDO_PLAYS * prior) / (total_plays + PRIOR_PSEUDO_PLAYS)

def zscore_dict(values):
    vals = list(values.values())
    if len(vals) < 8: return {}
    mean = stats.mean(vals); sd = stats.pstdev(vals) or 1e-9
    return {k: (v - mean) / sd for k, v in values.items()}

def clamp(v, lo, hi): return max(lo, min(hi, v))

def main():
    all_schedules = load_schedule_all()
    all_stats = {s: load_team_stats(s) for s in SEASONS}

    misses = []
    for season in range(2020, 2026):
        games = all_schedules[season]
        per_team_week = all_stats[season]
        prior_rating = full_season_rating(all_stats[season - 1]) if (season - 1) in all_stats else {}
        weeks = sorted({g["week"] for g in games})
        rating_by_week = {}
        for wk in weeks:
            raw = {}
            for team in per_team_week:
                r = rolling_rating_blended(per_team_week, team, wk, prior_rating)
                if r is not None: raw[team] = r
            rating_by_week[wk] = zscore_dict(raw)
        for g in games:
            z = rating_by_week.get(g["week"], {})
            if g["home"] not in z or g["away"] not in z: continue
            rating_margin = (z[g["home"]] - z[g["away"]]) * RATING_SCALE + HOME_FIELD
            if g["home_rest"] is not None and g["away_rest"] is not None:
                rating_margin += REST_WEIGHT * (g["home_rest"] - g["away_rest"])
            if g["div_game"]: rating_margin *= (1 - DIV_SHRINK)
            projected_spread = -rating_margin
            actual_margin = g["home_score"] - g["away_score"]
            ats_miss = abs((actual_margin - g["spread_line"]) - 0)  # how far off the CLOSING spread the game landed, for context
            model_miss = abs(actual_margin - rating_margin)  # how far off the model's own projected margin the actual result landed
            misses.append(dict(season=season, week=g["week"], home=g["home"], away=g["away"],
                                home_score=g["home_score"], away_score=g["away_score"],
                                spread_line=g["spread_line"], projected_spread=projected_spread,
                                model_miss=model_miss))

    misses.sort(key=lambda m: -m["model_miss"])
    print("TOP 25 GAMES WHERE THE MODEL WAS FURTHEST OFF (actual margin vs model's own projected margin):\n")
    for m in misses[:25]:
        print(f"{m['season']} wk{m['week']:<2} {m['away']:>4} @ {m['home']:<4}  final {m['away_score']}-{m['home_score']}  "
              f"closing line {m['home']} {m['spread_line']:+.1f}  model projected {m['home']} {m['projected_spread']:+.1f}  "
              f"miss={m['model_miss']:.1f} pts")

    print("\nMiss-size distribution by season (average |model miss|, and share of games missed by 21+ points):")
    for season in range(2020, 2026):
        s = [m for m in misses if m["season"] == season]
        avg = sum(m["model_miss"] for m in s) / len(s)
        blowout_share = sum(1 for m in s if m["model_miss"] >= 21) / len(s) * 100
        print(f"  {season}: n={len(s):<4} avg_miss={avg:.1f}  pct_missed_by_21pt+={blowout_share:.1f}%")

if __name__ == "__main__":
    main()
