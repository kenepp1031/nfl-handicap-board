"""Shared constants + small helpers for NFL 2.0. Team abbreviations here match
nflverse's convention exactly (as seen in games.csv / player_stats / snap_counts):
'LA' for the Rams (not 'LAR'), 'WAS' for Washington (not 'WSH')."""
from __future__ import annotations

import gzip
import json
import time
from pathlib import Path
from urllib.request import Request, urlopen

APP_DIR = Path(__file__).parent
CACHE_DIR = APP_DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)

USER_AGENT = "NFL-2.0-personal-handicapping-board/1.0"


def fetch_text(url: str, timeout: int = 40, gzipped: bool = False) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        raw = response.read()
    if gzipped:
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", "ignore")


def fetch_json(url: str, timeout: int = 25) -> dict:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def cached_fetch(name: str, url: str, max_age_hours: float, gzipped: bool = False) -> str:
    """Cache a large/slow-changing text pull (season CSVs, full game history,
    gzipped play-by-play) on disk for max_age_hours."""
    path = CACHE_DIR / name
    if path.exists():
        age_hours = (time.time() - path.stat().st_mtime) / 3600
        if age_hours < max_age_hours:
            return path.read_text(encoding="utf-8")
    text = fetch_text(url, timeout=90 if gzipped else 40, gzipped=gzipped)
    path.write_text(text, encoding="utf-8")
    return text


TEAM_NAMES = {
    "ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons", "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills", "CAR": "Carolina Panthers", "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns", "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos", "DET": "Detroit Lions", "GB": "Green Bay Packers",
    "HOU": "Houston Texans", "IND": "Indianapolis Colts", "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs", "LV": "Las Vegas Raiders", "LAC": "Los Angeles Chargers",
    "LA": "Los Angeles Rams", "MIA": "Miami Dolphins", "MIN": "Minnesota Vikings",
    "NE": "New England Patriots", "NO": "New Orleans Saints", "NYG": "New York Giants",
    "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers",
    "SF": "San Francisco 49ers", "SEA": "Seattle Seahawks", "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans", "WAS": "Washington Commanders",
}

# (lat, lon, indoors) -- ported from the old app's STADIUMS, re-keyed to nflverse abbrs.
STADIUMS = {
    "ARI": (33.5276, -112.2626, False), "ATL": (33.7554, -84.4008, True),
    "BAL": (39.2780, -76.6227, False), "BUF": (42.7738, -78.7868, False),
    "CAR": (35.2258, -80.8528, False), "CHI": (41.8623, -87.6167, False),
    "CIN": (39.0954, -84.5160, False), "CLE": (41.5061, -81.6995, False),
    "DAL": (32.7473, -97.0945, True), "DEN": (39.7439, -105.0201, False),
    "DET": (42.3400, -83.0456, True), "GB": (44.5013, -88.0622, False),
    "HOU": (29.6847, -95.4107, True), "IND": (39.7601, -86.1639, True),
    "JAX": (30.3239, -81.6373, False), "KC": (39.0489, -94.4839, False),
    "LV": (36.0909, -115.1833, True), "LAC": (33.9535, -118.3392, False),
    "LA": (33.9535, -118.3392, False), "MIA": (25.9580, -80.2389, False),
    "MIN": (44.9738, -93.2581, True), "NE": (42.0909, -71.2643, False),
    "NO": (29.9511, -90.0812, True), "NYG": (40.8135, -74.0745, False),
    "NYJ": (40.8135, -74.0745, False), "PHI": (39.9008, -75.1675, False),
    "PIT": (40.4468, -80.0158, False), "SF": (37.4030, -121.9700, False),
    "SEA": (47.5952, -122.3316, False), "TB": (27.9759, -82.5033, False),
    "TEN": (36.1665, -86.7713, False), "WAS": (38.9076, -76.8645, False),
}

# Venue display label "Stadium Name · City, ST", ported from the old app's VENUES.
VENUES = {
    "ARI": "State Farm Stadium · Glendale, AZ", "ATL": "Mercedes-Benz Stadium · Atlanta, GA",
    "BAL": "M&T Bank Stadium · Baltimore, MD", "BUF": "Highmark Stadium · Orchard Park, NY",
    "CAR": "Bank of America Stadium · Charlotte, NC", "CHI": "Soldier Field · Chicago, IL",
    "CIN": "Paycor Stadium · Cincinnati, OH", "CLE": "Huntington Bank Field · Cleveland, OH",
    "DAL": "AT&T Stadium · Arlington, TX", "DEN": "Empower Field at Mile High · Denver, CO",
    "DET": "Ford Field · Detroit, MI", "GB": "Lambeau Field · Green Bay, WI",
    "HOU": "NRG Stadium · Houston, TX", "IND": "Lucas Oil Stadium · Indianapolis, IN",
    "JAX": "EverBank Stadium · Jacksonville, FL", "KC": "GEHA Field at Arrowhead · Kansas City, MO",
    "LV": "Allegiant Stadium · Las Vegas, NV", "LAC": "SoFi Stadium · Inglewood, CA",
    "LA": "SoFi Stadium · Inglewood, CA", "MIA": "Hard Rock Stadium · Miami Gardens, FL",
    "MIN": "U.S. Bank Stadium · Minneapolis, MN", "NE": "Gillette Stadium · Foxborough, MA",
    "NO": "Caesars Superdome · New Orleans, LA", "NYG": "MetLife Stadium · East Rutherford, NJ",
    "NYJ": "MetLife Stadium · East Rutherford, NJ", "PHI": "Lincoln Financial Field · Philadelphia, PA",
    "PIT": "Acrisure Stadium · Pittsburgh, PA", "SF": "Levi's Stadium · Santa Clara, CA",
    "SEA": "Lumen Field · Seattle, WA", "TB": "Raymond James Stadium · Tampa, FL",
    "TEN": "Nissan Stadium · Nashville, TN", "WAS": "Northwest Stadium · Landover, MD",
}

# Personal read on venues that meaningfully hurt a visiting offense beyond the
# league-average home-field bump. Ported from the old app.
NOISE_ELITE = {"SEA", "KC", "NO", "MIN"}
NOISE_LOUD = {"PHI", "BUF", "PIT", "BAL", "DEN", "GB"}

# ESPN's team-logo CDN uses its own lowercase codes, which differ from
# nflverse's in two spots: Rams is "lar" not "la", Washington is "wsh" not "was".
ESPN_LOGO_CODE = {abbr: abbr.lower() for abbr in TEAM_NAMES}
ESPN_LOGO_CODE["LA"] = "lar"
ESPN_LOGO_CODE["WAS"] = "wsh"


def logo_url(abbr: str) -> str:
    code = ESPN_LOGO_CODE.get(abbr, abbr.lower())
    return f"https://a.espncdn.com/i/teamlogos/nfl/500/{code}.png"


# Position-value multiplier for injury-severity-aware roll-up (framework §6).
POSITION_VALUE = {
    "QB": 3.0,
    "OT": 1.6, "CB": 1.6,
    "WR": 1.3, "EDGE": 1.3, "DE": 1.3,
}
DEFAULT_POSITION_VALUE = 1.0

# Position groups used for grading/roll-up.
POSITION_GROUP = {
    "QB": "QB", "RB": "RB", "FB": "RB", "WR": "WR", "TE": "TE",
    "T": "OL", "OT": "OL", "G": "OL", "OG": "OL", "C": "OL", "OL": "OL",
    "DE": "DEF", "EDGE": "DEF", "DT": "DEF", "NT": "DEF", "DL": "DEF", "LB": "DEF",
    "ILB": "DEF", "OLB": "DEF", "MLB": "DEF", "CB": "DEF", "DB": "DEF",
    "S": "DEF", "SS": "DEF", "FS": "DEF", "SAF": "DEF",
    "K": "ST", "P": "ST", "LS": "ST",
}
