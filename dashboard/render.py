"""Static HTML dashboard styled after the "Edge Board" mockup: top best-bets
ticker, a Best Bets of the Week grid, then a full-detail card grid with
projected score, model-vs-market edge, public betting splits, and an
expandable "how we got here" breakdown. Every number shown is read straight
from stored tables -- never recomputed at render time -- and any section with
no underlying data (e.g. betting splits before the scraper has a game) is
simply omitted rather than faked.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from common import TEAM_NAMES, VENUES, NOISE_ELITE, NOISE_LOUD, logo_url
from db.db import connect
from projection.injury_adjust import team_injury_impact, MIN_POINTS_TO_NOTE

OUT_PATH = Path(__file__).parent / "dashboard.html"
SHARP_DIVERGENCE_THRESHOLD = 8.0  # handle% - bets% gap that earns a "Sharp" flag

# A BEST_BET_THRESHOLD = 65 lean-score gate used to live here, on the assumption that a
# higher lean score meant a better play. Graded over 2,654 games it did the opposite,
# and monotonically: the leans it SHOWED went 337-377 (47.2%) while the ones it HID went
# 916-922 (49.8%), and raising the bar widened the gap (70+ showed 46.9%). The old lean
# score scaled with how far the model sat from the market, and those gaps are mostly
# shrinkage artifacts rather than information -- see the note in projection/project.py.
# Filtering on it surfaced the model's worst leans, so the gate is gone: the board shows
# every lean and prints its real measured record next to them.

CSS = """
:root{
  --bg:#0a0e17; --card-bg:#111726; --card-border:#26314a; --card-border-best:#22c55e;
  --gold:#f5a623; --gold2:#fbbf24; --teal:#2dd4bf; --away:#f97362;
  --text:#e8ecf3; --text-dim:#8b93a7; --badge-bg:#1a2136; --green:#22c55e;
  --blue:#3b82f6; --red:#ef4444;
}
*{box-sizing:border-box;}
body{font-family:-apple-system,"Segoe UI",Arial,sans-serif;background:var(--bg);color:var(--text);margin:0;}
.ticker{background:#0d1220;border-bottom:1px solid var(--card-border);display:flex;align-items:center;
  gap:0;overflow:hidden;white-space:nowrap;height:44px;}
.ticker-track{display:flex;align-items:center;gap:28px;padding-left:20px;animation:scroll 140s linear infinite;}
.ticker-item{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--text-dim);}
.ticker-item b{color:var(--text);}
.ticker-conf{background:var(--gold);color:#1a1206;font-weight:700;font-size:11px;border-radius:4px;padding:1px 6px;}
@keyframes scroll{from{transform:translateX(0);}to{transform:translateX(-50%);}}
.wrap{padding:28px 32px 48px;max-width:1400px;margin:0 auto;}
h1{font-size:34px;margin:0 0 6px;font-weight:800;}
h1 .accent{background:linear-gradient(90deg,var(--gold),var(--gold2));-webkit-background-clip:text;
  background-clip:text;color:transparent;}
.sub{color:var(--text-dim);font-size:14px;margin-bottom:6px;}
.headerrow{display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:12px;margin-bottom:18px;}
.weektag{background:var(--badge-bg);border:1px solid var(--card-border);border-radius:6px;padding:6px 12px;
  font-size:12px;color:var(--text-dim);letter-spacing:.04em;white-space:nowrap;}
.legend{display:flex;gap:18px;flex-wrap:wrap;margin:18px 0 26px;font-size:12px;color:var(--text-dim);}
.legend span{display:inline-flex;align-items:center;gap:6px;}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;}
.section-title{font-size:13px;font-weight:800;letter-spacing:.05em;color:var(--text);margin:30px 0 4px;
  display:flex;align-items:baseline;gap:10px;}
.section-title .n{font-size:12px;font-weight:500;color:var(--text-dim);}
.bb-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px;margin-top:14px;}
.bb-card{background:var(--card-bg);border:1px solid var(--card-border);border-radius:10px;padding:16px;}
.bb-matchup{font-size:15px;font-weight:700;margin-bottom:6px;display:flex;align-items:center;gap:6px;}
.bb-logo{width:20px;height:20px;object-fit:contain;}
.bb-pick{font-size:13px;margin-bottom:8px;}
.bb-pick .pick-val{color:var(--gold2);font-weight:800;}
.bb-pick .pick-type{color:var(--text-dim);font-size:11px;letter-spacing:.04em;margin-left:4px;}
.bb-reason{font-size:12.5px;color:var(--text-dim);line-height:1.4;margin-bottom:10px;}
.confbar{height:5px;border-radius:3px;background:#232a3d;overflow:hidden;margin-bottom:6px;}
.confbar-fill{height:100%;background:linear-gradient(90deg,var(--gold),var(--gold2));}
.confrow{display:flex;justify-content:space-between;font-size:11px;color:var(--text-dim);}
.confrow b{color:var(--text);}
.empty-note{color:var(--text-dim);font-size:13px;margin-top:10px;}

.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px;margin-top:14px;}
.card{background:var(--card-bg);border:1.5px solid var(--card-border);border-radius:10px;padding:16px;position:relative;}
.card.best{border-color:var(--card-border-best);}
.card-top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;}
.pill{font-size:10px;font-weight:800;letter-spacing:.05em;border-radius:4px;padding:3px 7px;}
.pill-best{background:var(--gold);color:#1a1206;}
.pill-weather{border:1px solid var(--card-border);color:var(--text-dim);background:#0d1220;}
.pill-wind{border-color:var(--green);color:var(--green);}
.pill-rain{border-color:var(--blue);color:var(--blue);}
.pill-snow{border-color:#e5e7eb;color:#e5e7eb;}
.teams{display:flex;justify-content:space-between;font-size:11px;color:var(--text-dim);letter-spacing:.04em;margin-bottom:2px;}
.team-names{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;}
.team-home{color:var(--teal);font-weight:800;font-size:19px;display:flex;align-items:center;gap:6px;}
.team-away{color:var(--away);font-weight:800;font-size:19px;display:flex;align-items:center;gap:6px;flex-direction:row-reverse;}
.logo{width:26px;height:26px;object-fit:contain;}
.dkline{text-align:center;font-size:12px;color:var(--text-dim);background:#0d1220;border:1px solid var(--card-border);
  border-radius:6px;padding:5px 8px;margin-bottom:8px;}
.dkline b{color:var(--gold2);}
.linemove{font-size:11.5px;color:var(--text-dim);margin:4px 0 8px;}
.venue{font-size:12px;color:var(--text-dim);margin-bottom:2px;}
.metaline{font-size:12px;color:var(--text-dim);margin-bottom:2px;}
.section-label{font-size:10.5px;color:var(--text-dim);letter-spacing:.06em;margin:12px 0 4px;font-weight:700;}
.projscore{font-size:20px;font-weight:800;margin-bottom:8px;}
.projscore .home{color:var(--teal);} .projscore .away{color:var(--away);} .projscore .dash{color:var(--text-dim);font-weight:400;}
table.edge{width:100%;border-collapse:collapse;font-size:12.5px;margin-bottom:6px;}
table.edge th{text-align:left;font-weight:600;color:var(--text-dim);font-size:10.5px;padding:3px 4px;letter-spacing:.03em;}
table.edge td{padding:4px 4px;border-top:1px solid #1c2338;}
table.edge td.edgeval{color:var(--gold2);font-weight:700;}
.splitrow{margin-bottom:8px;}
.splitlabel{display:flex;justify-content:space-between;font-size:11px;color:var(--text-dim);margin-bottom:3px;}
.sharp{color:var(--gold2);font-weight:700;}
.bar{height:7px;border-radius:4px;overflow:hidden;display:flex;margin-bottom:3px;background:#1a2136;}
.bar-a{background:var(--teal);} .bar-b{background:var(--away);}
.bar-c{background:var(--gold2);} .bar-d{background:var(--blue);}
.barnum{font-size:11px;color:var(--text-dim);text-align:right;}
.confidence-row{display:flex;justify-content:space-between;font-size:13px;margin-top:10px;padding-top:8px;
  border-top:1px solid #1c2338;}
.confidence-row b{font-size:14px;}
.leannote{font-size:10px;color:var(--text-dim);font-weight:400;}
details{margin-top:8px;}
summary{cursor:pointer;font-size:12px;color:var(--gold2);font-weight:600;list-style:none;}
summary::-webkit-details-marker{display:none;}
summary:before{content:"▸ ";}
details[open] summary:before{content:"▾ ";}
.hwg{margin-top:8px;font-size:12px;color:var(--text-dim);line-height:1.6;}
.hwg b{color:var(--text);}
.report-list{margin:0 0 8px;padding-left:18px;}
.report-list li{margin-bottom:5px;}
.premodel{font-size:11.5px;color:var(--text-dim);font-style:italic;margin-bottom:8px;padding-top:6px;border-top:1px solid #1c2338;}
.grades{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px;}
.badge{background:var(--badge-bg);border:1px solid var(--card-border);border-radius:4px;padding:2px 6px;font-size:10.5px;}
.footer{margin-top:36px;padding-top:16px;border-top:1px solid var(--card-border);color:var(--text-dim);
  font-size:12.5px;line-height:1.6;}
.power-wrap{display:grid;grid-template-columns:repeat(auto-fit,minmax(440px,1fr));gap:16px;margin-top:14px;}
table.power{width:100%;border-collapse:collapse;font-size:12.5px;background:var(--card-bg);
  border:1px solid var(--card-border);border-radius:10px;overflow:hidden;}
table.power th{text-align:left;font-weight:600;color:var(--text-dim);font-size:10.5px;padding:8px 10px;letter-spacing:.04em;
  background:#0d1220;}
table.power td{padding:6px 10px;border-top:1px solid #1c2338;white-space:nowrap;}
table.power td.rank{color:var(--text-dim);width:28px;}
table.power td.team{font-weight:700;display:flex;align-items:center;gap:8px;}
table.power td.team img{width:22px;height:22px;object-fit:contain;}
table.power td.pwr{color:var(--gold2);font-weight:800;}
table.power td.num{color:var(--text-dim);}
.up{color:var(--green);} .down{color:var(--red);}
"""


def _team_name(abbr):
    return TEAM_NAMES.get(abbr, abbr)


def round_half(x):
    """Round to the nearest 0.5 -- cleaner-looking lines (7.5, not 7.3),
    same convention sportsbooks actually post spreads/totals in."""
    if x is None:
        return None
    return round(x * 2) / 2


def team_line(final_spread, team, home_abbr):
    """final_spread is home-perspective (positive = home favored). A betting
    board instead prices each team's own number, favorite negative -- e.g.
    home favored by 3 posts as 'HOME -3 / AWAY +3', not 'HOME +3'."""
    if final_spread is None:
        return None
    return -final_spread if team == home_abbr else final_spread


# Grades are stored 0-100 on a 50 + z*15 scale (see grading/player_grades.py
# _scale) -- invert back to z and bucket it into a standard +/- letter scale,
# same shape as a report card, centered on a C at league-average (z=0).
_LETTER_BREAKS = [
    (1.5, "A+"), (1.17, "A"), (0.83, "A-"), (0.5, "B+"), (0.17, "B"), (-0.17, "B-"),
    (-0.5, "C+"), (-0.83, "C"), (-1.17, "C-"), (-1.5, "D+"), (-1.83, "D"), (-2.17, "D-"),
]


def letter_grade(score):
    if score is None:
        return "N/A"
    z = (score - 50.0) / 15.0
    for threshold, letter in _LETTER_BREAKS:
        if z >= threshold:
            return letter
    return "F"


def _weather_badge(row):
    if row["wind_mph"] is not None and row["wind_mph"] >= 15:
        return '<span class="pill pill-weather pill-wind">WIND 15+</span>'
    if row["precip_type"] == "rain":
        return '<span class="pill pill-weather pill-rain">RAIN</span>'
    if row["precip_type"] == "snow":
        return '<span class="pill pill-weather pill-snow">SNOW</span>'
    return ""


def _edge_row(label, model_val, market_val, unit="pts", better_side=None, signed=True):
    """MODEL + EDGE only -- the market number is already shown up top in the
    DK line band, no need to repeat it here."""
    if model_val is None or market_val is None:
        return f'<tr><td>{label}</td><td colspan="2" style="color:var(--text-dim)">pending</td></tr>'
    model_val, market_val = round_half(model_val), round_half(market_val)
    edge = model_val - market_val
    arrow = "" if abs(edge) < 0.05 else f' → {better_side}'
    fmt = "{:+.1f}" if signed else "{:.1f}"
    return (f'<tr><td>{label}</td><td>{fmt.format(model_val)}</td>'
            f'<td class="edgeval">{abs(edge):.1f} {unit}{arrow}</td></tr>')


def _line_movement(values, signed=True):
    """values: already sign-converted/rounded numbers, oldest->newest. Returns
    'Opened X -> Now Y' or '' when there's nothing to call movement (fewer
    than 2 snapshots, or the line hasn't actually moved)."""
    values = [v for v in values if v is not None]
    if len(values) < 2 or values[0] == values[-1]:
        return ""
    fmt = "{:+.1f}" if signed else "{:.1f}"
    return f"Opened {fmt.format(values[0])} → Now {fmt.format(values[-1])}"


def _split_bar(pct_a, pct_b, cls_a, cls_b):
    total = (pct_a or 0) + (pct_b or 0)
    if total <= 0:
        return ""
    wa = 100 * pct_a / total
    return f'<div class="bar"><div class="bar-{cls_a}" style="width:{wa:.1f}%"></div><div class="bar-{cls_b}" style="width:{100-wa:.1f}%"></div></div>'


def measured_ats_record(con, season: int) -> str:
    """The model's own graded ATS record, read straight from backtest_log, so the
    leans section can never read as an edge claim. Falls back to all seasons when
    the current one has too few graded games to say anything."""
    def _row(sql, args):
        r = con.execute(sql, args).fetchone()
        return (r["w"] or 0), (r["l"] or 0)

    base = ("SELECT SUM(ats_result='win') w, SUM(ats_result='loss') l FROM backtest_log "
            "WHERE ats_result IS NOT NULL")
    w, l = _row(base + " AND season=?", (season,))
    label = f"{season}"
    if w + l < 32:
        w, l = _row(base, ())
        label = "all-time"
    if w + l == 0:
        return "no graded games yet"
    return f"{label} ATS {w}-{l} ({100 * w / (w + l):.1f}%) · break-even is 52.4%"


def _pick_text(bet, g, home):
    """'BUF -3.5' for a spread pick, 'OVER 44.5' for a total pick."""
    if bet["bet_type"] == "spread":
        line = round_half(team_line(g["final_spread"], bet["pick"], home))
        return f"{bet['pick']} {line:+.1f}" if line is not None else bet["pick"]
    total = round_half(g["final_total"])
    return f"{bet['pick']} {total:.1f}" if total is not None else bet["pick"]


def render_week(season: int, week: int) -> Path:
    with connect() as con:
        games = con.execute(
            """SELECT g.*, p.final_spread, p.final_total, p.home_win_prob, p.confidence_score,
                      p.pre_shrink_spread, p.base_score_diff, p.hfa_adj, p.rest_adj, p.weather_adj,
                      p.rivalry_adj, p.ref_adj, p.injury_adj,
                      w.temp_f, w.wind_mph, w.precip_type, w.alert_text,
                      o.referee_name, o.crew_home_ats_pct, o.crew_games
               FROM games g
               LEFT JOIN projections p ON p.game_id = g.game_id
               LEFT JOIN weather w ON w.game_id = g.game_id
               LEFT JOIN officiating o ON o.game_id = g.game_id
               WHERE g.season=? AND g.week=?
               ORDER BY g.kickoff_utc""",
            (season, week),
        ).fetchall()

        bets_by_game: dict[str, list] = {}
        for r in con.execute(
            "SELECT * FROM best_bets WHERE game_id IN (SELECT game_id FROM games WHERE season=? AND week=?)",
            (season, week),
        ).fetchall():
            bets_by_game.setdefault(r["game_id"], []).append(r)

        ats_record_line = measured_ats_record(con, season)

        group_scores = {
            (r["team_abbr"], r["position_group"]): r["score"]
            for r in con.execute(
                "SELECT * FROM position_group_scores WHERE season=? AND week <= ? "
                "AND week = (SELECT MAX(week) FROM position_group_scores s2 "
                "WHERE s2.team_abbr=position_group_scores.team_abbr AND s2.season=? AND s2.week<=?)",
                (season, week, season, week),
            ).fetchall()
        }
        # Latest team score through the week being rendered, plus last season's
        # exit score -- a team with no current-season row yet (week 1) shows its
        # exit score, which is exactly what the early-season blend starts from.
        team_power = {
            r["team_abbr"]: r
            for r in con.execute(
                "SELECT team_abbr, week, offense_score, defense_score, overall_score, rolling_window_games "
                "FROM team_scores WHERE season=? AND week = (SELECT MAX(week) FROM team_scores s2 "
                "WHERE s2.team_abbr=team_scores.team_abbr AND s2.season=? AND s2.week<=?)",
                (season, season, week),
            ).fetchall()
        }
        prior_exit = {
            r["team_abbr"]: r
            for r in con.execute(
                "SELECT team_abbr, offense_score, defense_score, overall_score FROM team_scores t "
                "WHERE season=? AND week = (SELECT MAX(week) FROM team_scores t2 "
                "WHERE t2.team_abbr=t.team_abbr AND t2.season=?)",
                (season - 1, season - 1),
            ).fetchall()
        }
        team_off_def = {
            team: (row["offense_score"], row["defense_score"])
            for team, row in {**prior_exit, **team_power}.items()
        }

        splits_by_game = {}
        for r in con.execute(
            "SELECT * FROM splits WHERE game_id IN (SELECT game_id FROM games WHERE season=? AND week=?)",
            (season, week),
        ).fetchall():
            splits_by_game.setdefault(r["game_id"], {})[(r["bet_type"], r["side"])] = r

        line_moves_by_game: dict[str, list] = {}
        for r in con.execute(
            "SELECT * FROM line_history WHERE game_id IN (SELECT game_id FROM games WHERE season=? AND week=?) "
            "ORDER BY checked_at",
            (season, week),
        ).fetchall():
            line_moves_by_game.setdefault(r["game_id"], []).append(r)

        teams_this_week = {g["home_abbr"] for g in games} | {g["away_abbr"] for g in games}
        injury_by_team = {team: team_injury_impact(con, team, season, week) for team in teams_this_week}

    def grade_badges(team):
        groups = []
        for pg in ("QB", "RB", "WR", "TE", "OL", "DEF"):
            score = group_scores.get((team, pg))
            if score is not None:
                groups.append(f'<span class="badge">{team} {pg} {letter_grade(score)}</span>')
        if not groups:
            off_def = team_off_def.get(team)
            if off_def:
                off, defn = off_def
                if off is not None:
                    groups.append(f'<span class="badge">{team} OFF {letter_grade(off)}</span>')
                if defn is not None:
                    groups.append(f'<span class="badge">{team} DEF {letter_grade(defn)}</span>')
        return groups

    ticker_items, best_bet_cards, game_cards = [], [], []

    for g in games:
        home, away = g["home_abbr"], g["away_abbr"]
        conf = g["confidence_score"]
        game_bets = bets_by_game.get(g["game_id"], [])
        qualifying_bets = [b for b in game_bets if b["confidence_score"] is not None]

        for b in game_bets:
            if b["confidence_score"] is None:
                continue
            bet_type_label = "ATS" if b["bet_type"] == "spread" else "O/U"
            pick_txt = _pick_text(b, g, home)
            ticker_items.append(
                f'<div class="ticker-item">{away} @ {home} &nbsp;<b>{pick_txt}</b> ({bet_type_label}) '
                f'<span class="ticker-conf">{b["confidence_score"]:.0f}</span></div>'
            )

        for b in qualifying_bets:
            bet_type_label = "ATS" if b["bet_type"] == "spread" else "O/U"
            pick_txt = _pick_text(b, g, home)
            confbar_pct = min(100, b["confidence_score"] or 0)
            best_bet_cards.append(f"""
<div class="bb-card">
  <div class="bb-matchup"><img class="logo bb-logo" src="{logo_url(away)}" alt=""> {away} @ {home} <img class="logo bb-logo" src="{logo_url(home)}" alt=""></div>
  <div class="bb-pick"><span class="pick-val">{pick_txt}</span><span class="pick-type">{bet_type_label}</span></div>
  <div class="bb-reason">{b['reasoning_text'] or ''}</div>
  <div class="confbar"><div class="confbar-fill" style="width:{confbar_pct:.0f}%"></div></div>
  <div class="confrow"><span>Data Strength</span><b>{b['confidence_score']:.0f}</b></div>
</div>""")

        # --- full card ---
        weather_badge = _weather_badge(g)
        best_pill = "<span></span>"

        venue = VENUES.get(home, "")
        if g["roof_type"] == "dome":
            weather_line = "Dome"
        elif g["roof_type"] == "closed":
            weather_line = "Roof Closed"
        elif g["alert_text"]:
            weather_line = g["alert_text"]
        elif g["temp_f"] is not None:
            weather_line = f"{g['temp_f']:.0f}°F, wind {g['wind_mph']:.0f}mph"
        else:
            weather_line = "Forecast pending"

        crew_line = f"Referee: {g['referee_name']}" if g["referee_name"] else "Referee · pending assignment"

        # Final scores are whole points -- round to the nearest 1, not 0.5, so
        # "24.5 - 20.0" (which can't happen in a real football score) doesn't show up.
        home_pts = away_pts = None
        if g["final_total"] is not None and g["final_spread"] is not None:
            home_pts = round((g["final_total"] + g["final_spread"]) / 2)
            away_pts = round((g["final_total"] - g["final_spread"]) / 2)
        proj_score_html = (
            f'<span class="home">{home} {home_pts}</span><span class="dash"> — </span>'
            f'<span class="away">{away} {away_pts}</span>'
            if home_pts is not None else '<span class="dash">Pending team scores</span>'
        )

        market_spread = g["closing_spread"]
        market_total = g["closing_total"]
        # The edge arrow points at the side the model likes MORE THAN THE MARKET
        # does, not at the outright favorite: market home -7 vs. model home -4 is
        # a 3-point edge toward the away side even though home is still favored.
        edge_side = None
        if g["final_spread"] is not None and market_spread is not None:
            edge_side = home if g["final_spread"] > market_spread else away
        # Spread row is shown as the HOME team's own posted number (favorite
        # negative), same convention as a real board -- team_line() flips the
        # sign since final_spread/closing_spread are stored home-favored-positive.
        edge_rows = (
            _edge_row("Spread", team_line(g["final_spread"], home, home), team_line(market_spread, home, home),
                      better_side=edge_side)
            + _edge_row("Total", g["final_total"], market_total, signed=False,
                        better_side="OVER" if (g["final_total"] or 0) >= (market_total or 0) else "UNDER")
        )

        dk_spread_txt = f"{round_half(team_line(market_spread, home, home)):+.1f}" if market_spread is not None else "—"
        dk_total_txt = f"{round_half(market_total):.1f}" if market_total is not None else "—"
        dk_line_band = f'<div class="dkline">DraftKings: <b>{home} {dk_spread_txt}</b> &nbsp;·&nbsp; O/U <b>{dk_total_txt}</b></div>'

        moves = line_moves_by_game.get(g["game_id"], [])
        spread_move = _line_movement([round_half(team_line(m["spread"], home, home)) for m in moves])
        total_move = _line_movement([round_half(m["total"]) for m in moves], signed=False)
        move_bits = []
        if spread_move:
            move_bits.append(f"Spread: {spread_move}")
        if total_move:
            move_bits.append(f"Total: {total_move}")
        line_movement_html = (f'<div class="linemove">📈 Line movement: {" · ".join(move_bits)}</div>'
                               if move_bits else "")

        splits = splits_by_game.get(g["game_id"], {})
        split_html = ""
        sh, sa = splits.get(("spread", "home")), splits.get(("spread", "away"))
        if sh and sa:
            div_h = (sh["handle_pct"] or 0) - (sh["bets_pct"] or 0)
            div_a = (sa["handle_pct"] or 0) - (sa["bets_pct"] or 0)
            sharp_side = None
            if abs(div_h) >= SHARP_DIVERGENCE_THRESHOLD or abs(div_a) >= SHARP_DIVERGENCE_THRESHOLD:
                sharp_side = home if div_h > div_a else away
            sharp_flag = f'<span class="sharp">⚡ Sharp: {sharp_side}</span>' if sharp_side else ""
            split_html += f"""
<div class="splitrow">
  <div class="splitlabel"><span>SPREAD — BETS</span>{sharp_flag}</div>
  {_split_bar(sh['bets_pct'], sa['bets_pct'], 'a', 'b')}
  <div class="barnum">{home} {sh['bets_pct']:.0f}% / {away} {sa['bets_pct']:.0f}%</div>
  <div class="splitlabel"><span>SPREAD — HANDLE</span></div>
  {_split_bar(sh['handle_pct'], sa['handle_pct'], 'a', 'b')}
  <div class="barnum">{home} {sh['handle_pct']:.0f}% / {away} {sa['handle_pct']:.0f}%</div>
</div>"""
        to, tu = splits.get(("total", "over")), splits.get(("total", "under"))
        if to and tu:
            split_html += f"""
<div class="splitrow">
  <div class="splitlabel"><span>TOTAL — BETS</span></div>
  {_split_bar(to['bets_pct'], tu['bets_pct'], 'c', 'd')}
  <div class="barnum">OVER {to['bets_pct']:.0f}% / UNDER {tu['bets_pct']:.0f}%</div>
</div>"""
        splits_block = (f'<div class="section-label">PUBLIC BETTING SPLITS</div>{split_html}') if split_html else ""

        badges = grade_badges(home) + grade_badges(away)

        # --- Game Report: plain-English account of what actually moved the number ---
        report_items = []
        hfa_val = g["hfa_adj"] or 0.0
        if hfa_val >= 2.5:
            report_items.append(f"Elevated home-field bonus for {home} (+{hfa_val:.1f} pts) — a notably loud/tough road venue.")
        elif hfa_val > 0.05:
            report_items.append(f"Standard home-field edge for {home} (+{hfa_val:.1f} pts).")

        rest_val = g["rest_adj"] or 0.0
        if abs(rest_val) > 0.3:
            beneficiary = home if rest_val > 0 else away
            report_items.append(f"Rest/travel edge favors {beneficiary} ({abs(rest_val):.1f} pts) — short week or extra rest differential.")

        weather_val = g["weather_adj"] or 0.0
        if abs(weather_val) > 0.05:
            report_items.append(f"Weather trims the total by {abs(weather_val):.1f} pts — wind/precipitation in the forecast.")

        if g["is_divisional"]:
            report_items.append("Divisional rivalry game — total compressed; these tend to play closer than the power-rating gap alone suggests.")

        if g["referee_name"]:
            ats = f"{g['crew_home_ats_pct']:.0f}%" if g["crew_home_ats_pct"] is not None else "n/a"
            games_n = g["crew_games"] or 0
            ref_val = g["ref_adj"] or 0.0
            if abs(ref_val) > 0.05:
                leaning = home if ref_val > 0 else away
                report_items.append(f"Referee {g['referee_name']}: {ats} career home ATS ({games_n}g) — "
                                     f"mild lean toward {leaning} ({abs(ref_val):.2f} pts, soft signal).")
            else:
                report_items.append(f"Referee {g['referee_name']}: {ats} career home ATS ({games_n}g) — no meaningful lean.")

        injury_val = g["injury_adj"] or 0.0
        injury_bits = []
        for team in (home, away):
            impact = injury_by_team.get(team, {"points": 0.0, "players": []})
            named = [p for p in impact["players"] if p["points"] >= MIN_POINTS_TO_NOTE]
            if named:
                lines = ", ".join(f"{p['position']} {p['name']} ({p['status']}) −{p['points']:.1f}" for p in named)
                injury_bits.append(f"<b>{team} −{impact['points']:.1f}</b>: {lines}")
        if abs(injury_val) > 0.05:
            beneficiary = home if injury_val > 0 else away
            report_items.append(f"Injuries move the number {abs(injury_val):.1f} pts toward {beneficiary}. "
                                 f"{' · '.join(injury_bits)}")
        elif injury_bits:
            report_items.append(f"Injuries wash out between the two sides. {' · '.join(injury_bits)}")

        if not report_items:
            report_items.append("No unusual factors on paper — this number is mostly the power-rating gap plus standard home field.")

        pre_shrink_note = ""
        if g["pre_shrink_spread"] is not None and market_spread is not None:
            pre_line = round_half(team_line(g["pre_shrink_spread"], home, home))
            pre_shrink_note = (f'<div class="premodel">Our own read before blending toward the market: '
                                f'{home} {pre_line:+.1f} — final number blends that 70% with the market line.</div>')

        report_html = "".join(f"<li>{item}</li>" for item in report_items)
        game_report = f"""
<div class="hwg">
  <ul class="report-list">{report_html}</ul>
  {pre_shrink_note}
  <div class="grades">{''.join(badges)}</div>
</div>"""

        conf_val = f"{conf:.0f}" if conf is not None else "N/A"
        weather_meta = f'<div class="metaline">{weather_line}</div>' if weather_line else ""
        speaker = ""
        if home in NOISE_ELITE:
            speaker = '<span title="Elite crowd noise — bigger home-field bump">🔊</span>'
        elif home in NOISE_LOUD:
            speaker = '<span title="Loud venue — modest home-field bump">🔈</span>'

        game_cards.append(f"""
<div class="card">
  <div class="card-top">{best_pill}{weather_badge}</div>
  <div class="teams"><span>HOME</span><span>AWAY</span></div>
  <div class="team-names">
    <span class="team-home"><img class="logo" src="{logo_url(home)}" alt="">{home}{speaker}</span>
    <span class="team-away">{away}<img class="logo" src="{logo_url(away)}" alt=""></span>
  </div>
  {dk_line_band}
  <div class="venue">{venue}</div>
  {weather_meta}
  <div class="metaline">{crew_line}</div>
  <div class="section-label">PROJECTED SCORE</div>
  <div class="projscore">{proj_score_html}</div>
  <table class="edge"><tr><th></th><th>MODEL</th><th>EDGE</th></tr>{edge_rows}</table>
  {line_movement_html}
  {splits_block}
  <div class="confidence-row"><span>Data Strength <span class="leannote">(how much data is behind the rating — not a bet grade)</span></span><b>{conf_val}</b></div>
  <details><summary>Game Report</summary>{game_report}</details>
</div>""")

    # --- Power rankings: every team by current overall score, with last
    # season's exit score/rank beside it so the carryover is visible ---
    prior_order = sorted((t for t in prior_exit if prior_exit[t]["overall_score"] is not None),
                         key=lambda t: -prior_exit[t]["overall_score"])
    prior_rank = {t: i + 1 for i, t in enumerate(prior_order)}
    ranking = []
    for team in TEAM_NAMES:
        cur, prior = team_power.get(team), prior_exit.get(team)
        src = cur or prior
        if not src or src["overall_score"] is None:
            continue
        ranking.append({
            "team": team, "overall": src["overall_score"], "off": src["offense_score"], "def": src["defense_score"],
            "games": cur["rolling_window_games"] if cur else 0,
            "prior": prior["overall_score"] if prior else None,
        })
    ranking.sort(key=lambda r: -r["overall"])
    power_rows = []
    for i, r in enumerate(ranking, start=1):
        was = prior_rank.get(r["team"])
        if was is None:
            move = '<span class="num">new</span>'
        elif was == i:
            move = '<span class="num">—</span>'
        elif was > i:
            move = f'<span class="up">▲{was - i}</span>'
        else:
            move = f'<span class="down">▼{i - was}</span>'
        prior_txt = f"{r['prior']:.1f} (#{was})" if r["prior"] is not None else "—"
        power_rows.append(
            f'<tr><td class="rank">{i}</td>'
            f'<td class="team"><img src="{logo_url(r["team"])}" alt="">{r["team"]}</td>'
            f'<td class="pwr">{r["overall"]:.1f}</td>'
            f'<td>{letter_grade(r["off"])}</td><td>{letter_grade(r["def"])}</td>'
            f'<td class="num">{prior_txt}</td><td>{move}</td><td class="num">{r["games"]}</td></tr>'
        )
    power_head = (f'<tr><th>#</th><th>TEAM</th><th>POWER</th><th>OFF</th><th>DEF</th>'
                  f'<th>{season - 1} EXIT</th><th>MOVE</th><th>GAMES</th></tr>')
    half = (len(power_rows) + 1) // 2
    power_tables = "".join(
        f'<table class="power">{power_head}{"".join(chunk)}</table>'
        for chunk in (power_rows[:half], power_rows[half:]) if chunk
    )
    scored_weeks = [row["week"] for row in team_power.values()]
    power_through = f"through week {max(scored_weeks)}" if scored_weeks else f"{season - 1} exit scores, no {season} games yet"
    power_section = (
        f'<div class="section-title">POWER RANKINGS <span class="n">{power_through} · '
        f'weeks 1-6 blend in each team\'s {season - 1} exit score and every returning player\'s '
        f'{season - 1} grade</span></div><div class="power-wrap">{power_tables}</div>'
        if power_rows else ""
    )

    n_games = len(games)
    n_leans = len(best_bet_cards)          # one card per bet (a game can have both)
    n_lean_games = sum(1 for g in games if bets_by_game.get(g["game_id"]))
    ticker_html = "".join(ticker_items) or '<div class="ticker-item">No graded games this week yet</div>'
    ticker_html = ticker_html + ticker_html  # duplicate for seamless marquee loop

    best_bets_section = (
        f'<div class="bb-grid">{"".join(best_bet_cards)}</div>' if best_bet_cards else
        '<div class="empty-note">No model leans this week — every game sits on the market number.</div>'
    )

    html_doc = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Edge Board — Week {week}, {season}</title><style>{CSS}</style></head>
<body>
<div class="ticker">
  <div class="ticker-track">{ticker_html}</div>
</div>
<div class="wrap">
  <div class="headerrow">
    <div>
      <h1>Edge <span class="accent">Board</span></h1>
      <div class="sub">Power-score projections with the full adjustment stack shown for every game — home team always on the left.</div>
    </div>
    <div class="weektag">WEEK {week} · {season} · {n_games} GAMES</div>
  </div>
  <div class="legend">
    <span><span class="dot" style="background:var(--green)"></span>Wind 15mph+</span>
    <span><span class="dot" style="background:var(--blue)"></span>Rain</span>
    <span><span class="dot" style="background:#e5e7eb"></span>Snow</span>
  </div>

  <div class="section-title">MODEL LEANS <span class="n">{n_leans} leans across {n_lean_games} games · {ats_record_line}</span></div>
  {best_bets_section}

  {power_section}

  <div class="section-title">ALL GAMES <span class="n">{n_games} games</span></div>
  <div class="grid">{''.join(game_cards)}</div>

  <div class="footer">
    Projected score splits the model's total by its spread. Edge is the model's number minus the market's —
    for spread it's points toward the team the model favors more than the market; for total it's points toward
    Over or Under. Public betting splits show % of bets (ticket count) vs. % of handle (money) per side — when
    handle leans one way notably more than bets do, that's the classic signature of sharp money, flagged at an
    {SHARP_DIVERGENCE_THRESHOLD:.0f}-point gap. Data Strength (0-100) says how much data sits behind a game's
    two team ratings, docked for how many situational adjustments had to carry the number — it is a data-quality
    reading, not a bet grade. It used to also scale with how far the model sat from the market, and a 2016-2026
    backtest over 2,654 graded games showed that ranking was backwards: leans scoring 65+ went 47.2% ATS while
    everything below went 49.8%. The cause is mechanical — the model's spread is shrunk toward zero in proportion
    to its own weakness (spread sd 4.65 vs the market's 6.09), so its biggest disagreements are mostly that
    shrinkage rather than information, which is also why 74.6% of all leans historically landed on the underdog.
    The model has never beaten the closing line (49.1% ATS over 2,654 games against a 52.4% break-even), so every
    lean here is a read, not an edge. QB/RB/WR/TE grades are individual EPA-based; OL/DEF use a
    snap-weighted team-unit proxy since no public per-player data exists for those positions. Power rankings are
    the model's team scores (0-100, 50 = league average) through the last completed week: a recency-weighted
    window of the last 8 games, with weeks 1-6 blending in last season's exit score (half at week 1, tapering
    to under a tenth by week 6). Each returning player's own grade is carried in from last season the same way,
    so a QB who changed teams brings his number with him. GAMES is how many of this season's games are in the
    window. Injury scale (spread points for a full-time starter ruled Out): QB 5 · RB, WR, OT, DE/EDGE, CB 1 ·
    TE, OLB 0.75 · G, C, DT, LB, S 0.5. Doubtful counts 90% of that, Questionable 40%, and everything is scaled
    by the player's share of his unit's snaps over his team's last three games, with a 7-point cap per team.
    The per-player numbers in each Game Report are those points, applied to the model number before the 70/30
    market blend. See nfl_handicapping_framework.md for the full model design.
  </div>
</div>
</body></html>"""
    OUT_PATH.write_text(html_doc, encoding="utf-8")
    return OUT_PATH


if __name__ == "__main__":
    season, week = int(sys.argv[1]), int(sys.argv[2])
    path = render_week(season, week)
    print(f"Wrote {path}")
