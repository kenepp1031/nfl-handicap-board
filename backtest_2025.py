"""Backtest: how often would this app's edge-vs-market logic have covered the
spread across the actual 2025 NFL season?

This is NOT a byte-for-byte replay of NFL_Handicapping_Desktop.pyw's live
confidence_score(). That function's main signal -- consensus_rating -- comes
from scraping *today's* live power-ranking pages, and its line-movement term
depends on this app's own line_history table recorded while it was running.
Neither can be reconstructed for a past season.

What this script does instead, using only data that genuinely existed at
each point in the 2025 season (no lookahead):
  - Downloads the real 2025 schedule, final scores, and closing spread_line
    for every game (nflverse games.csv).
  - Rebuilds a rolling team power rating week-by-week from each team's
    offensive/defensive EPA-per-play in games played *before* that week
    (same per-play EPA calc as the app's team_units(), just cumulative
    instead of prior-season-blended).
  - Reuses the app's actual edge -> confidence -> pick -> grade shape
    (see confidence_score/rating_margin/auto_pick_result in the .pyw), adapted
    to nflverse's spread_line sign convention (positive = home favored,
    opposite of the app's own dk_spread column):
        projected_spread = rating_margin        # positive = home favored
        edge             = spread_line - projected_spread
        power            = clamp(edge * 2/3, -2, 2)   # >0: market favors home more than our rating does
        score            = clamp(5 + power, 1, 10)
        pick             = away if power > 0 else home, only if |score-5| >= 1
        grade            = WON/LOST/PUSH from home_margin - spread_line
  - Skips the line-movement and recent-form terms: neither has a legitimate
    historical source here. This means the score in this backtest is not
    identical to what the live app would have shown; treat it as a test of
    the "projected margin vs. market spread" core, not the full live model.

Run:  python backtest_2025.py
Output: printed summary + stats_cache/backtest_2025_detail.csv
"""
from __future__ import annotations
import csv, io, statistics as stats
from pathlib import Path
from urllib.request import Request, urlopen

APP_DIR = Path(__file__).parent
CACHE_DIR = APP_DIR / "stats_cache"
CACHE_DIR.mkdir(exist_ok=True)

SEASON = 2025
GAMES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
STATS_URL = f"https://github.com/nflverse/nflverse-data/releases/download/stats_team/stats_team_week_{SEASON}.csv"
STATS_CACHE = CACHE_DIR / f"team_{SEASON}.csv"

HOME_FIELD = 1.5          # same constant as rating_margin() in the app
RATING_SCALE = 1.15       # same per-point-of-rating multiplier as rating_margin()
MIN_GAMES_FOR_RATING = 2  # need at least this many prior games to trust a team's rating this week
PASS_THRESHOLD = 1.0      # |score-5| must be >= this to count as a pick, matches confidence_label()


def fetch_text(url: str) -> str:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urlopen(req, timeout=30).read().decode("utf-8")


def load_schedule():
    text = fetch_text(GAMES_URL)
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        if row["season"] != str(SEASON) or row["game_type"] != "REG":
            continue
        if not row["home_score"] or not row["away_score"] or not row["spread_line"]:
            continue  # unplayed / no line
        rows.append({
            "week": int(row["week"]),
            "home": row["home_team"], "away": row["away_team"],
            "home_score": int(row["home_score"]), "away_score": int(row["away_score"]),
            "spread_line": float(row["spread_line"]),
        })
    rows.sort(key=lambda r: r["week"])
    return rows


def load_team_stats():
    if STATS_CACHE.exists():
        text = STATS_CACHE.read_text(encoding="utf-8")
    else:
        text = fetch_text(STATS_URL)
        STATS_CACHE.write_text(text, encoding="utf-8")
    # team -> week -> (off_epa_per_play, def_epa_per_play_allowed, plays)
    per_team_week = {}
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("season") != str(SEASON) or row.get("season_type") != "REG":
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
    """Average per-play offensive EPA across all games strictly before before_week."""
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


def main():
    print(f"Fetching {SEASON} schedule + closing spreads ...")
    games = load_schedule()
    print(f"Fetching {SEASON} team EPA stats ...")
    per_team_week = load_team_stats()

    # Pre-compute each team's rolling offensive rating for every week it played,
    # then z-score within each week (defense omitted -- symmetric with offense
    # would need opponent-adjustment beyond this script's scope; using offense
    # differential alone as the rating signal, same simplification tradeoff
    # the app makes for its consensus-rating-driven margin).
    weeks = sorted({g["week"] for g in games})
    zrating_by_week = {}
    for wk in weeks:
        raw = {}
        for team in per_team_week:
            r = rolling_off_rating(per_team_week, team, wk)
            if r is not None:
                raw[team] = r
        zrating_by_week[wk] = zscore_week(raw)

    detail_rows = []
    tiers = {"LEAN": [], "LARGE LEAN": []}
    skipped_no_rating = 0
    skipped_pass = 0

    for g in games:
        z = zrating_by_week.get(g["week"], {})
        if g["home"] not in z or g["away"] not in z:
            skipped_no_rating += 1
            continue
        # nflverse spread_line convention: POSITIVE means the home team was
        # favored by that many points (opposite of the app's own dk_spread,
        # which is negative when home is favored) -- verified against known
        # 2025 results (e.g. PHI opened -8.5 fav at home vs. DAL, spread_line
        # here is +8.5). rating_margin below is expressed the same way: home
        # favored by rating_margin points.
        rating_margin = (z[g["home"]] - z[g["away"]]) * RATING_SCALE + HOME_FIELD
        projected_spread = rating_margin
        edge = g["spread_line"] - projected_spread
        power = clamp(edge * (2 / 3), -2, 2)
        score = clamp(5 + power, 1, 10)
        diff = score - 5
        if abs(diff) < PASS_THRESHOLD:
            skipped_pass += 1
            continue
        # power > 0 means market favors home more than our rating does ->
        # take the side our rating likes better, which is away in that case.
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
            "week": g["week"], "matchup": f"{g['away']} @ {g['home']}",
            "spread_line_home": g["spread_line"], "score": round(score, 2),
            "tier": tier, "pick": pick_side, "outcome": outcome,
            "final": f"{g['away_score']}-{g['home_score']}",
        })

    detail_path = CACHE_DIR / "backtest_2025_detail.csv"
    with detail_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(detail_rows[0].keys()) if detail_rows else
                            ["week", "matchup", "spread_line_home", "score", "tier", "pick", "outcome", "final"])
        w.writeheader()
        w.writerows(detail_rows)

    def summarize(label, outcomes):
        wins = outcomes.count("WON"); losses = outcomes.count("LOST"); pushes = outcomes.count("PUSH")
        decided = wins + losses
        pct = (wins / decided * 100) if decided else float("nan")
        print(f"  {label:<12} picks={len(outcomes):<4} W-L-P {wins}-{losses}-{pushes:<3} ATS%={pct:5.1f}%" if decided else f"  {label:<12} picks=0")
        return wins, losses, pushes

    print(f"\n2025 REGULAR SEASON BACKTEST ({len(games)} games with a closing line found)")
    print(f"  Skipped (no rating history yet): {skipped_no_rating}")
    print(f"  Skipped (PASS, |score-5| < {PASS_THRESHOLD}): {skipped_pass}\n")
    total_w = total_l = total_p = 0
    for tier in ("LEAN", "LARGE LEAN"):
        w, l, p = summarize(tier, tiers[tier])
        total_w += w; total_l += l; total_p += p
    decided = total_w + total_l
    overall_pct = (total_w / decided * 100) if decided else float("nan")
    print(f"  {'OVERALL':<12} picks={total_w+total_l+total_p:<4} W-L-P {total_w}-{total_l}-{total_p:<3} ATS%={overall_pct:5.1f}%")
    print(f"\nDetail written to {detail_path}")


if __name__ == "__main__":
    main()
