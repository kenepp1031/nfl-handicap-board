"""Injury adjustment: our own fixed scale, in spread points, applied straight
to the number and itemized per player in the Game Report.

Each listed player is worth INJURY_SCALE[position] points if he's a full-time
starter ruled Out, times STATUS_WEIGHT for his status, times his share of his
unit's snaps. A team's total is capped at MAX_TEAM_POINTS. project.py adds
(away_points - home_points) to the spread before the market blend, so the
points shown next to a player in the report are exactly what he cost his
team's side of the number.

The snap share has to be the player's share WHEN HEALTHY, which is why it is
taken over his recent APPEARANCES (games with a snap on file) reaching back
into last season, rather than over the team's last few calendar weeks. Sam
Darnold, 2026 week 2, is the case that exposed the difference: ruled Out with
a glute injury, his only game on file this season was the week 1 start he left
after 10% of the snaps. The calendar-week lookup read that 0.10 as his role and
charged SEA 5.0 x 1.0 x 0.10 = 0.5 points for losing its starting quarterback.
Over his appearances the same player reads 1.00 and costs the full 5.0.

Quarterback is the exception to the fixed scale, and is priced by
grading.qb_rating instead: the charge is the rating gap between the man who
would start and the best arm still available, times a coefficient measured
against the closing line. A flat 5.0 says Seattle losing Sam Darnold to Drew
Lock costs what Kansas City losing Patrick Mahomes to a third-stringer costs,
and it also bills a team twice when two quarterbacks are on the report although
only one of them was going to play.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from common import POSITION_GROUP
from grading.position_groups import _unit_share
from grading import qb_rating as qbr

# Spread points for a full-time starter ruled Out, by nflverse position code.
INJURY_SCALE = {
    # No "QB" entry on purpose: quarterbacks are priced per player by
    # grading.qb_rating, not off this flat scale. See the module docstring.
    "RB": 1.0, "FB": 0.25, "WR": 1.0, "TE": 0.75,
    "T": 1.0, "OT": 1.0, "G": 0.5, "OG": 0.5, "C": 0.5, "OL": 0.5,
    "DE": 1.0, "EDGE": 1.0, "OLB": 0.75, "DT": 0.5, "NT": 0.5, "DL": 0.5,
    "LB": 0.5, "ILB": 0.5, "MLB": 0.5,
    "CB": 1.0, "DB": 0.5, "S": 0.5, "SS": 0.5, "FS": 0.5, "SAF": 0.5,
}
# Fraction of the Out value each status is worth.
STATUS_WEIGHT = {
    "out": 1.0, "injured reserve": 1.0, "ir": 1.0, "pup": 1.0,
    "suspended": 1.0, "suspension": 1.0,
    "doubtful": 0.9,
    "questionable": 0.4,
}
HEALTHY_LOOKBACK_GAMES = 8  # appearances that define a player's role, newest first
MIN_GAMES_FOR_ROLE = 3      # below this on the current team, look at where he came from
MAX_TEAM_POINTS = 7.0       # cap on one team's total injury hit
MIN_POINTS_TO_NOTE = 0.1    # players below this still count, but aren't named
QB_GROUP = "QB"             # only one quarterback plays, so only the priciest one is charged

_SUFFIX_RE = re.compile(r"\s+(jr|sr|ii|iii|iv)\.?$")


def _norm(name: str) -> str:
    """ESPN and nflverse spell most names identically, but not punctuation
    or suffixes -- 'Michael Penix Jr.' vs 'Michael Penix'."""
    n = name.lower().replace(".", "").replace("'", "").strip()
    return _SUFFIX_RE.sub("", n)


def _shares_from(con, season: int, week: int, team: str | None) -> dict[str, dict]:
    """{normalized name: {name, position, shares}} over recent appearances, for one
    team or (team=None) for the whole league. Newest first, capped at
    HEALTHY_LOOKBACK_GAMES appearances per player.

    Rows with no snap are skipped rather than counted as a 0% game: they are the
    weeks the player was hurt, inactive or not yet on the roster, and they are
    exactly what must not define his role."""
    where = "((pgs.season=? AND pgs.week<?) OR pgs.season=?)"
    args: list = [season, week, season - 1]
    if team is not None:
        where = "pgs.team_abbr=? AND " + where
        args.insert(0, team)
    rows = con.execute(
        f"""SELECT p.name, p.position, pgs.snap_pct, pgs.offense_pct, pgs.defense_pct, pgs.st_pct
            FROM player_game_stats pgs JOIN players p ON p.player_id = pgs.player_id
            WHERE {where}
            ORDER BY pgs.season DESC, pgs.week DESC""",
        args,
    ).fetchall()
    seen: dict[tuple[str, str], dict] = {}
    for r in rows:
        pos_group = POSITION_GROUP.get(r["position"])
        if not pos_group or pos_group == "ST":
            continue
        # Keyed by (name, position group), not name alone. The league-wide pass
        # (team=None) merges every team's players into one dict, and 74 of the
        # names in this DB belong to more than one player -- Alex Smith the QB and
        # Alex Smith the TE, Josh Allen the QB and Josh Allen the LB, and
        # "Chris Smith II" normalizing onto "Chris Smith". Merging them let a
        # listed backup inherit a starter's snap share at a different position,
        # and five of those names are on the current injury report.
        entry = seen.setdefault((_norm(r["name"]), pos_group),
                                {"name": r["name"], "position": r["position"], "shares": []})
        if len(entry["shares"]) >= HEALTHY_LOOKBACK_GAMES:
            continue
        share = _unit_share(r, pos_group) or 0.0
        if share > 0:
            entry["shares"].append(share)
    return seen


def _lookup(shares: dict, key: str, pos_group: str | None):
    """The share entry for one listed player. Prefers his own position group, and
    falls back to a name-only match ONLY when that name is unambiguous -- so the
    injury report spelling a position differently from nflverse costs nothing,
    while a genuine two-player collision is never silently resolved to whichever
    row sorted first."""
    if pos_group is not None and (key, pos_group) in shares:
        return shares[(key, pos_group)]
    same_name = [v for (n, _g), v in shares.items() if n == key]
    return same_name[0] if len(same_name) == 1 else None


def _healthy_shares(con, team: str, season: int, week: int,
                    listed: dict[str, str | None]) -> dict[str, tuple[str, str, float]]:
    """{normalized name: (name, position, share when healthy)} for the listed players.

    `listed` maps each normalized name to the position the injury report gives it,
    which is only used to tell same-named players apart.

    A player's own team is the right place to read his job from, so that is the
    first source. Someone with almost no history here is either newly acquired or
    has been hurt all along, and in both cases this team's games describe him
    badly -- Kyler Murray, traded to Minnesota, had exactly one Vikings game on
    file and left it after 17% of the snaps, which read as a part-time player
    rather than the starting quarterback. For those players only, his share where
    he came from is used as well, and the larger of the two wins."""
    # `listed` is {normalized name: reported position}; the position is only used
    # to pick the right player when a name is shared (see _lookup).
    groups = {k: POSITION_GROUP.get((pos or "").upper()) for k, pos in listed.items()}
    here = _shares_from(con, season, week, team)
    thin = {}
    for k in listed:
        entry = _lookup(here, k, groups[k])
        if entry is None or len(entry["shares"]) < MIN_GAMES_FOR_ROLE:
            thin[k] = groups[k]
    resolved: dict[str, dict] = {}
    if thin:
        elsewhere = _shares_from(con, season, week, None)
        for k, grp in thin.items():
            other = _lookup(elsewhere, k, grp)
            if other and other["shares"]:
                mine = _lookup(here, k, grp)
                resolved[k] = {"name": other["name"], "position": other["position"],
                               "shares": (mine["shares"] if mine else []) + other["shares"]}
    out: dict[str, tuple[str, str, float]] = {}
    for k, grp in groups.items():
        entry = resolved.get(k) or _lookup(here, k, grp)
        if entry and entry["shares"]:
            out[k] = (entry["name"], entry["position"], max(entry["shares"]))
    return out


def team_injury_impact(con, team: str, season: int, week: int) -> dict:
    """Returns {"points": total spread points this team's injuries cost it,
    "players": [{name, position, status, share, points}, ...] sorted by points}."""
    listed, listed_pos = {}, {}
    for r in con.execute("SELECT player_name, position, status FROM injuries WHERE team_abbr=?", (team,)):
        key = _norm(r["player_name"])
        listed[key] = r["status"]
        listed_pos[key] = r["position"]
    if not listed:
        return {"points": 0.0, "players": [], "raw_points": 0.0, "capped": False}
    share_by_player = _healthy_shares(con, team, season, week, listed_pos)

    players = []
    for key, status in listed.items():
        entry = share_by_player.get(key)
        if not entry:
            continue
        name, position, share = entry
        if POSITION_GROUP.get(position) == QB_GROUP:
            continue  # priced by grading.qb_rating below, not by the fixed scale
        base = INJURY_SCALE.get(position)
        weight = STATUS_WEIGHT.get((status or "").lower())
        if base is None or weight is None or share <= 0:
            continue
        points = round(base * weight * share, 2)
        if points < 0.05:
            continue
        players.append({"name": name, "position": position, "status": status,
                        "share": share, "points": points})

    qb = qbr.qb_injury_points(con, team, season, week, listed, _norm)
    if qb and qb["points"] >= 0.05:
        players.append({
            "name": qb["starter"], "position": "QB", "status": qb["status"],
            "share": 1.0, "points": qb["points"],
            "note": (f'{qb["starter"]} rates {qb["starter_rating"]} against '
                     f'{qb["backup"]} at {qb["backup_rating"]}'),
        })
    players.sort(key=lambda p: -p["points"])

    # The cap is a cap on the TEAM, so it has to be a cap on the itemization too.
    # Capping only the total left the Game Report listing players that added up to
    # more than the number printed beside them -- San Francisco, 2026 week 2, showed
    # "SF -7.0" over twenty players summing to 8.17, and a reader adding up the list
    # got a different answer than the model used. Scale every line by the same
    # factor so the report reconciles and the docstring above stays true.
    raw = round(sum(p["points"] for p in players), 2)
    if raw > MAX_TEAM_POINTS and raw > 0 and players:
        scale = MAX_TEAM_POINTS / raw
        for p in players:
            p["capped_from"] = p["points"]
            p["points"] = round(p["points"] * scale, 2)
        # Rounding 20 lines to 2dp does not land on the cap by itself, so the
        # residual goes on the biggest line (players is sorted descending). Without
        # this the report still misses by a couple of hundredths.
        residual = round(MAX_TEAM_POINTS - sum(p["points"] for p in players), 2)
        players[0]["points"] = round(players[0]["points"] + residual, 2)
    total = min(MAX_TEAM_POINTS, raw)
    return {"points": total, "players": players, "raw_points": raw,
            "capped": raw > MAX_TEAM_POINTS}


if __name__ == "__main__":
    from db.db import connect
    season, week = int(sys.argv[1]), int(sys.argv[2])
    with connect() as con:
        for team in [r["team_id"] for r in con.execute("SELECT team_id FROM teams ORDER BY team_id")]:
            impact = team_injury_impact(con, team, season, week)
            if impact["players"]:
                names = ", ".join(f"{p['position']} {p['name']} ({p['status']}) -{p['points']:.1f}"
                                  + (f" [{p['note']}]" if p.get("note") else "")
                                  for p in impact["players"])
                print(f"{team}: -{impact['points']:.1f} pts -- {names}")
