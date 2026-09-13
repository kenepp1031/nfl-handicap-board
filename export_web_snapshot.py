"""Export a JSON snapshot of everything the desktop app's Week view, Power
Rankings view, and My Picks view compute -- for the read-only Streamlit web
companion (streamlit_app.py), which cannot run Tkinter on Streamlit Cloud.

Run this locally (same machine/folder as NFL_Handicapping_Desktop.pyw and
nfl_handicapping_board.db), then commit+push web_snapshot.json to redeploy
the public site:

    python export_web_snapshot.py
    git add web_snapshot.json
    git commit -m "Refresh web snapshot"
    git push

HOW THIS WORKS
---------------
NFL_Handicapping_Desktop.pyw is imported directly (it is just a normal Python
file with an unusual extension) to reuse its module-level constants and pure
functions (TEAMS, STADIUMS, VENUES, RIVALRIES, NOISE_ELITE/LOUD, RANKING_*,
team_units(), grades(), round_half(), weather_icon(), haversine_miles(), ...)
without any risk to the desktop app, since importing only runs top-level
module code -- everything that opens the Tk window, starts background
threads, or hits os.startfile() lives inside `Board.__init__` or under
`if __name__ == "__main__":`, neither of which import touches.

The *calculation* methods (rating_margin, automated_factors, projected_game,
confidence_score, confidence_label, auto_pick_note, injury_lines,
venue_label, days_rest, opening_line, pick_result, ou_result, referee_*,
_compute_top_plays, _compute_team_ranks, team_grade_values, grade_colors,
rating_color) are defined on the `Board` Tkinter widget itself, so they take
`self` (theme, current week, cached unit grades, live Tk state) rather than
plain arguments. Instantiating a real `Board` to reuse them would build the
whole GUI, spin up background network threads, and open a browser tab via
os.startfile -- exactly what this script must NOT do. Per the plan, those
methods are ported here as plain functions instead, with formulas and
thresholds copied verbatim from the .pyw. If the desktop app's algorithm
ever changes, THESE FUNCTIONS MUST BE UPDATED TO MATCH -- they are a
deliberate duplicate, not a shared import.
"""
from __future__ import annotations

import importlib.util
import io
import json
import math
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

APP_DIR = Path(__file__).resolve().parent
DESKTOP_PATH = APP_DIR / "NFL_Handicapping_Desktop.pyw"
DB_PATH = APP_DIR / "nfl_handicapping_board.db"
OUT_PATH = APP_DIR / "web_snapshot.json"
STATS_CACHE = APP_DIR / "stats_cache"


def _load_desktop_module():
    """Import the .pyw for its module-level constants/pure functions only.
    Safe: everything with a side effect in that file is guarded either
    inside Board.__init__ (never instantiated here) or under
    `if __name__ == "__main__":` (never true for an import)."""
    spec = importlib.util.spec_from_file_location("nfl_desktop_calc", DESKTOP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


D = _load_desktop_module()

# ---------------------------------------------------------------------------
# Small presentational duplicates of Board.grade_colors / Board.rating_color.
# These two are pure color tables with no state dependency other than
# light/dark theme, so both palettes are exported and the Streamlit side
# picks whichever matches the viewer's theme -- see NFL_Handicapping_Desktop.pyw
# lines ~948-1080 for the source of truth if the palette ever changes there.
# ---------------------------------------------------------------------------
GRADE_COLORS = {
    "A": {"light": ("#137333", "#e6f4ea"), "dark": ("#58d987", "#153b26")},
    "B": {"light": ("#47751b", "#eef5df"), "dark": ("#abd66f", "#2c3c1d")},
    "C": {"light": ("#805b00", "#fff4cf"), "dark": ("#f2d16b", "#413719")},
    "D": {"light": ("#ad4b00", "#ffeadb"), "dark": ("#ffad6c", "#482b19")},
    "F": {"light": ("#b4232c", "#fde7e9"), "dark": ("#ff7d85", "#4a2028")},
    "N/A": {"light": ("#61616b", "#ededf0"), "dark": ("#b6b6c0", "#303036")},
}
RATING_COLORS_LIGHT = {10: "#075f35", 9: "#008c4d", 8: "#00b85c", 7: "#38d467", 6: "#91da31",
                        5: "#d2c62a", 4: "#e9a51d", 3: "#e67618", 2: "#d94724", 1: "#ad1f2d"}
RATING_COLORS_DARK = {**RATING_COLORS_LIGHT, 10: "#1ea968", 1: "#e8465c"}


def rating_color(rating, theme):
    if rating is None:
        return None
    table = RATING_COLORS_DARK if theme == "dark" else RATING_COLORS_LIGHT
    return table[max(1, min(10, int(round(rating))))]


def abbr_for(name: str) -> str:
    abbr = dict(D.TEAMS).get(name, "").upper()
    return {"LAR": "LA", "WSH": "WAS"}.get(abbr, abbr)


# ---------------------------------------------------------------------------
# nflverse team-EPA grade loading -- ports Board._load_unit_grades exactly
# (same STATS_URL, same daily cache, same 8-game prior-season shrink via
# grades()/team_units() which are imported unmodified from the .pyw).
# ---------------------------------------------------------------------------
def load_unit_grades(week: int):
    STATS_CACHE.mkdir(exist_ok=True)
    data = {}
    notes = []
    for year in (2025, 2026):
        path = STATS_CACHE / f"team_{year}.csv"
        fresh = path.exists() and datetime.now().timestamp() - path.stat().st_mtime < 86400
        if not fresh:
            try:
                with urlopen(Request(D.STATS_URL.format(year=year), headers={"User-Agent": "NFL personal board"}), timeout=30) as response:
                    raw = response.read().decode("utf-8")
                D.team_units(raw, year)
                path.write_text(raw, encoding="utf-8")
            except Exception:
                notes.append(f"{year} feed unavailable" + ("; cached data" if path.exists() else ""))
        data[year] = D.team_units(path.read_text(encoding="utf-8"), year, week if year == 2026 else 99) if path.exists() else {}
    result = D.grades(data[2025], data[2026])
    state = f"nflverse EPA · before Week {week} · daily downloads · " + ("; ".join(notes) if notes else "cached feeds available")
    return result, state


def team_grade_values(unit_grades, name):
    abbr = abbr_for(name)
    item = unit_grades.get(abbr, {})
    off = item.get("off_grade", "N/A")
    defense = item.get("def_grade", "N/A")
    if not item:
        basis = "stats unavailable"
    else:
        basis = f"2026: {item['games']}g + 2025 prior" if item.get("games") else "2025 prior"
    return off, defense, basis


def team_epa_net(unit_grades, name):
    abbr = abbr_for(name)
    item = unit_grades.get(abbr, {})
    off_z = item.get("off_z")
    def_z = item.get("def_z")
    if off_z is None and def_z is None:
        return None
    return (off_z or 0.0) + (def_z or 0.0)


# ---------------------------------------------------------------------------
# Plain-function ports of Board's calculation methods. `c` is a read-only
# sqlite3 connection (row_factory=Row) to nfl_handicapping_board.db.
# ---------------------------------------------------------------------------
def team_row(c, name):
    return c.execute("SELECT * FROM teams WHERE name=?", (name,)).fetchone()


def days_rest(c, team, kickoff):
    try:
        current = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
        previous = c.execute(
            "SELECT kickoff FROM games WHERE (home=? OR away=?) AND kickoff<? ORDER BY kickoff DESC LIMIT 1",
            (team, team, kickoff)).fetchone()
        if not previous:
            return None
        return max(0, (current - datetime.fromisoformat(previous["kickoff"].replace("Z", "+00:00"))).days)
    except Exception:
        return None


def opening_line(c, event_id):
    return c.execute("SELECT home_spread,total FROM line_history WHERE event_id=? ORDER BY checked_at ASC LIMIT 1", (event_id,)).fetchone()


def weather_row(c, event_id):
    return c.execute("SELECT * FROM weather WHERE event_id=?", (event_id,)).fetchone()


def weather_text(w):
    if w["dome"]:
        return "DOME / roof assumed closed"
    stale = False
    try:
        stale = (datetime.now() - datetime.fromisoformat(w["checked_at"])).total_seconds() > 10800
    except (TypeError, ValueError):
        stale = True
    if w["temperature"] is None or w["wind"] is None:
        return "Forecast unavailable"
    base = f"{w['temperature']:.0f}°F · game max wind {w['wind']:.0f} mph"
    alert = w["alert"] if "alert" in w.keys() else None
    return base + ("\nWEATHER ALERT: " + alert if alert else "") + ("\nSTALE forecast — refresh required" if stale else "")


def venue_label(home, away):
    return (D.NEUTRAL_SITES.get((home, away)) or (None, None, None, D.VENUES.get(home, "stadium location")))[3]


def referee_assignment(c, week, home, away):
    row = c.execute("SELECT referee FROM referee_assignments WHERE season_week=? AND home=? AND away=?", (week, home, away)).fetchone()
    if not row:
        return None
    stats = c.execute("SELECT games,home_ats_pct,over_pct FROM referee_stats WHERE referee=?", (row["referee"],)).fetchone()
    return dict(referee=row["referee"], games=stats["games"] if stats else None,
                home_ats_pct=stats["home_ats_pct"] if stats else None,
                over_pct=stats["over_pct"] if stats else None)


def referee_factor(c, week, home, away):
    assignment = referee_assignment(c, week, home, away)
    if not assignment or assignment["home_ats_pct"] is None or (assignment["games"] or 0) < 10:
        return 0.0
    return round(max(-0.4, min(0.4, (assignment["home_ats_pct"] - 50.0) / 50.0 * 0.8)), 2)


def injury_lines(c, team):
    POSITION_PRIORITY = {"QB": 0, "RB": 1, "WR": 1, "TE": 1, "EDGE": 2, "DE": 2, "DT": 2, "LB": 2, "CB": 2,
                          "S": 2, "FS": 2, "SS": 2, "DB": 2, "K": 4, "P": 4, "LS": 5}
    rows = c.execute("SELECT player,position,status,comment FROM injuries WHERE team=? ORDER BY player", (team,)).fetchall()
    rows = sorted(rows, key=lambda r: POSITION_PRIORITY.get((r["position"] or "").upper(), 3))

    def entry(r):
        tag = re.search(r"\(([^)]{2,30})\)", r["comment"] or "")
        return f'{r["player"]} ({tag.group(1)})' if tag else r["player"]

    questionable = [entry(r) for r in rows if (r["status"] or "").lower() == "questionable"]
    out = [entry(r) for r in rows if (r["status"] or "").lower() in ("out", "injured reserve")]

    def names(items, limit=10):
        return ", ".join(items[:limit]) + (f" +{len(items) - limit}" if len(items) > limit else "")

    return names(questionable), names(out)


def injury_factor(c, home, away):
    weights = {"QB": 1.25, "OT": 0.35, "OG": 0.30, "C": 0.30, "WR": 0.25, "RB": 0.20, "TE": 0.15,
               "EDGE": 0.25, "DE": 0.25, "DT": 0.20, "LB": 0.15, "CB": 0.20, "S": 0.15}

    def burden(team):
        rows = c.execute("SELECT position,status FROM injuries WHERE team=?", (team,)).fetchall()
        total = 0.0
        for row in rows:
            status = (row["status"] or "").lower()
            if status not in ("out", "doubtful", "questionable", "injured reserve", "ir"):
                continue
            multiplier = 1.0 if status in ("out", "doubtful", "injured reserve", "ir") else 0.35
            total += weights.get((row["position"] or "").upper(), 0.08) * multiplier
        return min(1.5, total)

    return D.round_half(burden(away) - burden(home))


def venue_coords(home, away):
    neutral = D.NEUTRAL_SITES.get((home, away))
    if neutral:
        return (neutral[0], neutral[1])
    stadium = D.STADIUMS.get(home)
    return (stadium[0], stadium[1]) if stadium else None


def situational_factor(home, away, kickoff, home_rest, away_rest):
    if (home, away) in D.NEUTRAL_SITES:
        return 0.0
    value = 0.0
    if home_rest is not None and away_rest is not None:
        if home_rest <= 4 and away_rest > 4:
            value -= 0.5
        elif away_rest <= 4 and home_rest > 4:
            value += 0.5
    coords = venue_coords(home, away)
    away_stadium = D.STADIUMS.get(away)
    if coords and away_stadium:
        if D.haversine_miles(away_stadium[0], away_stadium[1], coords[0], coords[1]) > 1500:
            value += 0.5
    return max(-1.0, min(1.0, value))


def line_movement_factor(c, event_id, dk_spread):
    opening = opening_line(c, event_id)
    if not opening or dk_spread is None or opening["home_spread"] is None:
        return 0.0
    return max(-1.0, min(1.0, (opening["home_spread"] - dk_spread) / 3))


def form_factor(c, team, before_kickoff):
    games = c.execute(
        "SELECT home,away,home_score,away_score FROM games WHERE (home=? OR away=?) AND kickoff<? AND game_status='FINAL' "
        "AND home_score IS NOT NULL AND away_score IS NOT NULL ORDER BY kickoff DESC LIMIT 3",
        (team, team, before_kickoff)).fetchall()
    if not games:
        return 0.0
    total = 0.0
    for g in games:
        is_home = g["home"] == team
        my_score, opp_score = (g["home_score"], g["away_score"]) if is_home else (g["away_score"], g["home_score"])
        opponent = g["away"] if is_home else g["home"]
        opp_row = team_row(c, opponent)
        opp_rating = opp_row["consensus_rating"] if opp_row and opp_row["consensus_rating"] is not None else 5.0
        margin = my_score - opp_score
        direction = 1 if margin > 0 else (-1 if margin < 0 else 0)
        per_game = direction * min(1.0, abs(margin) / 17) + 0.15 * ((opp_rating - 5.0) / 5.0)
        total += per_game
    return max(-1.0, min(1.0, total / len(games)))


def rating_margin(home_rating, away_rating, home_name, away_name=None):
    home_field = 1.5
    if (home_name, away_name) in D.NEUTRAL_SITES:
        home_field = 0.0
    margin = -((home_rating - away_rating) * 1.15 + home_field)
    if away_name is not None and frozenset({home_name, away_name}) in D.RIVALRIES:
        RIVALRY_SHRINK = 1.0
        shrink = min(abs(margin), RIVALRY_SHRINK)
        margin -= shrink if margin > 0 else -shrink
    return D.round_half(margin)


def automated_factors(c, g, home_rest, away_rest, unit_grades, week):
    home, away = g["home"], g["away"]
    factors = [("Injury report", injury_factor(c, home, away))]
    venue = 0.0 if (home, away) in D.NEUTRAL_SITES else (0.50 if home in D.NOISE_ELITE else (0.25 if home in D.NOISE_LOUD else 0.0))
    factors.append(("Home crowd / stadium", venue))
    rest = 0.0
    if home_rest is not None and away_rest is not None:
        if home_rest >= 13 and away_rest < 13:
            rest += 0.75
        elif away_rest >= 13 and home_rest < 13:
            rest -= 0.75
        if home_rest <= 4 and away_rest > 4:
            rest -= 0.50
        elif away_rest <= 4 and home_rest > 4:
            rest += 0.50
    factors.append(("Rest / bye week", rest))
    short_week_overlap = 0.0
    if home_rest is not None and away_rest is not None:
        if home_rest <= 4 and away_rest > 4:
            short_week_overlap = -0.50
        elif away_rest <= 4 and home_rest > 4:
            short_week_overlap = 0.50
    factors.append(("Travel", situational_factor(home, away, g["kickoff"], home_rest, away_rest) - short_week_overlap))
    weather = weather_row(c, g["event_id"])
    weather_nudge = 0.0
    if weather and not weather["dome"] and (weather["wind"] or 0) >= 15:
        weather_nudge = 0.10
    factors.append(("Weather", weather_nudge))
    home_net = team_epa_net(unit_grades, home)
    away_net = team_epa_net(unit_grades, away)
    epa = 0.0
    if home_net is not None and away_net is not None:
        epa = max(-1.5, min(1.5, (home_net - away_net) * 0.6))
    factors.append(("EPA power (blended, prior+current season)", epa))
    factors.append(("Referee crew (career home ATS lean)", referee_factor(c, week, home, away)))
    return factors


def projected_game(c, g, home_team, away_team, home_rest, away_rest, unit_grades):
    margin = -rating_margin(home_team["consensus_rating"], away_team["consensus_rating"], g["home"], g["away"])
    factors = automated_factors(c, g, home_rest, away_rest, unit_grades, g["week"])
    margin = D.round_half(margin + sum(value for _, value in factors))
    off_h, def_h, _ = team_grade_values(unit_grades, g["home"])
    off_a, def_a, _ = team_grade_values(unit_grades, g["away"])
    grade_value = {"A": 1.0, "B": 0.4, "C": 0.0, "D": -0.4, "F": -1.0, "N/A": 0.0}
    total = 43.0 + grade_value.get(off_h, 0) + grade_value.get(off_a, 0) + 0.5 * (grade_value.get(def_h, 0) + grade_value.get(def_a, 0))
    weather = weather_row(c, g["event_id"])
    if weather and not weather["dome"]:
        total -= min(3.0, max(0.0, ((weather["wind"] or 0) - 12) * 0.18) + (weather["precipitation"] or 0) * 0.35)
    assignment = referee_assignment(c, g["week"], g["home"], g["away"])
    if assignment and assignment["over_pct"] is not None and (assignment["games"] or 0) >= 10:
        total += max(-1.5, min(1.5, (assignment["over_pct"] - 50.0) / 50.0 * 3.0))
    total = max(30.0, min(58.0, D.round_half(total)))
    home_score = max(10, int(round((total + margin) / 2)))
    away_score = max(10, int(round((total - margin) / 2)))
    projected_spread = -margin
    edge = None if g["dk_spread"] is None else D.round_half(g["dk_spread"] - projected_spread)
    return dict(home_score=home_score, away_score=away_score, total=total, home_spread=projected_spread, edge=edge, factors=factors)


def confidence_score(c, g, home_team, away_team, home_rest, away_rest, unit_grades):
    breakdown = []
    power = 0.0
    if home_team["consensus_rating"] is not None and away_team["consensus_rating"] is not None and g["dk_spread"] is not None:
        projection = projected_game(c, g, home_team, away_team, home_rest, away_rest, unit_grades)
        edge = projection["edge"] or 0.0
        power = max(-2.0, min(2.0, edge * (2 / 3)))
    breakdown.append(("Projected spread vs market", power, True))
    for label, value in automated_factors(c, g, home_rest, away_rest, unit_grades, g["week"]):
        breakdown.append((label, max(-1.0, min(1.0, value)), False))
    breakdown.append(("Line movement", line_movement_factor(c, g["event_id"], g["dk_spread"]), True))
    form = form_factor(c, g["home"], g["kickoff"]) - form_factor(c, g["away"], g["kickoff"])
    breakdown.append(("Recent form", max(-1.0, min(1.0, form / 2)), True))
    total = D.round_half(max(1.0, min(10.0, 5.0 + sum(v for _, v, counted in breakdown if counted))))
    return total, breakdown


def confidence_label(score, home, away):
    diff = score - 5.0
    team = (home if diff > 0 else away).split()[-1].upper()
    if abs(diff) < 1:
        return "PASS — no real edge", team
    strength = "LARGE LEAN" if abs(diff) >= 3 else "LEAN"
    return f"{strength} {team}", team


def auto_pick_note(c, g, home_rest, away_rest, unit_grades):
    home, away = g["home"], g["away"]
    home_short = home.split()[-1].upper()
    away_short = away.split()[-1].upper()
    parts = []
    if (home, away) in D.NEUTRAL_SITES:
        parts.append("This is a neutral-site game, so the usual home-field/travel read doesn't really apply -- treat it as more of an unknown than a normal home game.")
    order = {"A": 5, "B": 4, "C": 3, "D": 2, "F": 1}
    off_h, def_h, _ = team_grade_values(unit_grades, home)
    off_a, def_a, _ = team_grade_values(unit_grades, away)

    def lopsided(off_grade, def_grade):
        if off_grade not in order or def_grade not in order:
            return None
        return order[off_grade] - order[def_grade]

    gap = lopsided(off_a, def_h)
    if gap is not None and gap >= 2:
        parts.append(f"{away_short}'s offense ({off_a} grade) looks well ahead of {home_short}'s defense ({def_h}) -- expect them to move the ball.")
    elif gap is not None and gap <= -2:
        parts.append(f"{home_short}'s defense ({def_h} grade) looks well ahead of {away_short}'s offense ({off_a}) -- expect it to give them trouble.")
    gap = lopsided(off_h, def_a)
    if gap is not None and gap >= 2:
        parts.append(f"{home_short}'s offense ({off_h} grade) looks well ahead of {away_short}'s defense ({def_a}) -- expect them to move the ball.")
    elif gap is not None and gap <= -2:
        parts.append(f"{away_short}'s defense ({def_a} grade) looks well ahead of {home_short}'s offense ({off_h}) -- expect it to give them trouble.")
    qb_rows = {team: c.execute("SELECT player,status FROM injuries WHERE team=? AND position='QB' AND status IN ('Out','Doubtful','Questionable')", (team,)).fetchone() for team in (home, away)}
    for team, label in ((home, home_short), (away, away_short)):
        qb = qb_rows[team]
        if qb:
            parts.append(f"{label}'s starting QB situation is in question -- {qb['player']} is listed {qb['status'].lower()}.")
    _, out = injury_lines(c, home)
    if out:
        parts.append(f"{home_short} are banged up: {out}.")
    _, out = injury_lines(c, away)
    if out:
        parts.append(f"{away_short} are banged up: {out}.")
    if home in D.NOISE_ELITE:
        parts.append(f"{home_short} play in one of the league's loudest buildings -- that tends to disrupt a visiting offense's snap count.")
    elif home in D.NOISE_LOUD:
        parts.append(f"{home_short}'s home crowd is a real factor for a visiting offense.")
    if home_rest is not None and away_rest is not None:
        if home_rest >= 13 and away_rest < 13:
            parts.append(f"{home_short} are coming off extra rest.")
        elif away_rest >= 13 and home_rest < 13:
            parts.append(f"{away_short} are coming off extra rest.")
        elif home_rest <= 4 and away_rest > 4:
            parts.append(f"{home_short} are on a short week.")
        elif away_rest <= 4 and home_rest > 4:
            parts.append(f"{away_short} are on a short week.")
    return " ".join(parts) if parts else "Nothing stands out on paper -- the two teams grade out close and both look healthy. This lean is mostly the market number looking off, not a clear matchup edge."


def auto_pick_result(g):
    if not g["auto_pick_side"] or g["auto_pick_spread"] is None or g["game_status"] != "FINAL" or g["home_score"] is None or g["away_score"] is None:
        return None
    result = g["home_score"] - g["away_score"] + g["auto_pick_spread"]
    if result == 0:
        return "PUSH"
    winner = "home" if result > 0 else "away"
    return "WON" if winner == g["auto_pick_side"] else "LOST"


def pick_result(g):
    if not g["pick_side"] or g["pick_spread"] is None or g["game_status"] != "FINAL" or g["home_score"] is None or g["away_score"] is None:
        return None
    result = g["home_score"] - g["away_score"] + g["pick_spread"]
    if result == 0:
        return "PUSH"
    winner = "home" if result > 0 else "away"
    return "WON" if winner == g["pick_side"] else "LOST"


def ou_result(g):
    if not g["ou_pick"] or g["dk_total"] is None or g["home_score"] is None or g["away_score"] is None:
        return None
    if g["game_status"] != "FINAL":
        return None
    actual = g["home_score"] + g["away_score"]
    if actual == g["dk_total"]:
        return "PUSH"
    winner = "over" if actual > g["dk_total"] else "under"
    return "WON" if winner == g["ou_pick"] else "LOST"


def kickoff_display(kickoff):
    try:
        dt = datetime.fromisoformat(kickoff.replace("Z", "+00:00")).astimezone()
        hour12 = dt.hour % 12 or 12
        return f"{dt.month}/{dt.day}/{dt.year} {hour12}:{dt:%M %p}"
    except Exception:
        return kickoff


def compute_team_ranks(c):
    ranked_teams = c.execute("SELECT name FROM teams WHERE consensus_rating IS NOT NULL ORDER BY consensus_rating DESC,name").fetchall()
    team_rank = {row["name"]: index for index, row in enumerate(ranked_teams, 1)}
    return team_rank, len(ranked_teams)


def grade_payload(unit_grades, name):
    off, defense, basis = team_grade_values(unit_grades, name)
    return {
        "offense": off, "defense": defense, "basis": basis,
        "offense_colors": GRADE_COLORS.get(off, GRADE_COLORS["N/A"]),
        "defense_colors": GRADE_COLORS.get(defense, GRADE_COLORS["N/A"]),
    }


def build_game_payload(c, g, unit_grades, team_rank, team_rank_count):
    home, away = g["home"], g["away"]
    home_team, away_team = team_row(c, home), team_row(c, away)
    home_rest, away_rest = days_rest(c, home, g["kickoff"]), days_rest(c, away, g["kickoff"])
    venue = venue_label(home, away)
    neutral = venue.startswith("NEUTRAL SITE")
    home_noise = "elite" if (home in D.NOISE_ELITE and not neutral) else ("loud" if (home in D.NOISE_LOUD and not neutral) else None)
    rivalry = frozenset({home, away}) in D.RIVALRIES
    home_q, home_out = injury_lines(c, home)
    away_q, away_out = injury_lines(c, away)

    w = weather_row(c, g["event_id"])
    weather_payload = None
    if w:
        weather_payload = {
            "dome": bool(w["dome"]),
            "temperature": w["temperature"],
            "wind": w["wind"],
            "precipitation": w["precipitation"],
            "weather_code": w["weather_code"],
            "alert": w["alert"] if "alert" in w.keys() else None,
            "text": weather_text(w),
            "icon": D.weather_icon(w["weather_code"], w["wind"]),
        }

    assignment = referee_assignment(c, g["week"], home, away)
    ref_line = None
    if assignment:
        if assignment["home_ats_pct"] is None:
            ref_line = f"REF {assignment['referee']} · no career sample yet"
        elif assignment["over_pct"] is not None:
            ref_line = f"REF {assignment['referee']} · Home ATS {assignment['home_ats_pct']:.1f}% · Over {assignment['over_pct']:.1f}%"
        else:
            ref_line = f"REF {assignment['referee']} · Home ATS {assignment['home_ats_pct']:.1f}%"

    opening = opening_line(c, g["event_id"])
    opening_text = None
    if opening and g["dk_spread"] is not None and opening["home_spread"] is not None and opening["home_spread"] != g["dk_spread"]:
        opening_text = f"OPENED {home.split()[-1].upper()} {opening['home_spread']:+g} → NOW {g['dk_spread']:+g}"

    projection = None
    your_spread_text = None
    if home_team["consensus_rating"] is not None and away_team["consensus_rating"] is not None:
        projection = projected_game(c, g, home_team, away_team, home_rest, away_rest, unit_grades)
        if projection["home_spread"] == 0:
            your_spread_text = "PICK 'EM"
        else:
            fav = home if projection["home_spread"] < 0 else away
            fav_spread = projection["home_spread"] if projection["home_spread"] < 0 else -projection["home_spread"]
            your_spread_text = f"{fav.split()[-1].upper()} {fav_spread:+g}"

    score, breakdown = confidence_score(c, g, home_team, away_team, home_rest, away_rest, unit_grades)
    label, _ = confidence_label(score, home, away)

    cover_text = None
    if g["home_score"] is not None and g["away_score"] is not None:
        if g["dk_spread"] is None:
            cover_text = "FINAL"
        else:
            result = g["home_score"] - g["away_score"] + g["dk_spread"]
            cover_text = f"{home.split()[-1].upper()} COVERED" if result > 0 else (f"{away.split()[-1].upper()} COVERED" if result < 0 else "PUSH")

    auto_payload = None
    if g["auto_pick_side"]:
        auto_payload = {
            "side": g["auto_pick_side"],
            "team": (home if g["auto_pick_side"] == "home" else away),
            "spread": g["auto_pick_spread"],
            "result": auto_pick_result(g),
            "note": auto_pick_note(c, g, home_rest, away_rest, unit_grades),
        }

    return {
        "event_id": g["event_id"], "week": g["week"],
        "kickoff": g["kickoff"], "kickoff_display": kickoff_display(g["kickoff"]),
        "home": home, "away": away,
        "home_abbr": abbr_for(home), "away_abbr": abbr_for(away),
        "venue_label": venue, "neutral_site": neutral, "rivalry": rivalry, "home_noise": home_noise,
        "home_rating": home_team["consensus_rating"], "away_rating": away_team["consensus_rating"],
        "home_rank": team_rank.get(home), "away_rank": team_rank.get(away), "team_rank_count": team_rank_count,
        "home_grade": grade_payload(unit_grades, home), "away_grade": grade_payload(unit_grades, away),
        "home_rest": home_rest, "away_rest": away_rest,
        "home_questionable": home_q, "home_out": home_out, "away_questionable": away_q, "away_out": away_out,
        "dk_spread": g["dk_spread"], "dk_total": g["dk_total"],
        "opening_spread": opening["home_spread"] if opening else None, "opening_text": opening_text,
        "home_bets": g["home_bets"], "home_handle": g["home_handle"], "away_bets": g["away_bets"], "away_handle": g["away_handle"],
        "home_score": g["home_score"], "away_score": g["away_score"], "game_status": g["game_status"], "cover_text": cover_text,
        "weather": weather_payload, "referee_line": ref_line, "referee": assignment,
        "projection": projection, "your_spread_text": your_spread_text,
        "confidence_score": score, "confidence_label": label,
        "confidence_breakdown": [{"label": lbl, "value": val, "counted": counted} for lbl, val, counted in breakdown],
        "pick_side": g["pick_side"], "bet_star": bool(g["bet_star"]), "pick_spread": g["pick_spread"], "pick_result": pick_result(g),
        "ou_pick": g["ou_pick"], "ou_result": ou_result(g),
        "auto_pick": auto_payload,
    }


def compute_top_plays(c, games, unit_grades):
    top_plays = []
    for g in games:
        home_team, away_team = team_row(c, g["home"]), team_row(c, g["away"])
        if home_team["consensus_rating"] is None or away_team["consensus_rating"] is None or g["dk_spread"] is None:
            continue
        home_rest, away_rest = days_rest(c, g["home"], g["kickoff"]), days_rest(c, g["away"], g["kickoff"])
        projection = projected_game(c, g, home_team, away_team, home_rest, away_rest, unit_grades)
        score, _ = confidence_score(c, g, home_team, away_team, home_rest, away_rest, unit_grades)
        if abs(score - 5.0) < 1:
            continue
        fav = g["home"] if projection["home_spread"] < 0 else g["away"]
        opp = g["away"] if fav == g["home"] else g["home"]
        fav_spread = projection["home_spread"] if projection["home_spread"] < 0 else -projection["home_spread"]
        top_plays.append({
            "kickoff_display": kickoff_display(g["kickoff"]), "score": score,
            "fav": fav.split()[-1].upper(), "fav_spread": fav_spread, "opp": opp.split()[-1].upper(),
        })
    top_plays.sort(key=lambda p: abs(p["score"] - 5.0), reverse=True)
    return top_plays


def build_rankings(c, current_week, unit_grades, stats_state):
    teams = c.execute(
        f"SELECT name,consensus_rating,{','.join(col for col, _ in D.RANKING_SOURCES)} FROM teams WHERE consensus_rating IS NOT NULL ORDER BY consensus_rating DESC,name"
    ).fetchall()
    refreshed = {r["column_name"]: r["updated_at"] for r in c.execute("SELECT column_name,updated_at FROM ranking_sources").fetchall()}
    previous = c.execute("SELECT MAX(season_week) AS week FROM ranking_history WHERE season_week<?", (current_week,)).fetchone()["week"]
    old = {}
    if previous:
        old = {r["team"]: r["rating"] for r in c.execute("SELECT team,rating FROM ranking_history WHERE season_week=?", (previous,)).fetchall()}
    old_order = {name: index + 1 for index, (name, _rating) in enumerate(sorted(old.items(), key=lambda item: (-item[1], item[0])))}
    cutoff = (datetime.now() - timedelta(days=D.RANKING_STALE_DAYS)).isoformat(timespec="minutes")

    def freshness(column):
        if column not in refreshed:
            return "never loaded"
        return ("STALE, last " if refreshed[column] < cutoff else "") + datetime.fromisoformat(refreshed[column]).strftime("%b %d %I:%M%p")

    source_state = " · ".join(f"{label} {freshness(column)}" for column, label in D.RANKING_SOURCES)

    rows = []
    team_count = len(teams)
    for index, t in enumerate(teams, 1):
        rating = t["consensus_rating"]
        prior = old_order.get(t["name"])
        if prior is None:
            move, move_direction = "NEW", "flat"
        elif prior == index:
            move, move_direction = "—", "flat"
        elif prior > index:
            move, move_direction = f"▲ {prior - index}", "up"
        else:
            move, move_direction = f"▼ {index - prior}", "down"
        off, defense, _basis = team_grade_values(unit_grades, t["name"])
        rows.append({
            "rank": index, "of": team_count, "name": t["name"], "consensus_rating": rating,
            "rating_color_light": rating_color(rating, "light"), "rating_color_dark": rating_color(rating, "dark"),
            "offense": off, "defense": defense,
            "offense_colors": GRADE_COLORS.get(off, GRADE_COLORS["N/A"]), "defense_colors": GRADE_COLORS.get(defense, GRADE_COLORS["N/A"]),
            "move": move, "move_direction": move_direction,
            "sources": {label: t[column] for column, label in D.RANKING_SOURCES},
        })

    return {
        "week": current_week, "grade_guide": D.GRADE_GUIDE, "stats_state": stats_state,
        "source_state": source_state, "teams": rows,
    }


def build_picks(c, unit_grades):
    games = c.execute("SELECT * FROM games WHERE pick_side IS NOT NULL OR ou_pick IS NOT NULL OR auto_pick_side IS NOT NULL ORDER BY week,kickoff").fetchall()

    def record(results):
        w = sum(r == "WON" for r in results)
        l = sum(r == "LOST" for r in results)
        p = sum(r == "PUSH" for r in results)
        return f"{w}-{l}" + (f"-{p}" if p else "")

    bet_games = [g for g in games if g["pick_side"] and g["bet_star"]]
    pick_games = [g for g in games if g["pick_side"]]
    ou_results = [r for g in games if g["ou_pick"] and (r := ou_result(g))]
    auto_results = [r for g in games if g["auto_pick_side"] and (r := auto_pick_result(g))]

    def row_payload(g):
        pick_team = (g["home"] if g["pick_side"] == "home" else g["away"]) if g["pick_side"] else None
        line = None
        if g["pick_spread"] is not None and g["pick_side"]:
            line = g["pick_spread"] if g["pick_side"] == "home" else -g["pick_spread"]
        result = pick_result(g)
        result_state = result or ("LINE_UNKNOWN" if g["pick_side"] and g["pick_spread"] is None else ("PENDING" if g["pick_side"] else None))
        ou_state = None
        if g["ou_pick"]:
            r = ou_result(g)
            ou_state = {"pick": g["ou_pick"], "result": r or "PENDING"}
        off_a, def_a, _ = team_grade_values(unit_grades, g["away"])
        off_h, def_h, _ = team_grade_values(unit_grades, g["home"])
        auto_payload = None
        if g["auto_pick_side"]:
            auto_payload = {
                "team": (g["home"] if g["auto_pick_side"] == "home" else g["away"]).split()[-1].upper(),
                "result": auto_pick_result(g),
            }
        return {
            "week": g["week"], "home": g["home"], "away": g["away"],
            "home_grade_line": f"O {off_h} / D {def_h}", "away_grade_line": f"O {off_a} / D {def_a}",
            "pick_team": pick_team, "bet_star": bool(g["bet_star"]), "line": line,
            "result": result_state, "ou": ou_state, "auto": auto_payload,
        }

    return {
        "bet_record": record([r for g in bet_games if (r := pick_result(g))]),
        "pick_record": record([r for g in pick_games if (r := pick_result(g))]),
        "ou_record": record(ou_results),
        "auto_record": record(auto_results),
        "bet_games": [row_payload(g) for g in bet_games],
        "pick_games": [row_payload(g) for g in pick_games],
    }


def main():
    # Fetch current hourly weather for every game before anything else -- this
    # script otherwise only READS the weather table, so without this call the
    # site could ship stale/missing conditions for games kicking off soon.
    try:
        import refresh_weather
        refresh_weather.main()
    except Exception as ex:
        print(f"Weather refresh skipped ({ex}); continuing with whatever weather data is already cached.")

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    c = conn

    weeks = [row[0] for row in c.execute("SELECT DISTINCT week FROM games ORDER BY week")]
    games_by_week = {}
    top_plays_by_week = {}
    last_stats_state = "stats unavailable"
    for week in weeks:
        unit_grades, stats_state = load_unit_grades(week)
        last_stats_state = stats_state
        team_rank, team_rank_count = compute_team_ranks(c)
        games = c.execute("SELECT * FROM games WHERE week=? ORDER BY kickoff", (week,)).fetchall()
        games_by_week[str(week)] = [build_game_payload(c, g, unit_grades, team_rank, team_rank_count) for g in games]
        top_plays_by_week[str(week)] = compute_top_plays(c, games, unit_grades)

    current_week = max(weeks) if weeks else 1
    current_unit_grades, current_stats_state = load_unit_grades(current_week)
    rankings = build_rankings(c, current_week, current_unit_grades, current_stats_state)
    picks = build_picks(c, current_unit_grades)

    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "weeks": weeks,
        "current_week": current_week,
        "games_by_week": games_by_week,
        "top_plays_by_week": top_plays_by_week,
        "rankings": rankings,
        "picks": picks,
        "grade_colors": GRADE_COLORS,
    }
    OUT_PATH.write_text(json.dumps(snapshot, indent=None, default=str), encoding="utf-8")
    print(f"Wrote {OUT_PATH} ({OUT_PATH.stat().st_size:,} bytes) covering weeks {weeks}.")
    conn.close()


if __name__ == "__main__":
    main()
