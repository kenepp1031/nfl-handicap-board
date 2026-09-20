"""Per-quarterback rating, and what a change at the position is worth in spread points.

Every QB who has taken 10+ dropbacks in a week already gets an individual
EPA-based grade from grading.player_grades -- backups included, 392 of them in
2025 alone across 64 different quarterbacks. Nothing consumed those grades. The
injury adjustment charged a flat 5.0 points for any QB ruled out, which says
Seattle losing Sam Darnold to Drew Lock costs exactly what Kansas City losing
Patrick Mahomes to a third-stringer costs. This module is what makes those two
different numbers.

A QB's rating is the mean of his last LOOKBACK individual grades, reaching back
through previous seasons, shrunk toward REPLACEMENT_GRADE by PRIOR_GAMES worth
of pseudo-observations so a backup with two good games doesn't read as a starter.
LOOKBACK is 24 rather than 8 because an eight-game window rates a backup off a
biased sample -- the handful of games he looked good enough to be left in -- and
produced Mitchell Trubisky above Josh Allen and Jake Browning above Joe Burrow.

WHAT THIS IS NOT: an edge. Announced quarterback changes are priced correctly by
the market. Over the 389 games in 2015-2025 where the previous week's snap leader
was INACTIVE and his replacement took 80%+ of the snaps -- a change knowable
before kickoff -- the side with the QB advantage beat the closing line by
+0.39 points (se 0.62, t = +0.6), and betting it went 48.3% ATS, getting worse as
the gap grew (45.9% at 8+ grade points). There is nothing here to bet.

An earlier version of this module claimed the opposite, from a test that defined
the starter as whoever led snaps in the game. That silently counted QBs knocked
out DURING the game: the team then underperforms the line, and the mid-game
injury gets relabelled as having started the backup. It measured -1.65 points
(t = -3.2) and 68% ATS, all of it information that does not exist before kickoff.
The filter above is what separates the two, and the effect does not survive it.

POINTS_PER_GRADE_POINT is therefore calibrated to the MARKET's own price, which
the test above shows to be the right one. Across the same 387 clean games:

    closing_spread ~ -0.66 + 1.037*our_pre_injury_spread + 0.194*qb_gap
                                                           se 0.022, t = +8.7

A 10-grade-point downgrade moves the line 1.9 points. That figure also matches
the value implied by the pipeline's own structure -- QB is 0.45 of the skill
composite, which is 0.75 of offense, which is half of overall, times the fitted
base coefficient, or about 0.18 -- so two independent routes agree.

The point of all this is accuracy, not edge: the projected score should say that
Seattle without Darnold is 1.4 points worse rather than a flat 5.0, because that
is what is true. See the nfl-2-0-no-edge-vs-close notes for everything else
tested against the close.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Mean career grade of quarterbacks with <=4 graded games in 2022-2025 -- the
# pool a team actually reaches into when the room is emptied. Measured at 35.3
# (median 35.2) against a league-wide QB mean of 49.8.
REPLACEMENT_GRADE = 35.3
LOOKBACK = 24     # individual grades behind a rating (~1.5 seasons of starts)
PRIOR_GAMES = 3   # pseudo-observations at replacement level, shrinking thin samples
RECENT_START_SHARE = 0.5  # snap share that counts as having started, not mopped up
POINTS_PER_GRADE_POINT = 0.19

# Statuses that mean the player is not available to take the snap. Anything
# softer (Questionable) leaves him on the depth chart as a possible starter.
UNAVAILABLE = {"out", "injured reserve", "ir", "pup", "suspended", "suspension", "doubtful"}
QUESTIONABLE_WEIGHT = 0.4  # matches injury_adjust.STATUS_WEIGHT


def qb_rating(con, player_id: str, season: int, week: int) -> tuple[float, int]:
    """(rating, games behind it) for one QB as of the week before `week`.

    Only `individual` grades count. A team_unit_proxy grade is the team's whole
    offense z-scored, so crediting it to the quarterback would rate a backup by
    how well the starter played. Rows rewritten by prior_season_blend are skipped
    for the same reason, and to keep this identical to what the coefficient above
    was fitted on -- the lookback reaches into earlier seasons, where unblended
    individual grades are plentiful."""
    rows = con.execute(
        """SELECT grade FROM player_grades
           WHERE player_id=? AND source='individual' AND grade IS NOT NULL
             AND ((season=? AND week<?) OR season<?)
           ORDER BY season DESC, week DESC LIMIT ?""",
        (player_id, season, week, season, LOOKBACK),
    ).fetchall()
    grades = [r["grade"] for r in rows]
    rating = (sum(grades) + REPLACEMENT_GRADE * PRIOR_GAMES) / (len(grades) + PRIOR_GAMES)
    return rating, len(grades)


def team_qb_room(con, team: str, season: int, week: int) -> list[dict]:
    """Every QB with a recent snap for this team, the man who would start if healthy
    first, each with his rating.

    Ordering is by best healthy snap share, with the most recent real start as the
    tiebreak. Both halves are needed and each one alone gets teams wrong:

    - Share alone cannot separate a current starter from a former one. Lamar
      Jackson and Tyler Huntley have both taken 100% of Baltimore's snaps in some
      game in the window, and sorting on share put Huntley first.
    - Recency alone loses the starter the moment he is hurt, which is exactly when
      this matters. Seattle's most recent snap leader is Drew Lock, because Sam
      Darnold left week 1 after 10% of the snaps -- the injury this whole module
      exists to price would have read as a non-event.

    Share is taken as the max over his appearances, so leaving a game hurt does not
    demote a starter, and RECENT_START_SHARE keeps a mop-up appearance from
    counting as a start."""
    rows = con.execute(
        """SELECT p.player_id, p.name, pgs.offense_pct, pgs.season, pgs.week
           FROM player_game_stats pgs JOIN players p ON p.player_id = pgs.player_id
           WHERE p.position='QB' AND pgs.team_abbr=? AND pgs.offense_pct > 0
             AND ((pgs.season=? AND pgs.week<?) OR pgs.season=?)""",
        (team, season, week, season - 1),
    ).fetchall()
    best: dict[str, dict] = {}
    for r in rows:
        entry = best.setdefault(r["player_id"], {"player_id": r["player_id"], "name": r["name"],
                                                 "share": 0.0, "last_start": (0, 0)})
        share = r["offense_pct"] or 0.0
        entry["share"] = max(entry["share"], share)
        if share >= RECENT_START_SHARE:
            entry["last_start"] = max(entry["last_start"], (r["season"], r["week"]))
    for entry in best.values():
        entry["rating"], entry["n_games"] = qb_rating(con, entry["player_id"], season, week)
    return sorted(best.values(),
                  key=lambda e: (-e["share"], [-x for x in e["last_start"]], -e["rating"]))


def qb_injury_points(con, team: str, season: int, week: int,
                     listed: dict, norm) -> dict | None:
    """Spread points this team loses at quarterback, or None if nothing changes.

    `listed` is {normalized name: status} from the injury report and `norm` is the
    caller's name normalizer, so both sides spell players the same way.

    The charge is the rating gap between the QB who would otherwise start and the
    best QB still available, priced at POINTS_PER_GRADE_POINT. A team whose backup
    is nearly as good as its starter loses almost nothing, which is the entire
    point of doing this per player. Losing a below-replacement starter is worth
    0.0, never a bonus -- the model does not get to claim an injury helped."""
    room = team_qb_room(con, team, season, week)
    if not room:
        return None
    starter = room[0]
    status = listed.get(norm(starter["name"]))
    if status is None:
        return None
    weight = 1.0 if (status or "").lower() in UNAVAILABLE else QUESTIONABLE_WEIGHT
    available = [q for q in room[1:]
                 if (listed.get(norm(q["name"])) or "").lower() not in UNAVAILABLE]
    backup = max(available, key=lambda q: q["rating"]) if available else None
    backup_rating = backup["rating"] if backup else REPLACEMENT_GRADE
    gap = max(0.0, starter["rating"] - backup_rating)
    return {
        "starter": starter["name"], "starter_rating": round(starter["rating"], 1),
        "backup": backup["name"] if backup else "replacement level",
        "backup_rating": round(backup_rating, 1),
        "status": status, "gap": round(gap, 1),
        "points": round(gap * POINTS_PER_GRADE_POINT * weight, 2),
    }


if __name__ == "__main__":
    from db.db import connect
    season, week = int(sys.argv[1]), int(sys.argv[2])
    with connect() as con:
        for r in con.execute("SELECT team_id FROM teams ORDER BY team_id"):
            room = team_qb_room(con, r["team_id"], season, week)
            if not room:
                continue
            print(f'{r["team_id"]:>4}: ' + "  |  ".join(
                f'{q["name"]} {q["rating"]:.1f} ({q["n_games"]}g, {q["share"]:.0%})' for q in room[:3]))
