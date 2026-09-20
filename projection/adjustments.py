"""Situational adjustment layer (framework §3.6). Each factor is a pure
function, applied additively (in home-team-favoring points) to the base
spread. Independently toggleable via the `enabled` dict so backtest/report.py
can isolate each one's marginal value (framework §3.9)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from common import NOISE_ELITE, NOISE_LOUD

# "ref" ships OFF: the officiating table is a current-week scrape (32 rows), so the
# crew lean fired on 13 of 2,654 graded games and can never be validated historically.
# It stays wired up so a real crew-history backfill can switch it back on.
DEFAULT_ENABLED = {
    "hfa": True, "weather": True, "rest": True, "rivalry": True, "ref": False, "injury": True,
}

LEAGUE_AVG_HFA = 1.8
NOISE_ELITE_BONUS = 1.2
NOISE_LOUD_BONUS = 0.5


def hfa_adjust(home_abbr: str) -> float:
    if home_abbr in NOISE_ELITE:
        return LEAGUE_AVG_HFA + NOISE_ELITE_BONUS
    if home_abbr in NOISE_LOUD:
        return LEAGUE_AVG_HFA + NOISE_LOUD_BONUS
    return LEAGUE_AVG_HFA


def weather_adjust(wind_mph: float | None, precip_type: str | None, temp_f: float | None) -> tuple[float, float]:
    """Returns (spread_adj, total_adj). NOTE: the spread leg is always 0.0 -- this
    factor moves the total only. Anything reading `weather_adj` off `projections`
    or `backtest_log` is reading a column that is zero on every row by design. Wind suppresses passing efficiency and
    total the most; precip/cold matter less than commonly assumed but still
    shave a little off the total. Spread impact is treated as negligible
    (affects both teams) except at extreme wind, which very slightly favors
    the run-heavier/less pass-dependent team -- left at 0 here since we don't
    yet track team pass-rate tendency; total is where weather actually moves."""
    total_adj = 0.0
    if wind_mph is not None and wind_mph >= 20:
        total_adj -= 3.0
    elif wind_mph is not None and wind_mph >= 15:
        total_adj -= 1.5
    if precip_type and precip_type not in ("none", "", None):
        total_adj -= 1.0
    if temp_f is not None and temp_f <= 20:
        total_adj -= 0.5
    return 0.0, total_adj


def rest_adjust(rest_days_home: int | None, rest_days_away: int | None) -> float:
    """+ favors home team. Short week (<6 days) is a real penalty; extra rest
    (>7, i.e. coming off a bye) is a real boost. Difference is what matters."""
    if rest_days_home is None or rest_days_away is None:
        return 0.0
    diff = rest_days_home - rest_days_away
    per_day = 0.3
    return max(-2.0, min(2.0, diff * per_day))


def rivalry_adjust(is_divisional: bool) -> tuple[float, float]:
    """Returns (spread_adj, total_adj). The spread leg is always 0.0 here -- rivalry
    reaches the spread through rivalry_compress_spread() below, whose delta project.py
    folds back into `rivalry_adj` so the stored column reflects the real effect. Rivalry games run closer than the
    rating gap alone suggests and tend to go a bit under -- compress the total,
    nudge the spread toward pick'em rather than moving it directionally."""
    if is_divisional:
        return 0.0, -1.0
    return 0.0, 0.0


def ref_adjust(crew_home_ats_pct: float | None, crew_games: int | None) -> float:
    """A crew's career Home ATS% lean, folded in at a fraction of face value --
    a soft signal next to injuries/rest/travel. Requires a real sample."""
    if crew_home_ats_pct is None or not crew_games or crew_games < 10:
        return 0.0
    return round(max(-0.4, min(0.4, (crew_home_ats_pct - 50.0) / 50.0 * 0.8)), 2)


def rivalry_compress_spread(spread: float, is_divisional: bool, factor: float = 0.90) -> float:
    """Applied after the additive stack: shrink the final spread toward
    pick'em a little for rivalry games, per the framework's guidance that
    these compress margin more than they shift direction."""
    return spread * factor if is_divisional else spread
