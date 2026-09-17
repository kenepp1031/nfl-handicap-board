"""ESPN injury report -- dependency-free HTML table parse, ported from the old
app's InjuryParser. Used by the dashboard's Game Report to flag when a team's
starting QB or a top-snap RB/WR is banged up, instead of only ever attributing
a spread to schedule/weather/rivalry factors."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from common import TEAM_NAMES, fetch_text
from db.db import connect

ESPN_INJURIES = "https://www.espn.com/nfl/injuries"
NAME_TO_ABBR = {name: abbr for abbr, name in TEAM_NAMES.items()}


class InjuryParser(HTMLParser):
    """Small, dependency-free reader for ESPN's team injury tables."""
    def __init__(self):
        super().__init__()
        self.team = None
        self.in_row = False
        self.cell = False
        self.cells = []
        self.rows = []

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.in_row = True
            self.cells = []
        elif tag in ("td", "th") and self.in_row:
            self.cell = True

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self.cell = False
        elif tag == "tr":
            if self.team and len(self.cells) >= 4 and self.cells[0].upper() != "NAME":
                self.rows.append((self.team, *self.cells[:5]))
            self.in_row = False
            self.cells = []

    def handle_data(self, data):
        text = " ".join(data.split())
        if not text:
            return
        if text in NAME_TO_ABBR:
            self.team = text
        elif self.cell:
            self.cells.append(text)


def refresh() -> int:
    try:
        source = fetch_text(ESPN_INJURIES, timeout=30)
    except Exception:
        return 0
    parser = InjuryParser()
    parser.feed(source)
    timestamp = datetime.now(timezone.utc).isoformat(timespec="minutes")
    updated = 0
    with connect() as con:
        for team_name, player, position, _return_date, status, *rest in parser.rows:
            abbr = NAME_TO_ABBR.get(team_name)
            if not abbr:
                continue
            comment = rest[0] if rest else ""
            con.execute(
                """INSERT INTO injuries(team_abbr, player_name, position, status, comment, updated_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(team_abbr, player_name) DO UPDATE SET
                       position=excluded.position, status=excluded.status,
                       comment=excluded.comment, updated_at=excluded.updated_at""",
                (abbr, player, position, status, comment, timestamp),
            )
            updated += 1
    return updated


if __name__ == "__main__":
    from db.db import init_db
    init_db()
    print(f"Updated {refresh()} injury records")
