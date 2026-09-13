"""Backtest: how often would this app's edge-vs-market logic have covered the
spread across every NFL regular season from 2020 through the present, using
only data that genuinely existed at each point in each season (no lookahead).

Same core logic as backtest_2025.py, generalized across seasons:
  - Downloads the real schedule, final scores, and closing spread_line for
    every game (nflverse games.csv), for SEASONS below.
  - Rebuilds a rolling team power rating week-by-week from each team's
    offensive EPA-per-play in games played *before* that week (nflverse
    stats_team_week_<season>.csv).
  - Reuses the app's edge -> confidence -> pick -> grade shape:
        projected_spread = rating_margin        # positive = home favored
        edge             = spread_line - projected_spread
        power            = clamp(edge * 2/3, -2, 2)
        score            = clamp(5 + power, 1, 10)
        pick             = away if power > 0 else home, only if |score-5| >= 1
        grade            = WON/LOST/PUSH from home_margin - spread_line
  - Skips the line-movement and recent-form terms: neither has a legitimate
    historical source here. Treat this as a test of the "projected margin vs.
    market spread" core, not the full live model.

Run:  python backtest_multi.py
Output: printed per-season + combined summary, stats_cache/backtest_multi_detail.csv
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

HOME_FIELD = 1.5          # same constant as rating_margin() in the app
RATING_SCALE = 1.15       # same per-point-of-rating multiplier as rating_margin()
MIN_GAMES_FOR_RATING = 2  # need at least this many prior games to trust a team's rating this week
PASS_THRESHOLD = 1.0      # |score-5| must be >= this to count as a pick, matches confidence_label()


def fetch_text(url: str) -> str:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urlopen(req, timeout=30).read().decode("utf-8")


def load_schedule(season: int):
    if GAMES_CACHE.exists():
        text = GAMES_CACHE.read_text(encoding="utf-8")
    else:
        text = fetch_text(GAMES_URL)
        GAMES_CACHE.write_text(text, encoding="utf-8")
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        if row["season"] != str(season) or row["game_type"] != "REG":
            continue
        if not row["home_score"] or not row["away_score"] or not row["spread_line"]:
            continue
        rows.append({
            "week": int(row["week"]),
            "home": row["home_team"], "away": row["away_team"],
            "home_score": int(row["home_score"]), "away_score": int(row["away_score"]),
            "spread_line": float(row["spread_line"]),
        })
    rows.sort(key=lambda r: r["week"])
    return rows


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


def rolling_off_rating(per_team_week, team, before_week):
    games = [v for wk, v in per_team_week.get(team, {}).items() if wk < before_week]
    if len(games) < MIN_GAMES_FOR_RATING:
        return None
    total_plays = sum(p for _, p in games)
    if total_plays <= 0:
        return None
    return sum(off * p for off, p in games) / total_plays


def zscore_week(ratings: dict):
    values = list(ratings.values())
    if len(values) < 8:
        return {}
    mean = stats.mean(values); sd = stats.pstdev(values) or 1e-9
    return {team: (v - mean) / sd for team, v in ratings.items()}


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def run_season(season: int, detail_rows: list):
    games = load_schedule(season)
    per_team_week = load_team_stats(season)

    weeks = sorted({g["week"] for g in games})
    zrating_by_week = {}
    for wk in weeks:
        raw = {}
        for team in per_team_week:
            r = rolling_off_rating(per_team_week, team, wk)
            if r is not None:
                raw[team] = r
        zrating_by_week[wk] = zscore_week(raw)

    tiers = {"LEAN": [], "LARGE LEAN": []}
    skipped_no_rating = skipped_pass = 0

    for g in games:
        z = zrating_by_week.get(g["week"], {})
        if g["home"] not in z or g["away"] not in z:
            skipped_no_rating += 1
            continue
        rating_margin = (z[g["home"]] - z[g["away"]]) * RATING_SCALE + HOME_FIELD
        projected_spread = rating_margin
        edge = g["spread_line"] - projected_spread
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
        if result == 0:
            outcome = "PUSH"
        else:
            winner = "home" if result > 0 else "away"
            outcome = "WON" if winner == pick_side else "LOST"

        tiers[tier].append(outcome)
        detail_rows.append({
            "season": season, "week": g["week"], "matchup": f"{g['away']} @ {g['home']}",
            "spread_line_home": g["spread_line"], "score": round(score, 2),
            "tier": tier, "pick": pick_side, "outcome": outcome,
            "final": f"{g['away_score']}-{g['home_score']}",
        })

    return games, tiers, skipped_no_rating, skipped_pass


def summarize(label, outcomes):
    wins = outcomes.count("WON"); losses = outcomes.count("LOST"); pushes = outcomes.count("PUSH")
    decided = wins + losses
    pct = (wins / decided * 100) if decided else float("nan")
    if decided:
        print(f"  {label:<12} picks={len(outcomes):<4} W-L-P {wins}-{losses}-{pushes:<3} ATS%={pct:5.1f}%")
    else:
        print(f"  {label:<12} picks=0")
    return wins, losses, pushes


def main():
    all_detail = []
    grand_w = grand_l = grand_p = 0
    for season in SEASONS:
        print(f"\n=== {season} REGULAR SEASON ===")
        games, tiers, skipped_no_rating, skipped_pass = run_season(season, all_detail)
        print(f"  ({len(games)} games with a closing line) skipped(no-rating)={skipped_no_rating} skipped(PASS)={skipped_pass}")
        sw = sl = sp = 0
        for tier in ("LEAN", "LARGE LEAN"):
            w, l, p = summarize(tier, tiers[tier])
            sw += w; sl += l; sp += p
        decided = sw + sl
        pct = (sw / decided * 100) if decided else float("nan")
        if decided:
            print(f"  {'SEASON':<12} picks={sw+sl+sp:<4} W-L-P {sw}-{sl}-{sp:<3} ATS%={pct:5.1f}%")
        grand_w += sw; grand_l += sl; grand_p += sp

    detail_path = CACHE_DIR / "backtest_multi_detail.csv"
    with detail_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(all_detail[0].keys()) if all_detail else
                            ["season", "week", "matchup", "spread_line_home", "score", "tier", "pick", "outcome", "final"])
        w.writeheader()
        w.writerows(all_detail)

    decided = grand_w + grand_l
    overall_pct = (grand_w / decided * 100) if decided else float("nan")
    print(f"\n=== COMBINED {SEASONS[0]}-{SEASONS[-1]} ===")
    print(f"  TOTAL picks={grand_w+grand_l+grand_p} W-L-P {grand_w}-{grand_l}-{grand_p} ATS%={overall_pct:.1f}%")
    print(f"\nDetail written to {detail_path}")


if __name__ == "__main__":
    main()
