"""Standalone DraftKings line/public-split refresh for the web snapshot pipeline.

Same logic as the desktop app's refresh_public_lines()/_public_lines_worker()
(reused via import, not duplicated), run headlessly so the published site can
pick up a current spread, total, and betting-split percentages -- including
Over/Under splits -- without opening the Tkinter app.

Run:  python refresh_splits.py [week]
(defaults to every week present in the local database)
"""
from __future__ import annotations
import importlib.util
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen

APP_DIR = Path(__file__).resolve().parent
DESKTOP_PATH = APP_DIR / "NFL_Handicapping_Desktop.pyw"
DB_PATH = APP_DIR / "nfl_handicapping_board.db"


def _load_desktop_module():
    spec = importlib.util.spec_from_file_location("nfl_desktop_calc", DESKTOP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(week: int | None = None):
    D = _load_desktop_module()
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    columns = {r[1] for r in con.execute("PRAGMA table_info(games)")}
    for name in ("over_bets", "over_handle", "under_bets", "under_handle"):
        if name not in columns:
            con.execute(f"ALTER TABLE games ADD COLUMN {name} INTEGER")

    weeks = [week] if week is not None else [r["week"] for r in con.execute("SELECT DISTINCT week FROM games ORDER BY week")]

    def load(market, page_number):
        url = D.DK_SPLITS + f"&tb_emt={market}&tb_page={page_number}"
        request = Request(url, headers={"User-Agent": "Mozilla/5.0 (personal NFL board)"})
        with urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", "ignore")
        import re, html
        return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw)))

    updated = skipped = 0
    for wk in weeks:
        try:
            spreads = " ".join(load("Spread", page) for page in range(1, 5))
            totals = " ".join(load("Total", page) for page in range(1, 5))
        except Exception as ex:
            print(f"Week {wk}: DK Network fetch failed ({ex})")
            continue

        games = con.execute("SELECT * FROM games WHERE week=?", (wk,)).fetchall()
        for game in games:
            values = D.Board._draftkings_game_values(None, spreads, totals, game["away"], game["home"])
            if not values:
                skipped += 1
                continue
            checked = datetime.now().isoformat(timespec="minutes")
            line_values, split_values = values[:6], values[6:]
            con.execute(
                "UPDATE games SET dk_spread=?,dk_total=?,home_bets=?,home_handle=?,away_bets=?,away_handle=?,"
                "line_source=?,line_checked=?,over_bets=?,over_handle=?,under_bets=?,under_handle=? WHERE event_id=?",
                (*line_values, "DraftKings", checked, *split_values, game["event_id"]),
            )
            con.execute(
                "INSERT OR IGNORE INTO line_history(event_id,checked_at,home_spread,total,home_bets,home_handle,away_bets,away_handle) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (game["event_id"], checked, *line_values),
            )
            con.commit()
            updated += 1
            print(f"  Week {wk}: {game['home']} vs {game['away']} — updated")

    print(f"\nDraftKings splits refreshed for {updated} game(s), skipped {skipped} (not found on DK Network's board).")


if __name__ == "__main__":
    import sys
    main(int(sys.argv[1]) if len(sys.argv) > 1 else None)
