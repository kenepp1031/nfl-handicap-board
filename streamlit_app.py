"""Public, mobile-friendly companion for the NFL handicapping board.

This app deliberately reads `web_snapshot.json` -- a pre-computed export of
everything the desktop app's Week / Power Rankings / My Picks views show,
produced locally by `export_web_snapshot.py` (which can import the desktop
.pyw and hit the network; Tkinter and outbound scraping can't run on
Streamlit Community Cloud). That makes this site static and always
reachable, independent of the authoring PC.

Read-only by design: there is no pick-saving or star-toggling here. That
stays desktop-only.
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


st.set_page_config(page_title="NFL Handicapping Board", page_icon="\U0001F3C8", layout="wide")

try:
    snap = load_snapshot()
except (FileNotFoundError, json.JSONDecodeError) as error:
    st.title("NFL handicapping board")
    st.error(f"Unable to load the published board: {error}")
    st.stop()

st.markdown(
    """
    <style>
    .chip{display:inline-block;padding:3px 9px;margin:0 3px 3px 0;border-radius:5px;font-weight:700;font-size:12px}
    .grade-chip{display:inline-block;min-width:46px;padding:4px 8px;margin:0 3px;text-align:center;border-radius:5px}
    .grade-chip .t{display:block;font-size:8px;font-weight:700;letter-spacing:.3px}
    .grade-chip .g{display:block;font-size:19px;font-weight:800;line-height:1.15}
    .metaline{font-size:12.5px;margin:2px 0}
    .metaline.gray{color:#8a8a94}
    .metaline.bad{color:#c62839;font-weight:700}
    .metaline.good{color:#1f9d55;font-weight:700}
    .metaline.warn{color:#c07a12;font-weight:700}
    .metaline.info{color:#3a7dc9;font-weight:700}
    .teamname{font-size:17px;font-weight:800}
    .kickoff{font-size:12px;font-weight:700;color:#8a8a94;text-align:center}
    .spreadbig{font-size:20px;font-weight:800;color:#c7132d;text-align:center}
    .totalmid{font-size:14px;font-weight:700;text-align:center}
    /* Matchup header: logos + names side by side, fixed flexbox -- deliberately
       NOT st.columns, which Streamlit stacks vertically below ~640px and is
       what made phone cards render as a long single-file dump. */
    .matchup-row{display:flex;align-items:center;justify-content:center;gap:6px;margin-bottom:6px}
    .matchup-team{display:flex;flex-direction:column;align-items:center;flex:1;min-width:0}
    .matchup-team img{width:40px;height:40px;object-fit:contain}
    .matchup-team .name{font-weight:800;font-size:12.5px;text-align:center;margin-top:2px;line-height:1.15}
    .matchup-at{font-weight:800;font-size:12px;color:#8a8a94;padding:0 2px}
    .predict-block{text-align:center;margin:4px 0 8px}
    .predict-score{font-size:19px;font-weight:800}
    .predict-total{font-size:12.5px;font-weight:700;color:#3a7dc9;margin-top:1px}
    .predict-market{font-size:11.5px;color:#8a8a94;margin-top:1px}
    .compare-row{display:flex;gap:6px;margin-top:4px}
    .compare-col{flex:1;min-width:0;text-align:center}
    @media (max-width: 480px){
      h1{font-size:26px !important}
      .matchup-team img{width:32px;height:32px}
      .matchup-team .name{font-size:11px}
      .predict-score{font-size:16px}
    }
    </style>
    """,
    unsafe_allow_html=True,
)

LOGO_URL = "https://a.espncdn.com/i/teamlogos/nfl/500/{abbr}.png"

# Border color per weather condition, checked in this priority order (snow beats
# rain beats wind-only) -- reuses the same alert text the desktop app already
# computes in weather_flags(), so "SNOW" / "RAIN" / "WIND" keywords line up
# with its FREEZING RAIN / SNOW / RAIN / WIND flag strings.
WEATHER_BORDER_COLORS = {"snow": "#e8e8ec", "rain": "#3a7dc9", "wind": "#8a8a94"}


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

st.title("NFL handicapping board")
st.caption("Public web companion · research only, not betting advice · mirrors the desktop app's Week / Power Rankings / My Picks views")
st.caption(f"Snapshot generated {snap.get('generated_at', 'unknown time')} · refresh by running `export_web_snapshot.py` on the desktop machine, then committing `web_snapshot.json`")


def grade_chip(label: str, grade: str, colors: dict, dark: bool) -> str:
    fg, bg = colors["dark" if dark else "light"]
    return f'<span class="grade-chip" style="color:{fg};background:{bg}"><span class="t">{label}</span><span class="g">{grade}</span></span>'


with st.sidebar:
    st.header("Board controls")
    dark_mode = st.toggle("Use dark-theme badge colors", value=False, help="Match colors to a dark Streamlit theme; leave off for the default light theme.")
    st.divider()
    st.caption(
        "This cloud edition uses the latest snapshot committed to GitHub. "
        "The owner refreshes it by running the desktop app, then "
        "`export_web_snapshot.py`, then pushing the updated file."
    )

tab_week, tab_rankings, tab_picks = st.tabs(["This Week", "Power Rankings", "My Picks"])

# ---------------------------------------------------------------------------
# TAB 1: This Week -- mirrors Board.render() / the print page's game cards.
# ---------------------------------------------------------------------------
with tab_week:
    weeks = snap.get("weeks", [])
    if not weeks:
        st.info("No games loaded in this snapshot yet.")
    else:
        default_index = weeks.index(snap.get("current_week", weeks[-1])) if snap.get("current_week") in weeks else len(weeks) - 1
        week = st.selectbox("Week", weeks, index=default_index)
        games = snap["games_by_week"].get(str(week), [])

        finals = sum(g["game_status"] == "FINAL" for g in games)
        starred = sum(g["bet_star"] for g in games)
        saved = sum(bool(g["pick_side"]) for g in games)
        with st.container(horizontal=True):
            st.metric("Games", len(games), border=True)
            st.metric("Final", finals, border=True)
            st.metric("Saved sides", saved, border=True)
            st.metric("Starred bets", starred, border=True)

        top_plays = snap.get("top_plays_by_week", {}).get(str(week), [])
        if top_plays:
            items = " &nbsp;·&nbsp; ".join(
                f"{p['fav']} {p['fav_spread']:+g} vs {p['opp']} ({p['score']:g}/10)" for p in top_plays[:5]
            )
            st.markdown(f"**MOST FAVORED GAMES THIS WEEK** &nbsp; {items}")

        st.subheader(f"Week {week} matchups")
        st.caption("Colored border = weather alert for that game: ⬜ snow · 🟦 rain · ⬛ wind 15+ mph")
        for g in games:
            alert_kind = weather_alert_kind(g.get("weather"))
            card_key = f"game_{g['event_id']}"
            if alert_kind:
                color = WEATHER_BORDER_COLORS[alert_kind]
                st.markdown(
                    f'<style>.st-key-{card_key} {{border:3px solid {color} !important}}</style>',
                    unsafe_allow_html=True,
                )
            with st.container(border=True, key=card_key):
                away_last, home_last = g["away"].split()[-1].upper(), g["home"].split()[-1].upper()
                away_logo, home_logo = LOGO_URL.format(abbr=g["away_abbr"]), LOGO_URL.format(abbr=g["home_abbr"])

                header = [f'<div class="kickoff">{g["kickoff_display"]}</div>']
                if g["rivalry"]:
                    header.append('<div class="metaline info" style="text-align:center">⚔ RIVALRY GAME</div>')
                header.append(
                    '<div class="matchup-row">'
                    f'<div class="matchup-team"><img src="{away_logo}" alt=""><div class="name">{away_last}</div></div>'
                    '<div class="matchup-at">@</div>'
                    f'<div class="matchup-team"><img src="{home_logo}" alt=""><div class="name">{home_last}</div></div>'
                    "</div>"
                )
                if g.get("weather"):
                    w = g["weather"]
                    cls = "warn" if w.get("alert") else "gray"
                    text = (w["text"] or "").replace("\n", " · ")
                    header.append(f'<div class="metaline {cls}" style="text-align:center;font-weight:700">{w.get("icon") or ""} {text}</div>')
                st.markdown("".join(header), unsafe_allow_html=True)

                # Predicted score / total, front and center -- this used to only show
                # up inside the collapsed "Game intel" expander.
                predict_html = ['<div class="predict-block">']
                spread_text = "—" if g["dk_spread"] is None else f"{home_last} {g['dk_spread']:+g}"
                total_text = "—" if g["dk_total"] is None else f'{g["dk_total"]:g}'
                predict_html.append(f'<div class="spreadbig">DraftKings: {spread_text}</div>')
                predict_html.append(f'<div class="totalmid">O/U {total_text}</div>')
                if g["projection"]:
                    p = g["projection"]
                    predict_html.append(
                        f'<div class="predict-score">Predicted: {away_last} {p["away_score"]} – {p["home_score"]} {home_last}</div>'
                    )
                    if g["dk_total"] is not None:
                        total_edge = p["total"] - g["dk_total"]
                        lean = "OVER" if total_edge > 0.5 else ("UNDER" if total_edge < -0.5 else "close to market")
                        predict_html.append(
                            f'<div class="predict-total">Predicted total {p["total"]:g} → {lean} lean ({total_edge:+.1f} vs market)</div>'
                        )
                    else:
                        predict_html.append(f'<div class="predict-total">Predicted total {p["total"]:g} (no market total yet)</div>')
                    predict_html.append(
                        f'<div class="predict-market">Your spread: {g["your_spread_text"]} · Lean {g["confidence_score"]:g}/10</div>'
                    )
                else:
                    predict_html.append(f'<div class="predict-total">Lean index {g["confidence_score"]:g}/10 — {g["confidence_label"]}</div>')
                predict_html.append("</div>")
                st.markdown("".join(predict_html), unsafe_allow_html=True)

                # Rank/grade/rest/injury-count comparison, side by side via flexbox
                # (not st.columns) so it stays side-by-side on a phone instead of
                # stacking away-then-home as one long scroll.
                def team_compare_html(team_key, rating_key, rank_key, grade_key, q_key, out_key, rest_key):
                    rating = g[rating_key]; grade = g[grade_key]; rank = g[rank_key]
                    rank_suffix = f" · #{rank}/{g['team_rank_count']}" if rating is not None and rank is not None else ""
                    rating_text = "—" if rating is None else f"{rating:.1f}{rank_suffix}"
                    chips = grade_chip("OFF", grade["offense"], grade["offense_colors"], dark_mode) + grade_chip("DEF", grade["defense"], grade["defense_colors"], dark_mode)
                    parts = [
                        '<div class="compare-col">',
                        f'<div class="metaline" style="font-weight:700">{rating_text}</div>',
                        f'<div style="margin:2px 0">{chips}</div>',
                    ]
                    if g[rest_key] is not None:
                        rest_cls = "info" if g[rest_key] >= 10 else "gray"
                        parts.append(f'<div class="metaline {rest_cls}">{g[rest_key]:g}d rest</div>')
                    parts.append("</div>")
                    return "".join(parts)

                compare = (
                    '<div class="compare-row">'
                    + team_compare_html("away", "away_rating", "away_rank", "away_grade", "away_questionable", "away_out", "away_rest")
                    + team_compare_html("home", "home_rating", "home_rank", "home_grade", "home_questionable", "home_out", "home_rest")
                    + "</div>"
                )
                st.markdown(compare, unsafe_allow_html=True)

                if g["neutral_site"] or g["home_noise"] == "elite" or g.get("referee_line"):
                    extra = []
                    noise = " \U0001F50A" if g["home_noise"] == "elite" else ""
                    label = g["venue_label"] if g["neutral_site"] else f"Home field: {g['venue_label']}"
                    extra.append(f'<div class="metaline gray" style="text-align:center">{label}{noise}</div>')
                    if g.get("referee_line"):
                        extra.append(f'<div class="metaline gray" style="text-align:center">{g["referee_line"]}</div>')
                    st.markdown("".join(extra), unsafe_allow_html=True)

                results_html = []
                if g["home_bets"] is not None:
                    results_html.append(
                        f'<div class="metaline gray" style="text-align:center">{home_last} {g["home_bets"]}% bets/{g["home_handle"]}% money'
                        f' · {away_last} {g["away_bets"]}% bets/{g["away_handle"]}% money</div>'
                    )
                if g["cover_text"]:
                    results_html.append(f'<div class="metaline" style="text-align:center;font-weight:700">FINAL {g["home_score"]}–{g["away_score"]} · {g["cover_text"]}</div>')
                if g["pick_result"] or g["pick_side"]:
                    pick_team = g["home"] if g["pick_side"] == "home" else g["away"]
                    result = g["pick_result"]
                    cls = "good" if result == "WON" else ("bad" if result == "LOST" else "gray")
                    glyph = "✓ " if result == "WON" else ("✗ " if result == "LOST" else "")
                    label = "BET" if g["bet_star"] else "PICK"
                    results_html.append(f'<div class="metaline {cls}" style="text-align:center">{glyph}Your {label.lower()}: {pick_team.split()[-1].upper()}{" (" + result + ")" if result else ""}</div>')
                if g["ou_pick"]:
                    result = g["ou_result"]
                    cls = "good" if result == "WON" else ("bad" if result == "LOST" else "gray")
                    results_html.append(f'<div class="metaline {cls}" style="text-align:center">Your O/U pick ({g["ou_pick"].upper()}): {result or "PENDING"}</div>')
                if g["auto_pick"]:
                    auto = g["auto_pick"]
                    result = auto["result"]
                    cls = "good" if result == "WON" else ("bad" if result == "LOST" else "gray")
                    glyph = "✓ " if result == "WON" else ("✗ " if result == "LOST" else "")
                    results_html.append(f'<div class="metaline {cls}" style="text-align:center;font-weight:700">{glyph}★ Algorithm pick: {auto["team"].split()[-1].upper()}</div>')
                if results_html:
                    st.markdown("".join(results_html), unsafe_allow_html=True)
                if g["auto_pick"]:
                    st.caption(g["auto_pick"]["note"])

                with st.expander("Game intel (lean + full injury report)"):
                    st.markdown(f"**LEAN INDEX:** {g['confidence_score']:g} / 10 — {g['confidence_label']}")
                    st.caption("Directional only -- not a win probability or a betting recommendation.")
                    if g.get("referee"):
                        ref = g["referee"]
                        over_text = f"{ref['over_pct']:.1f}%" if ref["over_pct"] is not None else "n/a"
                        games_text = f"{ref['games']} games" if ref["games"] is not None else "no career sample yet"
                        if ref["home_ats_pct"] is not None:
                            st.markdown(f"**On the call:** {ref['referee'].upper()} · career Home ATS {ref['home_ats_pct']:.1f}% ({games_text}) · Over {over_text}")
                        else:
                            st.markdown(f"**On the call:** {ref['referee'].upper()} · {games_text}")
                    st.divider()
                    st.markdown(f"**{g['away'].upper()} INJURY REPORT**  \nQuestionable: {g['away_questionable'] or 'none listed'}  \nOut / IR: {g['away_out'] or 'none listed'}")
                    st.markdown(f"**{g['home'].upper()} INJURY REPORT**  \nQuestionable: {g['home_questionable'] or 'none listed'}  \nOut / IR: {g['home_out'] or 'none listed'}")

# ---------------------------------------------------------------------------
# TAB 2: Power Rankings -- mirrors Board.render_rankings().
# ---------------------------------------------------------------------------
with tab_rankings:
    rankings = snap.get("rankings", {})
    st.caption(rankings.get("grade_guide", ""))
    st.caption(rankings.get("stats_state", ""))
    st.caption("Ranking sources refreshed: " + rankings.get("source_state", ""))
    st.subheader(f"Week {rankings.get('week', '?')} consensus power rankings")
    st.caption("Color scale: 10 dark green → neon green → yellow → orange → 1 red")

    for row in rankings.get("teams", []):
        cols = st.columns((1, 3, 1.4, 1, 1, 1, 3))
        color = row["rating_color_dark" if dark_mode else "rating_color_light"]
        cols[0].markdown(f"**{row['rank']}/{row['of']}**")
        cols[1].markdown(f"**{row['name'].upper()}**")
        cols[2].markdown(f'<span style="color:{color};font-weight:800">{row["consensus_rating"]:.1f} / 10</span>', unsafe_allow_html=True)
        cols[3].markdown(grade_chip("OFF", row["offense"], row["offense_colors"], dark_mode), unsafe_allow_html=True)
        cols[4].markdown(grade_chip("DEF", row["defense"], row["defense_colors"], dark_mode), unsafe_allow_html=True)
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
