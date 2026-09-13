"""v2 backtest: adds defensive EPA/play allowed (via opponent's offensive EPA
in the same game_id) on top of backtest_multi.py's offense-only rating, to
see whether an offense-minus-defense net rating covers the spread better.

Rating per team per week = zscore(rolling off_epa/play) - zscore(rolling
def_epa/play allowed), each side z-scored independently across the week's
teams before differencing. Same edge -> confidence -> pick -> grade shape,
same no-lookahead rolling window, same seasons.

Run:  python backtest_v2.py
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
    """Returns per_team_week[team][week] = (off_epa_per_play, def_epa_allowed_per_play, plays)."""
    cache = CACHE_DIR / f"team_{season}.csv"
    if cache.exists():
        text = cache.read_text(encoding="utf-8")
    else:
        text = fetch_text(STATS_URL_TMPL.format(season=season))
        cache.write_text(text, encoding="utf-8")

    # First pass: per (game_id, team) offensive epa/play, keyed so we can look
    # up "what did my opponent do against me" for the defensive side.
    game_team_off = {}  # (game_id, team) -> (off_epa_per_play, plays, week, opponent)
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("season") != str(season) or row.get("season_type") != "REG":
            continue
        team = row["team"]; week = int(row["week"]); game_id = row["game_id"]; opp = row["opponent_team"]
        try:
            plays = float(row["attempts"] or 0) + float(row["sacks_suffered"] or 0) + float(row["carries"] or 0)
            pass_epa = float(row["passing_epa"] or 0); rush_epa = float(row["rushing_epa"] or 0)
        except ValueError:
            continue
        if plays <= 0:
            continue
        off = (pass_epa + rush_epa) / plays
        game_team_off[(game_id, team)] = (off, plays, week, opp)

    per_team_week = {}
    for (game_id, team), (off, plays, week, opp) in game_team_off.items():
        opp_entry = game_team_off.get((game_id, opp))
        def_allowed = opp_entry[0] if opp_entry else None  # opponent's offensive epa/play == what this team's defense allowed
        per_team_week.setdefault(team, {})[week] = (off, def_allowed, plays)
    return per_team_week


def rolling_rating(per_team_week, team, before_week):
    off_games = [(o, p) for wk, (o, d, p) in per_team_week.get(team, {}).items() if wk < before_week]
    def_games = [(d, p) for wk, (o, d, p) in per_team_week.get(team, {}).items() if wk < before_week and d is not None]
    if len(off_games) < MIN_GAMES_FOR_RATING:
        return None
    total_plays = sum(p for _, p in off_games)
    if total_plays <= 0:
        return None
    off_rating = sum(o * p for o, p in off_games) / total_plays
    if len(def_games) < MIN_GAMES_FOR_RATING:
        def_rating = None
    else:
        dp = sum(p for _, p in def_games)
        def_rating = sum(d * p for d, p in def_games) / dp if dp > 0 else None
    return off_rating, def_rating


def zscore_dict(values: dict):
    vals = list(values.values())
    if len(vals) < 8:
        return {}
    mean = stats.mean(vals); sd = stats.pstdev(vals) or 1e-9
    return {k: (v - mean) / sd for k, v in values.items()}


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def net_rating_by_week(per_team_week, weeks):
    result = {}
    for wk in weeks:
        off_raw, def_raw = {}, {}
        for team in per_team_week:
            r = rolling_rating(per_team_week, team, wk)
            if r is None:
                continue
            off_rating, def_rating = r
            off_raw[team] = off_rating
            if def_rating is not None:
                def_raw[team] = def_rating
        z_off = zscore_dict(off_raw)
        z_def = zscore_dict(def_raw)  # higher def_epa_allowed = worse defense, so subtract
        net = {}
        for team in z_off:
            d = z_def.get(team, 0.0)
            net[team] = z_off[team] - d
        result[wk] = net
    return result


def run_season(season: int, detail_rows: list):
    games = load_schedule(season)
    per_team_week = load_team_stats(season)
    weeks = sorted({g["week"] for g in games})
    net_by_week = net_rating_by_week(per_team_week, weeks)

    tiers = {"LEAN": [], "LARGE LEAN": []}
    skipped_no_rating = skipped_pass = 0

    for g in games:
        z = net_by_week.get(g["week"], {})
        if g["home"] not in z or g["away"] not in z:
            skipped_no_rating += 1
            continue
        rating_margin = (z[g["home"]] - z[g["away"]]) * RATING_SCALE + HOME_FIELD
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
        detail_rows.append({"season": season, "week": g["week"], "matchup": f"{g['away']} @ {g['home']}",
                             "spread_line_home": g["spread_line"], "score": round(score, 2),
                             "tier": tier, "pick": pick_side, "outcome": outcome,
                             "final": f"{g['away_score']}-{g['home_score']}"})
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
        print(f"\n=== {season} ===")
        games, tiers, skipped_no_rating, skipped_pass = run_season(season, all_detail)
        print(f"  ({len(games)} games) skipped(no-rating)={skipped_no_rating} skipped(PASS)={skipped_pass}")
        sw = sl = sp = 0
        for tier in ("LEAN", "LARGE LEAN"):
            w, l, p = summarize(tier, tiers[tier])
            sw += w; sl += l; sp += p
        decided = sw + sl
        pct = (sw / decided * 100) if decided else float("nan")
        if decided:
            print(f"  {'SEASON':<12} picks={sw+sl+sp:<4} W-L-P {sw}-{sl}-{sp:<3} ATS%={pct:5.1f}%")
        grand_w += sw; grand_l += sl; grand_p += sp

    detail_path = CACHE_DIR / "backtest_v2_detail.csv"
    with detail_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(all_detail[0].keys()) if all_detail else
                            ["season", "week", "matchup", "spread_line_home", "score", "tier", "pick", "outcome", "final"])
        w.writeheader()
        w.writerows(all_detail)

    decided = grand_w + grand_l
    overall_pct = (grand_w / decided * 100) if decided else float("nan")
    print(f"\n=== COMBINED {SEASONS[0]}-{SEASONS[-1]} ===")
    print(f"  TOTAL picks={grand_w+grand_l+grand_p} W-L-P {grand_w}-{grand_l}-{grand_p} ATS%={overall_pct:.1f}%")


if __name__ == "__main__":
    main()
