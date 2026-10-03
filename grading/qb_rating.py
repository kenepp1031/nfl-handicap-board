"""Per-quarterback rating, and what a change at the position is worth in spread points.

Every QB who has taken 10+ dropbacks in a week already gets an individual
EPA-based grade from grading.player_grades -- backups included, 392 of them in
2025 alone across 64 different quarterbacks. Nothing consumed those grades. The
injury adjustment charged a flat 5.0 points for any QB ruled out, which says
Seattle losing Sam Darnold to Drew Lock costs exactly what Kansas City losing
Patrick Mahomes to a third-stringer costs. This module is what makes those two
different numbers.

THE RATING. A QB's rating is a recency-weighted mean of his individual grades
over the last MAX_SEASONS_BACK seasons, each season back worth DECAY_PER_SEASON
of the one before, shrunk toward REPLACEMENT_GRADE by PRIOR_GAMES worth of
pseudo-observations so a backup with two good games doesn't read as a starter.

The first version of this took the last LOOKBACK grades with no decay and no
season limit, and that is exactly how a backup ended up rated like a starter: a
journeyman plays five games a year, so his 24-game window reached back to the
seasons he was somebody's starter. Marcus Mariota was rated off games back to
2018, Jameis Winston off 2019, and both landed within two points of the men they
back up. With a three-season window and a 0.5 decay Winston reads 43 rather than
51 and Josh Johnson 39 rather than 46, while established starters barely move.
The decayed rating is also the more informative one: the market's response per
rating point (below) has a t-statistic of 11.8-12.0 against 10.0 for the undecayed
version, at 0.6 and 0.4 per season alike; 0.5 is shipped as the midpoint.

Current-season games count. grading.player_grades.blend_prior_season rewrites
weeks 1-6 as `prior_season_blend` rows and used to leave no way to tell whether
the number underneath was the player's own grade or a team-unit proxy, so those
rows were skipped and no quarterback's rating moved before week 7. The blend now
records `current_season_source`, and a blended row whose underlying grade was
individual counts here at that underlying grade. A proxy grade never counts: it
is the team's whole offense z-scored, and crediting it to the quarterback would
rate a backup by how well the starter played.

WHAT A STARTER IS WORTH. Measured over the 2,677 graded games in 2015-2025 with
a closing line, of which 336 had one team's previous-week snap leader inactive
and a replacement taking 80%+ of the snaps -- a change knowable before kickoff:

    closing_spread ~ -0.27 + 1.05*our_pre_injury_spread + 0.20*qb_gap + 2.0*starter_out
                                                          se 0.021       se 0.21
                                                          t = +9.4       t = +9.4

The market charges a flat premium of about two points for losing whoever was
starting, and on top of that about a fifth of a point per rating point between
him and his replacement. Bucketed by gap the same shape reads straight off the
data: sides whose backup rates HIGHER than the starter were charged -0.3, gaps of
0-4 were charged 2.3, 4-8 3.2, 8-12 4.0, 12+ 4.4. So STARTER_PREMIUM plus
POINTS_PER_GRADE_POINT times the gap, floored at zero, is the market's own price
-- and it is not only the market's: regressing the ACTUAL MARGIN the same way
gives +0.22 per rating point (t = 2.9) and +1.5 for the starter-out dummy
(t = 2.0). The premium shows up in results, not just in the line.

WHAT THIS IS NOT: an edge. The market prices announced quarterback changes
correctly, and an earlier pass over the same games found betting the side with
the QB advantage went 48.3% ATS, getting worse as the gap grew. The point of all
this is accuracy: the projected score should say that Washington without Jayden
Daniels is two points worse, because that is what is true, rather than the 0.4
that a rating gap alone produced when the fixed scale said nothing about the
starter himself. See the nfl-2-0-no-edge-vs-close notes for everything else
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
LOOKBACK = 24            # most individual grades that can sit behind a rating
MAX_SEASONS_BACK = 3     # a game older than this says nothing about who he is now
DECAY_PER_SEASON = 0.5   # weight of a game one season further back, relative to the season after it
PRIOR_GAMES = 3          # pseudo-observations at replacement level, shrinking thin samples
RECENT_START_SHARE = 0.5  # snap share that counts as having started, not mopped up
STARTER_GAMES = 4         # the team's last N games decide who its starter is (weights 1, .75, .5, .25)

# The market's price for a change at quarterback (see the module docstring):
# a flat premium for losing the starter, plus a per-rating-point term for how
# far the replacement sits below him. Floored at zero in qb_injury_points, so a
# starter whose backup rates higher costs nothing rather than paying a bonus.
STARTER_PREMIUM = 2.0
POINTS_PER_GRADE_POINT = 0.20

# Statuses that mean the player is not available to take the snap. Anything
# softer (Questionable) leaves him on the depth chart as a possible starter.
UNAVAILABLE = {"out", "injured reserve", "ir", "pup", "suspended", "suspension", "doubtful"}
QUESTIONABLE_WEIGHT = 0.4  # matches injury_adjust.STATUS_WEIGHT


def qb_rating(con, player_id: str, season: int, week: int) -> tuple[float, int]:
    """(rating, games behind it) for one QB as of the week before `week`.

    Only individual grades count -- either a row graded `individual` outright, or
    an early-season `prior_season_blend` row whose recorded underlying grade was
    individual, taken at that underlying grade. Rows the blend built from a proxy
    or from nothing at all are skipped: a team_unit_proxy grade is the team's whole
    offense z-scored, and crediting it to the quarterback would rate a backup by
    how well the starter played. Each season further back is worth
    DECAY_PER_SEASON of the one after it, and nothing older than MAX_SEASONS_BACK
    seasons counts at all."""
    rows = con.execute(
        """SELECT season, source, grade,
                  json_extract(grade_components_json, '$.current_season_grade') AS raw_grade
           FROM player_grades
           WHERE player_id=? AND ((season=? AND week<?) OR (season<? AND season>=?))
             AND (source='individual'
                  OR (source='prior_season_blend'
                      AND json_extract(grade_components_json, '$.current_season_source')='individual'))
           ORDER BY season DESC, week DESC LIMIT ?""",
        (player_id, season, week, season, season - MAX_SEASONS_BACK, LOOKBACK),
    ).fetchall()
    num, den, n = REPLACEMENT_GRADE * PRIOR_GAMES, float(PRIOR_GAMES), 0
    for r in rows:
        grade = r["grade"] if r["source"] == "individual" else r["raw_grade"]
        if grade is None:
            continue
        weight = DECAY_PER_SEASON ** (season - r["season"])
        num += weight * grade
        den += weight
        n += 1
    return num / den, n


def team_qb_room(con, team: str, season: int, week: int) -> list[dict]:
    """Every QB with a recent snap for this team, the man who would start if healthy
    first, each with his rating.

    "Would start" is read off the team's last STARTER_GAMES games: each QB's share
    of the offensive snaps in those games, weighted 1, .75, .5, .25 from the most
    recent back, and the highest total is the starter. The rating breaks ties.

    This replaced ordering by best-ever share with the most recent start as the
    tiebreak, which fails in exactly the case the module exists for. Jaxson Dart
    left New York's second game of 2026 after 12% of the snaps and Jameis Winston
    took the other 88%; that relief appearance counted as a "start", so it made
    Winston the starter, Dart's move to injured reserve two days later read as a
    backup being hurt, and the Giants were charged nothing. Over the last four games
    Dart holds 1.62 of the weighted share to Winston's 0.88, so he is the starter and
    the charge is priced from him.

    The window is deliberately short. A team that has played its backup for three
    or four games already carries that in its rating -- the rolling window behind
    overall_score IS those games -- and charging the original starter's absence on
    top would bill the same loss twice. Once the backup owns the recent snaps, he is
    the starter for this purpose and there is nothing left to charge."""
    rows = con.execute(
        """SELECT p.player_id, p.name, pgs.offense_pct, pgs.season, pgs.week
           FROM player_game_stats pgs JOIN players p ON p.player_id = pgs.player_id
           WHERE p.position='QB' AND pgs.team_abbr=? AND pgs.offense_pct > 0
             AND ((pgs.season=? AND pgs.week<?) OR pgs.season=?)""",
        (team, season, week, season - 1),
    ).fetchall()
    recent_games = sorted({(r["season"], r["week"]) for r in rows}, reverse=True)[:STARTER_GAMES]
    recency = {key: 1.0 - i / STARTER_GAMES for i, key in enumerate(recent_games)}
    best: dict[str, dict] = {}
    for r in rows:
        entry = best.setdefault(r["player_id"], {"player_id": r["player_id"], "name": r["name"],
                                                 "share": 0.0, "recent_share": 0.0, "season_snaps": 0.0,
                                                 "last_start": (0, 0)})
        share = r["offense_pct"] or 0.0
        entry["share"] = max(entry["share"], share)
        entry["recent_share"] += share * recency.get((r["season"], r["week"]), 0.0)
        if r["season"] == season:
            entry["season_snaps"] += share  # who has actually taken this season's snaps
        if share >= RECENT_START_SHARE:
            entry["last_start"] = max(entry["last_start"], (r["season"], r["week"]))
    for entry in best.values():
        entry["rating"], entry["n_games"] = qb_rating(con, entry["player_id"], season, week)
    return sorted(best.values(), key=lambda e: (-e["recent_share"], -e["rating"]))


def qb_injury_points(con, team: str, season: int, week: int,
                     listed: dict, norm) -> dict | None:
    """Spread points this team loses at quarterback, or None if nothing changes.

    `listed` is {normalized name: status} from the injury report and `norm` is the
    caller's name normalizer, so both sides spell players the same way.

    The charge is STARTER_PREMIUM for losing the man who would start, plus
    POINTS_PER_GRADE_POINT for every rating point he sits above his replacement
    -- the market's measured price, see the module docstring. A backup who rates
    above his starter eats into the premium but never turns it into a bonus: the
    model does not get to claim an injury helped.

    The replacement is the available backup who has actually taken this season's
    snaps, which is the team's own depth chart speaking; only when no backup has
    played yet is it the best-rated arm on the roster. Ratings a tenth of a point
    apart are noise, and reading them as a depth chart named Russell Wilson as
    New York's replacement for Jaxson Dart in the week Jameis Winston was starting."""
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
    played = [q for q in available if q["season_snaps"] > 0]
    if played:
        backup = max(played, key=lambda q: q["season_snaps"])
    else:
        backup = max(available, key=lambda q: q["rating"]) if available else None
    backup_rating = backup["rating"] if backup else REPLACEMENT_GRADE
    gap = starter["rating"] - backup_rating
    gap_points = POINTS_PER_GRADE_POINT * gap
    points = max(0.0, STARTER_PREMIUM + gap_points) * weight
    return {
        "starter": starter["name"], "starter_rating": round(starter["rating"], 1),
        "backup": backup["name"] if backup else "replacement level",
        "backup_rating": round(backup_rating, 1),
        "status": status, "gap": round(gap, 1),
        "premium": round(STARTER_PREMIUM * weight, 2),
        "gap_points": round(gap_points * weight, 2),
        "points": round(points, 2),
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
                f'{q["name"]} {q["rating"]:.1f} ({q["n_games"]}g, recent {q["recent_share"]:.2f})' for q in room[:3]))
