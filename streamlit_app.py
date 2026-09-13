"""Public, mobile-friendly companion for the NFL handicapping board.

This app deliberately reads the checked-in SQLite snapshot rather than the Windows
desktop UI.  That makes it suitable for Streamlit Community Cloud: the site can be
open even when the authoring PC is off.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import streamlit as st


APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "nfl_handicapping_board.db"


@st.cache_data(ttl=300)
def load_snapshot() -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Load the published board snapshot with no write access required."""
    if not DB_PATH.exists():
        raise FileNotFoundError("The published board snapshot is missing.")
    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        games = [dict(row) for row in connection.execute(
            "SELECT * FROM games ORDER BY week, kickoff"
        )]
        teams = [dict(row) for row in connection.execute(
            "SELECT * FROM teams ORDER BY consensus_rating DESC, name"
        )]
        injuries = [dict(row) for row in connection.execute(
            "SELECT * FROM injuries ORDER BY team, player"
        )]
        weather = [dict(row) for row in connection.execute("SELECT * FROM weather")]
    return games, teams, injuries, weather


def kickoff_text(value: str) -> str:
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        hour = moment.hour % 12 or 12
        return f"{moment:%a, %b} {moment.day} · {hour}:{moment:%M %p}"
    except ValueError:
        return value


def team_rating(team: str, ratings: dict[str, float | None]) -> str:
    value = ratings.get(team)
    return "—" if value is None else f"{value:.1f}"


def injury_summary(team: str, injuries: list[dict]) -> str:
    people = [row for row in injuries if row["team"] == team]
    if not people:
        return "No listed injuries in this snapshot"
    return " · ".join(
        f"{row['player']} ({row.get('position') or '—'}: {row.get('status') or 'status unavailable'})"
        for row in people[:4]
    )


def weather_summary(game: dict, weather_by_event: dict[str, dict]) -> str:
    row = weather_by_event.get(game["event_id"])
    if not row:
        return "Weather not available in this snapshot"
    if row.get("dome"):
        return "Dome / roof assumed closed"
    parts = []
    if row.get("temperature") is not None:
        parts.append(f"{row['temperature']:.0f}°F")
    if row.get("wind") is not None:
        parts.append(f"wind {row['wind']:.0f} mph")
    if row.get("alert"):
        parts.append(str(row["alert"]))
    return " · ".join(parts) if parts else "Weather data pending"


st.set_page_config(page_title="NFL Handicapping Board", page_icon="🏈", layout="wide")
st.title("NFL handicapping board")
st.caption("Public web companion · research only, not betting advice")

try:
    games, teams, injuries, weather = load_snapshot()
except (sqlite3.Error, FileNotFoundError) as error:
    st.error(f"Unable to load the published board: {error}")
    st.stop()

ratings = {row["name"]: row.get("consensus_rating") for row in teams}
weather_by_event = {row["event_id"]: row for row in weather}
weeks = sorted({row["week"] for row in games})

with st.sidebar:
    st.header("Board controls")
    week = st.selectbox("Week", weeks, index=len(weeks) - 1 if weeks else 0)
    show_injuries = st.toggle("Show injury notes", value=True)
    st.divider()
    st.caption(
        "This cloud edition uses the latest database snapshot committed to the site. "
        "Update the repository to publish new lines, scores, rankings, or picks."
    )

week_games = [row for row in games if row["week"] == week]
finals = sum(row.get("game_status") == "FINAL" for row in week_games)
selected = sum(bool(row.get("pick_side")) for row in week_games)
starred = sum(bool(row.get("bet_star")) for row in week_games)
with st.container(horizontal=True):
    st.metric("Games", len(week_games), border=True)
    st.metric("Final", finals, border=True)
    st.metric("Saved sides", selected, border=True)
    st.metric("Starred bets", starred, border=True)

st.subheader(f"Week {week} matchups")
for game in week_games:
    home, away = game["home"], game["away"]
    with st.container(border=True):
        st.caption(kickoff_text(game["kickoff"]))
        left, middle, right = st.columns((4, 3, 4))
        with left:
            st.markdown(f"### {away}")
            st.caption(f"Consensus rating: {team_rating(away, ratings)}")
            if show_injuries:
                st.caption(injury_summary(away, injuries))
        with middle:
            if game.get("dk_spread") is None:
                st.metric("Market spread", "—")
            else:
                st.metric("Market spread", f"{home.split()[-1]} {game['dk_spread']:+g}")
            st.metric("Total", "—" if game.get("dk_total") is None else f"O/U {game['dk_total']:g}")
            if game.get("game_status") == "FINAL":
                st.caption(f"Final: {away} {game.get('away_score', '—')} · {home} {game.get('home_score', '—')}")
            elif game.get("pick_side"):
                pick = home if game["pick_side"] == "home" else away
                st.caption(f"Saved side: {pick}" + (" ★" if game.get("bet_star") else ""))
            if game.get("home_bets") is not None:
                st.caption(f"Public bets: {home.split()[-1]} {game['home_bets']}% · {away.split()[-1]} {game['away_bets']}%")
        with right:
            st.markdown(f"### {home}")
            st.caption(f"Consensus rating: {team_rating(home, ratings)}")
            st.caption(weather_summary(game, weather_by_event))
            if show_injuries:
                st.caption(injury_summary(home, injuries))

st.subheader("Consensus rankings")
ranking_rows = [
    {"Rank": index, "Team": row["name"], "Consensus": row["consensus_rating"]}
    for index, row in enumerate((row for row in teams if row.get("consensus_rating") is not None), start=1)
]
st.dataframe(ranking_rows, hide_index=True, width="stretch")

st.caption("Lines, injuries, weather, rankings, and picks are research context. Verify current information before making any decision.")
