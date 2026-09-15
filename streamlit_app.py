"""Public, mobile-friendly companion for the NFL handicapping board.

This app deliberately reads `web_snapshot.json` -- a pre-computed export of
everything the desktop app's Week / Power Rankings / My Picks views show,
produced locally by `export_web_snapshot.py` (which can import the desktop
.pyw and hit the network; Tkinter and outbound scraping can't run on
Streamlit Community Cloud). That makes this site static and always
reachable, independent of the authoring PC.

Read-only by design: there is no pick-saving or star-toggling here. That
stays desktop-only.

Visual design: the "Edge Board" look -- dark theme, scrolling best-bets
ticker, a Best Bets of the Week card grid, then a full matchup card grid
with projected score, model-vs-market edge table, and public betting
splits. Ported from dashboard/render.py's static HTML dashboard so both
the desktop board and this cloud companion share one visual identity.
"""
from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

APP_DIR = Path(__file__).resolve().parent
SNAPSHOT_PATH = APP_DIR / "web_snapshot.json"


@st.cache_data(ttl=300)
def load_snapshot() -> dict:
    if not SNAPSHOT_PATH.exists():
        raise FileNotFoundError(
            "web_snapshot.json is missing. Run `python export_web_snapshot.py` "
            "locally, then commit and push it."
        )
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


st.set_page_config(page_title="NFL Handicapping Board", page_icon="\U0001F3C8", layout="wide", initial_sidebar_state="collapsed")

try:
    snap = load_snapshot()
except (FileNotFoundError, json.JSONDecodeError) as error:
    st.title("NFL handicapping board")
    st.error(f"Unable to load the published board: {error}")
    st.stop()

# ---------------------------------------------------------------------------
# Edge Board dark theme -- palette and component classes lifted straight from
# dashboard/render.py's CSS so the two boards look like one product. Layered
# on top of/over Streamlit's own chrome (app background, headers, tabs,
# sidebar, buttons) via data-testid selectors so widgets that stay
# Streamlit-native (tabs, selectbox, expanders, dataframes) still read as
# part of the same dark UI instead of clashing with it.
# ---------------------------------------------------------------------------
st.markdown(
    """
    <style>
    :root{
      --bg:#0a0e17; --card-bg:#111726; --card-border:#26314a; --card-border-best:#22c55e;
      --gold:#f5a623; --gold2:#fbbf24; --teal:#2dd4bf; --away:#f97362;
      --text:#e8ecf3; --text-dim:#8b93a7; --badge-bg:#1a2136; --green:#22c55e;
      --blue:#3b82f6; --red:#ef4444;
    }
    [data-testid="stAppViewContainer"], [data-testid="stHeader"], [data-testid="stSidebar"], .stApp{
      background:var(--bg) !important; color:var(--text) !important;
    }
    [data-testid="stSidebar"]{border-right:1px solid var(--card-border);}
    [data-testid="stMarkdownContainer"] p, [data-testid="stMarkdownContainer"] li,
    [data-testid="stCaptionContainer"], .stApp p, .stApp span, .stApp label{color:var(--text);}
    [data-testid="stCaptionContainer"]{color:var(--text-dim) !important;}
    .stTabs [data-baseweb="tab-list"]{border-bottom:1px solid var(--card-border);gap:4px;}
    .stTabs [data-baseweb="tab"]{color:var(--text-dim);}
    .stTabs [aria-selected="true"]{color:var(--gold2) !important;}
    [data-testid="stMetric"], [data-testid="stExpander"], .stDataFrame{
      background:var(--card-bg) !important;border:1px solid var(--card-border) !important;border-radius:10px !important;
    }
    [data-testid="stMetricValue"]{color:var(--text) !important;}
    [data-testid="stMetricLabel"]{color:var(--text-dim) !important;}
    hr{border-color:var(--card-border) !important;}

    /* --- Edge Board components (ported verbatim from render.py's CSS) --- */
    .eb-ticker{background:#0d1220;border-bottom:1px solid var(--card-border);border-radius:8px;
      display:flex;align-items:center;overflow:hidden;white-space:nowrap;height:40px;margin:4px 0 18px;}
    .eb-ticker-track{display:flex;align-items:center;gap:28px;padding-left:16px;animation:eb-scroll 140s linear infinite;}
    .eb-ticker-item{display:flex;align-items:center;gap:8px;font-size:12.5px;color:var(--text-dim);}
    .eb-ticker-item b{color:var(--text);}
    .eb-ticker-conf{background:var(--gold);color:#1a1206;font-weight:700;font-size:10.5px;border-radius:4px;padding:1px 6px;}
    @keyframes eb-scroll{from{transform:translateX(0);}to{transform:translateX(-50%);}}

    h1.eb-h1{font-size:32px;margin:0 0 4px;font-weight:800;}
    h1.eb-h1 .accent{background:linear-gradient(90deg,var(--gold),var(--gold2));-webkit-background-clip:text;
      background-clip:text;color:transparent;}
    .eb-sub{color:var(--text-dim);font-size:13.5px;margin-bottom:4px;}
    .eb-section-title{font-size:13px;font-weight:800;letter-spacing:.05em;color:var(--text);margin:26px 0 4px;
      display:flex;align-items:baseline;gap:10px;}
    .eb-section-title .n{font-size:12px;font-weight:500;color:var(--text-dim);}

    .bb-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px;margin-top:12px;}
    .bb-card{background:var(--card-bg);border:1px solid var(--card-border);border-radius:10px;padding:14px;}
    .bb-matchup{font-size:14.5px;font-weight:700;margin-bottom:6px;display:flex;align-items:center;gap:6px;}
    .bb-logo{width:19px;height:19px;object-fit:contain;}
    .bb-pick{font-size:13px;margin-bottom:8px;}
    .bb-pick .pick-val{color:var(--gold2);font-weight:800;}
    .bb-pick .pick-type{color:var(--text-dim);font-size:11px;letter-spacing:.04em;margin-left:4px;}
    .bb-reason{font-size:12px;color:var(--text-dim);line-height:1.4;margin-bottom:10px;}
    .confbar{height:5px;border-radius:3px;background:#232a3d;overflow:hidden;margin-bottom:6px;}
    .confbar-fill{height:100%;background:linear-gradient(90deg,var(--gold),var(--gold2));}
    .confrow{display:flex;justify-content:space-between;font-size:11px;color:var(--text-dim);}
    .confrow b{color:var(--text);}
    .empty-note{color:var(--text-dim);font-size:13px;margin-top:10px;}

    .eb-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px;margin-top:14px;}
    .eb-card{background:var(--card-bg);border:1.5px solid var(--card-border);border-radius:10px;
      padding:16px;position:relative;}
    .eb-card.best{border-color:var(--card-border-best);}
    .eb-card-top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;gap:6px;flex-wrap:wrap;}
    .pill{font-size:10px;font-weight:800;letter-spacing:.05em;border-radius:4px;padding:3px 7px;}
    .pill-best{background:var(--gold);color:#1a1206;}
    .pill-weather{border:1px solid var(--card-border);color:var(--text-dim);background:#0d1220;}
    .pill-wind{border-color:var(--green);color:var(--green);}
    .pill-rain{border-color:var(--blue);color:var(--blue);}
    .pill-snow{border-color:#e5e7eb;color:#e5e7eb;}
    .eb-team-names{display:flex;justify-content:center;align-items:center;gap:10px;margin-bottom:8px;}
    .eb-team{display:flex;align-items:center;gap:6px;font-weight:800;font-size:17px;}
    .eb-team.home{color:var(--teal);} .eb-team.away{color:var(--away);flex-direction:row-reverse;}
    .eb-team img{width:26px;height:26px;object-fit:contain;}
    .eb-at{color:var(--text-dim);font-weight:700;font-size:12px;}
    .dkline{text-align:center;font-size:12px;color:var(--text-dim);background:#0d1220;border:1px solid var(--card-border);
      border-radius:6px;padding:5px 8px;margin-bottom:8px;}
    .dkline b{color:var(--gold2);}
    .eb-meta{font-size:12px;color:var(--text-dim);margin-bottom:2px;text-align:center;}
    .eb-meta.info{color:#3a7dc9;} .eb-meta.warn{color:#c07a12;font-weight:700;}
    .eb-meta.good{color:var(--green);font-weight:700;} .eb-meta.bad{color:var(--red);font-weight:700;}
    .section-label{font-size:10.5px;color:var(--text-dim);letter-spacing:.06em;margin:12px 0 4px;font-weight:700;}
    .projscore{font-size:20px;font-weight:800;margin-bottom:8px;text-align:center;}
    .projscore .home{color:var(--teal);} .projscore .away{color:var(--away);} .projscore .dash{color:var(--text-dim);font-weight:400;}
    table.edge{width:100%;border-collapse:collapse;font-size:12.5px;margin-bottom:6px;}
    table.edge th{text-align:left;font-weight:600;color:var(--text-dim);font-size:10.5px;padding:3px 4px;letter-spacing:.03em;}
    table.edge td{padding:4px 4px;border-top:1px solid #1c2338;color:var(--text);}
    table.edge td.edgeval{color:var(--gold2);font-weight:700;}
    .splitrow{margin-bottom:8px;}
    .splitlabel{display:flex;justify-content:space-between;font-size:11px;color:var(--text-dim);margin-bottom:3px;}
    .sharp{color:var(--gold2);font-weight:700;}
    .bar{height:7px;border-radius:4px;overflow:hidden;display:flex;margin-bottom:3px;background:#1a2136;}
    .bar-a{background:var(--teal);} .bar-b{background:var(--away);}
    .bar-c{background:var(--gold2);} .bar-d{background:var(--blue);}
    .barnum{font-size:11px;color:var(--text-dim);text-align:right;}
    .confidence-row{display:flex;justify-content:space-between;font-size:13px;margin-top:10px;padding-top:8px;
      border-top:1px solid #1c2338;color:var(--text);}
    .confidence-row b{font-size:14px;}
    .leannote{font-size:10px;color:var(--text-dim);font-weight:400;}
    .eb-grades{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px;}
    .badge{background:var(--badge-bg);border:1px solid var(--card-border);border-radius:4px;padding:2px 6px;font-size:10.5px;}
    details.eb-details{margin-top:8px;}
    details.eb-details summary{cursor:pointer;font-size:12px;color:var(--gold2);font-weight:600;list-style:none;}
    details.eb-details summary::-webkit-details-marker{display:none;}
    details.eb-details summary:before{content:"▸ ";}
    details.eb-details[open] summary:before{content:"▾ ";}
    .eb-details-body{margin-top:8px;font-size:12px;color:var(--text-dim);line-height:1.6;}
    .eb-details-body b{color:var(--text);}
    .eb-report-list{margin:0 0 8px;padding-left:18px;}
    .eb-report-list li{margin-bottom:5px;}

    @media (max-width: 480px){
      h1.eb-h1{font-size:24px !important}
      .eb-team img{width:22px;height:22px}
      .eb-team{font-size:14px}
      .projscore{font-size:17px}
    }
    </style>
    """,
    unsafe_allow_html=True,
)

LOGO_URL = "https://a.espncdn.com/i/teamlogos/nfl/500/{abbr}.png"

WEATHER_BORDER_COLORS = {"snow": "#e5e7eb", "rain": "#3b82f6", "wind": "#22c55e"}
WEATHER_PILL_CLASS = {"snow": "pill-snow", "rain": "pill-rain", "wind": "pill-wind"}


def weather_alert_kind(weather: dict | None) -> str | None:
    if not weather or weather.get("dome"):
        return None
    alert = (weather.get("alert") or "").upper()
    if "SNOW" in alert:
        return "snow"
    if "RAIN" in alert:
        return "rain"
    if "WIND" in alert or (weather.get("wind") or 0) >= 15:
        return "wind"
    return None


DARK = True  # Edge Board is a fixed dark theme -- always use the dark badge/rating palette.


def grade_chip(label: str, grade: str, colors: dict) -> str:
    fg, bg = colors["dark" if DARK else "light"]
    return f'<span class="badge" style="color:{fg};background:{bg}">{label} {grade}</span>'


weeks = snap.get("weeks", [])
default_index = weeks.index(snap.get("current_week", weeks[-1])) if snap.get("current_week") in weeks else len(weeks) - 1 if weeks else 0

header_l, header_r = st.columns([3, 1])
with header_l:
    st.markdown('<h1 class="eb-h1">Edge <span class="accent">Board</span></h1>', unsafe_allow_html=True)
with header_r:
    if weeks:
        week = st.selectbox("Week", weeks, index=default_index, label_visibility="collapsed")
    view = st.pills("View", ["This Week", "Power Rankings"], default="This Week", label_visibility="collapsed")

# ---------------------------------------------------------------------------
# This Week -- Edge Board look: ticker, Best Bets grid, matchup cards.
# ---------------------------------------------------------------------------
if not weeks:
    st.info("No games loaded in this snapshot yet.")
elif view == "This Week":
    if True:
        games = snap["games_by_week"].get(str(week), [])

        # --- Ticker: every game's lean, scrolling marquee like the desktop board ---
        ticker_items = []
        for g in games:
            if g["confidence_score"] is None:
                continue
            away_last, home_last = g["away"].split()[-1].upper(), g["home"].split()[-1].upper()
            conf_pct = round(min(100, max(0, g["confidence_score"] * 10)))
            ticker_items.append(
                f'<div class="eb-ticker-item">{away_last} @ {home_last} &nbsp;<b>{g["confidence_label"]}</b> '
                f'<span class="eb-ticker-conf">{conf_pct}</span></div>'
            )
        ticker_html = "".join(ticker_items) or '<div class="eb-ticker-item">No graded games this week yet</div>'
        ticker_html += ticker_html
        st.markdown(f'<div class="eb-ticker"><div class="eb-ticker-track">{ticker_html}</div></div>', unsafe_allow_html=True)

        # --- Best Bets of the Week: top plays by lean score ---
        BEST_BET_THRESHOLD = 7.5
        best_bet_games = sorted(
            (g for g in games if g["confidence_score"] is not None and abs(g["confidence_score"] - 5.0) >= (BEST_BET_THRESHOLD - 5.0)),
            key=lambda g: abs(g["confidence_score"] - 5.0),
            reverse=True,
        )
        best_bet_cards = []
        for g in best_bet_games:
            away_abbr, home_abbr = g["away_abbr"], g["home_abbr"]
            away_last, home_last = g["away"].split()[-1].upper(), g["home"].split()[-1].upper()
            pick_txt = g["confidence_label"]
            score_pct = round(min(100, max(0, g["confidence_score"] * 10)))
            reason = g["auto_pick"]["note"] if g.get("auto_pick") else f"Lean index {g['confidence_score']:g}/10."
            best_bet_cards.append(f"""
<div class="bb-card">
  <div class="bb-matchup"><img class="bb-logo" src="{LOGO_URL.format(abbr=away_abbr)}" alt=""> {away_last} @ {home_last} <img class="bb-logo" src="{LOGO_URL.format(abbr=home_abbr)}" alt=""></div>
  <div class="bb-pick"><span class="pick-val">{pick_txt}</span></div>
  <div class="bb-reason">{reason}</div>
  <div class="confbar"><div class="confbar-fill" style="width:{score_pct}%"></div></div>
  <div class="confrow"><span>Lean Score</span><b>{g['confidence_score']:g}/10</b></div>
</div>""")

        st.markdown(
            f'<div class="eb-section-title">BEST BETS OF THE WEEK <span class="n">{len(best_bet_cards)} qualify at {BEST_BET_THRESHOLD:g}+ lean score</span></div>',
            unsafe_allow_html=True,
        )
        if best_bet_cards:
            st.markdown(f'<div class="bb-grid">{"".join(best_bet_cards)}</div>', unsafe_allow_html=True)
        else:
            st.markdown(f'<div class="empty-note">No plays clear the {BEST_BET_THRESHOLD:g}+ lean-score bar this week.</div>', unsafe_allow_html=True)

        st.markdown(
            f'<div class="eb-section-title">ALL GAMES <span class="n">{len(games)} games</span></div>',
            unsafe_allow_html=True,
        )

        all_cards = []
        for g in games:
            away_abbr, home_abbr = g["away_abbr"], g["home_abbr"]
            away_logo, home_logo = LOGO_URL.format(abbr=away_abbr), LOGO_URL.format(abbr=home_abbr)
            alert_kind = weather_alert_kind(g.get("weather"))
            is_best_bet = g in best_bet_games

            card = [f'<div class="eb-card{" best" if is_best_bet else ""}"'
                    + (f' style="border-color:{WEATHER_BORDER_COLORS[alert_kind]}"' if alert_kind else "") + '>']

            top_pills = []
            if is_best_bet:
                top_pills.append('<span class="pill pill-best">★ BEST BET</span>')
            if alert_kind:
                label = {"snow": "SNOW", "rain": "RAIN", "wind": "WIND 15+"}[alert_kind]
                top_pills.append(f'<span class="pill pill-weather {WEATHER_PILL_CLASS[alert_kind]}">{label}</span>')
            card.append(f'<div class="eb-card-top">{"".join(top_pills)}</div>')

            # Home always on the left, away on the right -- matches the desktop/print board's convention.
            card.append(
                '<div class="eb-team-names">'
                f'<div class="eb-team home"><img src="{home_logo}" alt="">{home_abbr}</div>'
                '<div class="eb-at">@</div>'
                f'<div class="eb-team away">{away_abbr}<img src="{away_logo}" alt=""></div>'
                "</div>"
            )

            spread_text = "—" if g["dk_spread"] is None else f"{home_abbr} {g['dk_spread']:+g}"
            total_text = "—" if g["dk_total"] is None else f'{g["dk_total"]:g}'
            card.append(f'<div class="dkline">DraftKings: <b>{spread_text}</b> &nbsp;·&nbsp; O/U <b>{total_text}</b></div>')

            card.append(f'<div class="eb-meta">{g["venue_label"]}</div>')

            if g.get("weather"):
                w = g["weather"]
                text = (w["text"] or "").replace("\n", " · ")
                weather_line = text if w.get("dome") else (w.get("icon", "") + " " + text).strip()
            else:
                weather_line = "Forecast pending"
            card.append(f'<div class="eb-meta">{weather_line}</div>')

            card.append(f'<div class="eb-meta">{g.get("referee_line") or "Referee · pending assignment"}</div>')

            card.append('<div class="section-label" style="text-align:center">PROJECTED SCORE</div>')
            if g["projection"]:
                p = g["projection"]
                card.append(
                    f'<div class="projscore"><span class="home">{home_abbr} {p["home_score"]}</span>'
                    f'<span class="dash"> — </span><span class="away">{away_abbr} {p["away_score"]}</span></div>'
                )
                model_spread = p["home_score"] - p["away_score"]
                spread_edge_row = (
                    f'<tr><td>Spread</td><td>{model_spread:+.1f}</td>'
                    f'<td class="edgeval">{abs(model_spread - g["dk_spread"]):.1f} pts</td></tr>'
                    if g["dk_spread"] is not None else
                    '<tr><td>Spread</td><td colspan="2" style="color:var(--text-dim)">pending</td></tr>'
                )
                total_edge_row = (
                    f'<tr><td>Total</td><td>{p["total"]:g}</td>'
                    f'<td class="edgeval">{abs(p["total"] - g["dk_total"]):.1f} pts</td></tr>'
                    if g["dk_total"] is not None else
                    '<tr><td>Total</td><td colspan="2" style="color:var(--text-dim)">pending</td></tr>'
                )
                card.append(f'<table class="edge"><tr><th></th><th>MODEL</th><th>EDGE</th></tr>{spread_edge_row}{total_edge_row}</table>')
            else:
                card.append(f'<div class="projscore dash">Lean index {g["confidence_score"]:g}/10 — {g["confidence_label"]}</div>')

            if g["home_bets"] is not None or g.get("over_bets") is not None:
                split_html = ['<div class="section-label">PUBLIC BETTING SPLITS</div><div class="splitrow">']
                if g["home_bets"] is not None:
                    spread_sharp = g.get("sharp_side")
                    spread_sharp_flag = f'<span class="sharp">⚡ Sharp: {spread_sharp.split()[-1].upper()}</span>' if spread_sharp else ""
                    split_html.append(
                        f'<div class="splitlabel"><span>SPREAD — BETS</span>{spread_sharp_flag}</div>'
                        f'<div class="bar"><div class="bar-a" style="width:{g["home_bets"]}%"></div><div class="bar-b" style="width:{100 - g["home_bets"]}%"></div></div>'
                        f'<div class="barnum">{home_abbr} {g["home_bets"]}% / {away_abbr} {g["away_bets"]}%</div>'
                        '<div class="splitlabel" style="margin-top:4px"><span>SPREAD — HANDLE</span></div>'
                        f'<div class="bar"><div class="bar-a" style="width:{g["home_handle"]}%"></div><div class="bar-b" style="width:{100 - g["home_handle"]}%"></div></div>'
                        f'<div class="barnum">{home_abbr} {g["home_handle"]}% / {away_abbr} {g["away_handle"]}%</div>'
                    )
                if g.get("over_bets") is not None:
                    total_sharp = g.get("sharp_total")
                    total_sharp_flag = f'<span class="sharp">⚡ Sharp: {total_sharp}</span>' if total_sharp else ""
                    split_html.append(
                        f'<div class="splitlabel" style="margin-top:4px"><span>TOTAL — BETS</span>{total_sharp_flag}</div>'
                        f'<div class="bar"><div class="bar-c" style="width:{g["over_bets"]}%"></div><div class="bar-d" style="width:{100 - g["over_bets"]}%"></div></div>'
                        f'<div class="barnum">OVER {g["over_bets"]}% / UNDER {g["under_bets"]}%</div>'
                        '<div class="splitlabel" style="margin-top:4px"><span>TOTAL — HANDLE</span></div>'
                        f'<div class="bar"><div class="bar-c" style="width:{g["over_handle"]}%"></div><div class="bar-d" style="width:{100 - g["over_handle"]}%"></div></div>'
                        f'<div class="barnum">OVER {g["over_handle"]}% / UNDER {g["under_handle"]}%</div>'
                    )
                split_html.append("</div>")
                card.append("".join(split_html))

            # Team-level OFF/DEF (the only grade possible for OL/DEF -- no public
            # per-player EPA exists for those positions) plus real individual
            # QB/RB/WR/TE grades for each team's current starters, when nflverse's
            # per-player feed has a qualifying sample for them.
            badges = grade_chip(f"{home_abbr} OFF", g["home_grade"]["offense"], g["home_grade"]["offense_colors"]) \
                + grade_chip(f"{home_abbr} DEF", g["home_grade"]["defense"], g["home_grade"]["defense_colors"]) \
                + grade_chip(f"{away_abbr} OFF", g["away_grade"]["offense"], g["away_grade"]["offense_colors"]) \
                + grade_chip(f"{away_abbr} DEF", g["away_grade"]["defense"], g["away_grade"]["defense_colors"])
            for side_abbr, positions in ((home_abbr, g.get("home_positions") or []), (away_abbr, g.get("away_positions") or [])):
                for pos in positions:
                    badges += grade_chip(f"{side_abbr} {pos['position']}", pos["grade"], pos["colors"])

            report_items = []
            if g["auto_pick"]:
                report_items.append(g["auto_pick"]["note"])
            if g["away_questionable"] or g["away_out"]:
                report_items.append(f'{away_abbr} injuries — Questionable: {g["away_questionable"] or "none"} · Out/IR: {g["away_out"] or "none"}')
            if g["home_questionable"] or g["home_out"]:
                report_items.append(f'{home_abbr} injuries — Questionable: {g["home_questionable"] or "none"} · Out/IR: {g["home_out"] or "none"}')
            if not report_items:
                report_items.append("No unusual factors on paper — this number is mostly the power-rating gap plus standard home field.")
            report_html = "".join(f"<li>{item}</li>" for item in report_items)

            card.append(
                f'<div class="confidence-row"><span>Lean Score <span class="leannote">(relative rank, not a win probability)</span></span><b>{g["confidence_score"]:g}/10</b></div>'
                f'<details class="eb-details"><summary>Game Report</summary>'
                f'<div class="eb-details-body"><ul class="eb-report-list">{report_html}</ul>'
                f'<div class="eb-grades">{badges}</div></div></details>'
            )

            card.append("</div>")
            all_cards.append("".join(card))

        st.markdown(f'<div class="eb-grid">{"".join(all_cards)}</div>', unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Power Rankings -- mirrors Board.render_rankings().
# ---------------------------------------------------------------------------
elif view == "Power Rankings":
    rankings = snap.get("rankings", {})
    st.caption(rankings.get("grade_guide", ""))
    st.caption(rankings.get("stats_state", ""))
    st.caption("Ranking sources refreshed: " + rankings.get("source_state", ""))
    st.subheader(f"Week {rankings.get('week', '?')} consensus power rankings")
    st.caption("Color scale: 10 dark green → neon green → yellow → orange → 1 red")

    for row in rankings.get("teams", []):
        cols = st.columns((1, 3, 1.4, 1, 1, 1, 3))
        color = row["rating_color_dark" if DARK else "rating_color_light"]
        cols[0].markdown(f"**{row['rank']}/{row['of']}**")
        cols[1].markdown(f"**{row['name'].upper()}**")
        cols[2].markdown(f'<span style="color:{color};font-weight:800">{row["consensus_rating"]:.1f} / 10</span>', unsafe_allow_html=True)
        cols[3].markdown(grade_chip("OFF", row["offense"], row["offense_colors"]), unsafe_allow_html=True)
        cols[4].markdown(grade_chip("DEF", row["defense"], row["defense_colors"]), unsafe_allow_html=True)
        move_color = {"up": "#1f9d55", "down": "#c62839", "flat": "#8a8a94"}[row["move_direction"]]
        cols[5].markdown(f'<span style="color:{move_color};font-weight:700">{row["move"]}</span>', unsafe_allow_html=True)
        sources_text = " · ".join(f"{label} {rank if rank else '—'}" for label, rank in row["sources"].items())
        cols[6].caption(sources_text)
        st.divider()

# ---------------------------------------------------------------------------
# TAB 3: My Picks -- mirrors Board.render_picks().
# ---------------------------------------------------------------------------
with tab_picks:
    picks = snap.get("picks", {})

    def picks_table(rows):
        if not rows:
            return None
        return [
            {
                "Wk": r["week"],
                "Matchup": f"{r['away'].split()[-1].upper()} @ {r['home'].split()[-1].upper()}",
                "Grades (away/home)": f"{r['away_grade_line']} / {r['home_grade_line']}",
                "Pick": (("★ " if r["bet_star"] else "") + r["pick_team"].split()[-1].upper()) if r["pick_team"] else "—",
                "Line": (f"{r['line']:+g}" if r["line"] is not None else "N/A"),
                "Result": r["result"] or "—",
                "O/U pick": (f"{r['ou']['pick'].upper()} ({r['ou']['result']})" if r["ou"] else "—"),
                "Algorithm pick": (f"{r['auto']['team']} ({r['auto']['result'] or 'pending'})" if r["auto"] else "—"),
            }
            for r in rows
        ]

    st.markdown(f"### ★ My bets record (ATS): {picks.get('bet_record', '0-0')}")
    table = picks_table(picks.get("bet_games", []))
    if table:
        st.dataframe(table, hide_index=True, width="stretch")
    else:
        st.caption("No starred bets yet.")

    st.markdown(f"### My picks record (ATS): {picks.get('pick_record', '0-0')} · O/U record: {picks.get('ou_record', '0-0')}")
    table = picks_table(picks.get("pick_games", []))
    if table:
        st.dataframe(table, hide_index=True, width="stretch")
    else:
        st.caption("No picks made yet.")

    st.markdown(f"### ★ Algorithm picks record (ATS): {picks.get('auto_record', '0-0')}")
    st.caption("What the automatic Lean Index would have picked in every game it took a real side on -- graded against the spread at the moment it first leaned, tracked purely for comparison against the picks above.")

st.caption("Lines, injuries, weather, rankings, and picks are research context. Verify current information before making any decision.")
