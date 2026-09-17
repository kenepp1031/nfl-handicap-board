"""Public, mobile-friendly companion for the NFL 2.0 board.

The batch pipeline (main.py) already writes one fully self-contained static
page -- dashboard/dashboard.html, inline CSS, no client-side fetch/polling --
so this file does almost nothing: it reads that file and hands it to
st.iframe(). Same pattern as the sibling NCAAF board.

The published board refreshes itself: every `python main.py` run commits the
re-rendered dashboard/dashboard.html and pushes it (see publish.py), and
Streamlit Cloud redeploys on push. nfl_2_0.db is NOT deployed -- the page
needs only the rendered HTML.
"""
from __future__ import annotations

from pathlib import Path

import streamlit as st

APP_DIR = Path(__file__).resolve().parent
DASHBOARD_PATH = APP_DIR / "dashboard" / "dashboard.html"

st.set_page_config(page_title="NFL 2.0 Board", page_icon="🏈", layout="wide")

# layout="wide" alone still leaves Streamlit's own content column padded and
# capped short of the viewport (visibly narrower than opening dashboard.html
# directly), since that padding lives outside what width="stretch" on
# st.iframe controls. Strip it so the embedded page gets the full width.
st.markdown(
    "<style>.block-container{padding-left:0.5rem;padding-right:0.5rem;max-width:100%}</style>",
    unsafe_allow_html=True,
)

if not DASHBOARD_PATH.exists():
    st.error(
        "No published board found (dashboard/dashboard.html is missing). "
        "Run `python main.py` locally; it commits + pushes dashboard/dashboard.html."
    )
    st.stop()

# Passing the Path directly (not its text) lets st.iframe recognize it as a
# local HTML file and auto-size the iframe to the rendered page's height.
st.iframe(DASHBOARD_PATH, width="stretch", height="content")
