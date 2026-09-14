"""Personal NFL board: descriptive ratings, team efficiency, lines, and weather."""
from __future__ import annotations

import base64, csv, ctypes, html, io, json, math, os, queue, re, sqlite3, sys, threading, webbrowser
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote_plus, urlencode
from urllib.request import Request, urlopen
# Built-in analytics; keep the desktop app self-contained.

STATS_URL = 'https://github.com/nflverse/nflverse-data/releases/download/stats_team/stats_team_week_{year}.csv'
GRADE_GUIDE = ('EPA efficiency grades: A >= +1 SD, B >= +0.35, C >= -0.35, '
               'D >= -1, F below -1. Defense is reversed so higher is better. '
               'Current season blends with eight games of prior-season efficiency. '
               'Descriptive only. Score projections blend these grades with power ratings; '
               'N/A means missing data; prior means no current-season sample.')

def letter(z):
    if z is None or not math.isfinite(z): return 'N/A'
    return 'A' if z >= 1 else 'B' if z >= .35 else 'C' if z >= -.35 else 'D' if z >= -1 else 'F'

def team_units(csv_text, year, before_week=99):
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    required = {'team','opponent_team','season','season_type','week','game_id',
                'passing_epa','rushing_epa','attempts','sacks_suffered','carries'}
    if not rows or not required.issubset(rows[0]): raise ValueError('Missing team EPA fields')
    totals = {}; seen = set()
    for r in rows:
        if r['season_type'] != 'REG' or int(r['season']) != year or int(r['week']) >= before_week: continue
        key = (r['game_id'], r['team'])
        if key in seen: continue
        seen.add(key)
        try:
            epa = float(r['passing_epa']) + float(r['rushing_epa'])
            plays = float(r['attempts']) + float(r['sacks_suffered']) + float(r['carries'])
        except (ValueError, TypeError): continue
        if not math.isfinite(epa) or not math.isfinite(plays) or plays <= 0: continue
        for team, unit, value in ((r['team'], 'off', epa),(r['opponent_team'], 'def', -epa)):
            v = totals.setdefault(team, {}).setdefault(unit, [0.,0.,0])
            v[0] += value; v[1] += plays; v[2] += 1
    return {team:{unit:(v[0]/v[1],v[2]) for unit,v in units.items()} for team,units in totals.items()}

def grades(prior, current):
    values = {}
    for team in prior.keys() | current.keys():
        item = {}
        for unit in ('off','def'):
            p = prior.get(team,{}).get(unit); c = current.get(team,{}).get(unit)
            if p and c: value = (p[0]*8+c[0]*c[1])/(8+c[1])
            elif p: value = p[0]
            elif c and c[1] >= 4: value = c[0]
            else: continue
            item[unit] = value
            item['games'] = c[1] if c else 0
        values[team] = item
    for unit in ('off','def'):
        sample = [v[unit] for v in values.values() if unit in v]
        if len(sample) < 28: continue  # incomplete league cannot support relative grades
        avg = sum(sample)/len(sample)
        sd = (sum((x-avg)**2 for x in sample)/len(sample))**.5
        for item in values.values():
            if unit in item:
                z = (item[unit]-avg)/sd if sd else 0.
                item[unit+'_grade'] = letter(z)
                item[unit+'_z'] = z
    return values

def weather_flags(wind=None, gust=None, precipitation=None, probability=None, codes=()):
    flags=[]; codes=set(c for c in codes if c is not None)
    if wind is not None and wind >= 15: flags.append(f'WIND {wind:.0f} mph')
    if gust is not None and gust >= 15: flags.append(f'GUSTS {gust:.0f} mph')
    if codes & {56,57,66,67}: flags.append('FREEZING RAIN / ICE RISK')
    if codes & {71,73,75,77,85,86}: flags.append('SNOW / WINTRY PRECIP')
    if codes & {51,53,55,61,63,65,80,81,82,95,96,99}: flags.append('RAIN / SHOWERS')
    if precipitation is not None and precipitation > 0 and not any('RAIN' in f or 'SNOW' in f for f in flags): flags.append('PRECIPITATION')
    if probability is not None and probability >= 30: flags.append(f'PRECIP CHANCE {probability:.0f}%')
    return ' | '.join(flags)

def forecast_window(hourly, kickoff):
    """Include kickoff's hour through three hours later, including next UTC day."""
    start=kickoff.astimezone(timezone.utc).replace(minute=0,second=0,microsecond=0)
    times=hourly.get('time',[])
    wanted=[(start+timedelta(hours=i)).strftime('%Y-%m-%dT%H:00') for i in range(4)]
    if any(t not in times for t in wanted): raise ValueError('Incomplete game-window forecast')
    ix=[times.index(t) for t in wanted]
    def vals(key):
        array=hourly.get(key,[])
        return [array[i] for i in ix if i < len(array) and array[i] is not None]
    def peak(key):
        v=vals(key); return max(v) if v else None
    codes=vals('weathercode')
    temperature=vals('temperature_2m')
    wind=peak('windspeed_10m'); gust=peak('windgusts_10m')
    precip=vals('precipitation'); probability=peak('precipitation_probability')
    if len(temperature)!=4 or len(vals('windspeed_10m'))!=4 or len(codes)!=4 or len(precip)!=4:
        raise ValueError('Missing forecast measurements')
    total=sum(precip)
    return dict(temperature=temperature[0],wind=wind,precipitation=total,weather_code=codes[0],
                alert=weather_flags(wind,gust,total,probability,codes))

import tkinter as tk
from tkinter import messagebox, ttk

# Optional: only used to fade team logos into a subtle background watermark on
# each matchup card. Plain Tkinter has no way to blend an image's opacity against
# whatever's behind it -- Pillow can pre-bake that fade into the image itself
# before Tk ever sees it. The app works fully without this; the watermark simply
# doesn't appear if Pillow isn't installed (`pip install pillow`).
try:
    from PIL import Image as _PILImage, ImageTk as _PILImageTk
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

def _fix_windows_dpi_scaling() -> None:
    """Tell Windows this app manages its own display scaling.
    Without this, on a scaled laptop display (125%/150%, extremely common)
    Windows silently rescales the rendered window, but Tk keeps computing
    "which widget is under the pointer" using the old unscaled coordinates.
    The mismatch makes clicks/scrolls miss ordinary Tk widgets almost
    everywhere -- except native OS-themed controls like ttk.Scrollbar,
    which Windows positions correctly on its own. That's the "scrolling
    only works right over the arrow" symptom. This must run before the Tk
    root window is created.
    """
    if sys.platform != "win32": return
    try: ctypes.windll.shcore.SetProcessDpiAwareness(1)  # per-monitor DPI aware (Windows 8.1+)
    except Exception:
        try: ctypes.windll.user32.SetProcessDPIAware()  # fallback for older Windows
        except Exception: pass

APP_DIR = Path(__file__).parent
DB = APP_DIR / "nfl_handicapping_board.db"
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
NFLVERSE_GAMES = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
DK_SPLITS = "https://dknetwork.draftkings.com/draftkings-sportsbook-betting-splits/?tb_eg=NFL&tb_edate=n7days&itm_content=NFL"
ESPN_INJURIES = "https://www.espn.com/nfl/injuries"
# Power-ranking sources. Every URL is a page/feed its publisher updates in place
# each week (NFL.com's hub always links the newest weekly article first), so none
# of these need editing as the season goes on. Betting-market lists (Kalshi futures,
# ESPN FPI -- whose preseason rating is built on Vegas win totals) are deliberately
# left out: the consensus should be an opinion to hold up against the DraftKings
# line, not a copy of it.
CBS_RANKINGS = "https://www.cbssports.com/nfl/powerrankings/"
SHARP_RANKINGS = "https://www.sharpfootballanalysis.com/analysis/nfl-power-rankings/"
NFL_RANKINGS_HUB = "https://www.nfl.com/news/series/power-rankings-news"
PFM_RANKINGS = "https://profootballmania.com/nfl-team-rankings/"
TEAMRANKINGS_RATINGS = "https://www.teamrankings.com/nfl/ranking/predictive-by-other"
SAGARIN_RATINGS = "http://sagarin.com/sports/nflsend.htm"
# (teams column, label) for every live source. One that hasn't refreshed within
# RANKING_STALE_DAYS (site down, or redesigned so its parser stops finding all 32
# teams) drops out of the consensus instead of freezing it at an old week.
RANKING_SOURCES = (("cbs_rank","CBS"),("sharp_rank","Sharp"),("nfl_rank","NFL.com"),("pfm_rank","PFM"),("tr_rank","TeamRankings"),("sagarin_rank","Sagarin"))
RANKING_STALE_DAYS = 10
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"  # free, no API key -- kickoff-hour forecast for outdoor stadiums
# Rotowire republishes a new article (new URL/id) most weeks with that week's crew
# assignments -- if the referee factor silently goes stale, this is the first thing
# to check and update to that week's article URL.
ROTOWIRE_REFS = "https://www.rotowire.com/football/article/nfl-referee-assignments-betting-trends-by-crew-133202"
# nflverse's games.csv carries a "referee" column back to 1999 alongside the actual
# closing spread/total -- computing career Home ATS% / Over% straight from real
# results here is steadier than scraping a stats page, since it can never drift
# from what actually happened and won't break when someone redesigns a site.
NFLVERSE_GAMES = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
REFEREE_GAMES_CACHE = APP_DIR / "referee_games_cache.csv"
REFEREE_GAMES_CACHE_HOURS = 20  # results trickle in through the week; no need to re-pull the whole history often
AUTO_UPDATE_MS = 60*60*1000  # hourly is plenty for injuries/lines/weather/referees; use the REFRESH button for on-demand

TEAMS = [("Arizona Cardinals","ari"),("Atlanta Falcons","atl"),("Baltimore Ravens","bal"),("Buffalo Bills","buf"),("Carolina Panthers","car"),("Chicago Bears","chi"),("Cincinnati Bengals","cin"),("Cleveland Browns","cle"),("Dallas Cowboys","dal"),("Denver Broncos","den"),("Detroit Lions","det"),("Green Bay Packers","gb"),("Houston Texans","hou"),("Indianapolis Colts","ind"),("Jacksonville Jaguars","jax"),("Kansas City Chiefs","kc"),("Las Vegas Raiders","lv"),("Los Angeles Chargers","lac"),("Los Angeles Rams","lar"),("Miami Dolphins","mia"),("Minnesota Vikings","min"),("New England Patriots","ne"),("New Orleans Saints","no"),("New York Giants","nyg"),("New York Jets","nyj"),("Philadelphia Eagles","phi"),("Pittsburgh Steelers","pit"),("San Francisco 49ers","sf"),("Seattle Seahawks","sea"),("Tampa Bay Buccaneers","tb"),("Tennessee Titans","ten"),("Washington Commanders","wsh")]
STADIUMS = {"Arizona Cardinals":(33.5276,-112.2626,False),"Atlanta Falcons":(33.7554,-84.4008,True),"Baltimore Ravens":(39.2780,-76.6227,False),"Buffalo Bills":(42.7738,-78.7868,False),"Carolina Panthers":(35.2258,-80.8528,False),"Chicago Bears":(41.8623,-87.6167,False),"Cincinnati Bengals":(39.0954,-84.5160,False),"Cleveland Browns":(41.5061,-81.6995,False),"Dallas Cowboys":(32.7473,-97.0945,True),"Denver Broncos":(39.7439,-105.0201,False),"Detroit Lions":(42.3400,-83.0456,True),"Green Bay Packers":(44.5013,-88.0622,False),"Houston Texans":(29.6847,-95.4107,True),"Indianapolis Colts":(39.7601,-86.1639,True),"Jacksonville Jaguars":(30.3239,-81.6373,False),"Kansas City Chiefs":(39.0489,-94.4839,False),"Las Vegas Raiders":(36.0909,-115.1833,True),"Los Angeles Chargers":(33.9535,-118.3392,False),"Los Angeles Rams":(33.9535,-118.3392,False),"Miami Dolphins":(25.9580,-80.2389,False),"Minnesota Vikings":(44.9738,-93.2581,True),"New England Patriots":(42.0909,-71.2643,False),"New Orleans Saints":(29.9511,-90.0812,True),"New York Giants":(40.8135,-74.0745,False),"New York Jets":(40.8135,-74.0745,False),"Philadelphia Eagles":(39.9008,-75.1675,False),"Pittsburgh Steelers":(40.4468,-80.0158,False),"San Francisco 49ers":(37.4030,-121.9700,False),"Seattle Seahawks":(47.5952,-122.3316,False),"Tampa Bay Buccaneers":(27.9759,-82.5033,False),"Tennessee Titans":(36.1665,-86.7713,False),"Washington Commanders":(38.9076,-76.8645,False)}
VENUES = {"Arizona Cardinals":"State Farm Stadium · Glendale, AZ","Atlanta Falcons":"Mercedes-Benz Stadium · Atlanta, GA","Baltimore Ravens":"M&T Bank Stadium · Baltimore, MD","Buffalo Bills":"Highmark Stadium · Orchard Park, NY","Carolina Panthers":"Bank of America Stadium · Charlotte, NC","Chicago Bears":"Soldier Field · Chicago, IL","Cincinnati Bengals":"Paycor Stadium · Cincinnati, OH","Cleveland Browns":"Huntington Bank Field · Cleveland, OH","Dallas Cowboys":"AT&T Stadium · Arlington, TX","Denver Broncos":"Empower Field at Mile High · Denver, CO","Detroit Lions":"Ford Field · Detroit, MI","Green Bay Packers":"Lambeau Field · Green Bay, WI","Houston Texans":"NRG Stadium · Houston, TX","Indianapolis Colts":"Lucas Oil Stadium · Indianapolis, IN","Jacksonville Jaguars":"EverBank Stadium · Jacksonville, FL","Kansas City Chiefs":"GEHA Field at Arrowhead · Kansas City, MO","Las Vegas Raiders":"Allegiant Stadium · Las Vegas, NV","Los Angeles Chargers":"SoFi Stadium · Inglewood, CA","Los Angeles Rams":"SoFi Stadium · Inglewood, CA","Miami Dolphins":"Hard Rock Stadium · Miami Gardens, FL","Minnesota Vikings":"U.S. Bank Stadium · Minneapolis, MN","New England Patriots":"Gillette Stadium · Foxborough, MA","New Orleans Saints":"Caesars Superdome · New Orleans, LA","New York Giants":"MetLife Stadium · East Rutherford, NJ","New York Jets":"MetLife Stadium · East Rutherford, NJ","Philadelphia Eagles":"Lincoln Financial Field · Philadelphia, PA","Pittsburgh Steelers":"Acrisure Stadium · Pittsburgh, PA","San Francisco 49ers":"Levi's Stadium · Santa Clara, CA","Seattle Seahawks":"Lumen Field · Seattle, WA","Tampa Bay Buccaneers":"Raymond James Stadium · Tampa, FL","Tennessee Titans":"Nissan Stadium · Nashville, TN","Washington Commanders":"Northwest Stadium · Landover, MD"}
TEAM_NICKNAMES = {name.split()[-1]: name for name in STADIUMS}  # "Patriots" -> "New England Patriots", for matching referee-site team mentions
# City-only labels some rating sites print ("Seattle", "Tampa Bay"). Los Angeles and
# New York are shared, so those teams are left to the nickname match ("LA Rams").
TEAM_CITIES = {name.rsplit(" ",1)[0]: name for name in STADIUMS if sum(other.rsplit(" ",1)[0]==name.rsplit(" ",1)[0] for other in STADIUMS)==1}
# A designated "home" team does not always mean that the game is in its normal city.
# (latitude, longitude, indoors, venue label)
NEUTRAL_SITES = {("Los Angeles Rams","San Francisco 49ers"):(-37.81997,144.98345,False,"NEUTRAL SITE · Melbourne Cricket Ground · Melbourne, Australia")}
# Personal house rule for the YOUR RATING HAS formula: some stadiums are built (or
# just consistently loud enough) to genuinely disrupt a visiting offense's snap
# count -- false starts, delayed cadence, fewer possessions -- so their home team
# gets a bigger home-field bump than the league-average +2. NOISE_ELITE also gets a
# small speaker icon next to the total as a reminder that these venues have
# historically leaned Under. Entirely a personal read, not a market number -- edit
# either set or the bonus values in rating_margin() below freely.
NOISE_ELITE = {"Seattle Seahawks","Kansas City Chiefs","New Orleans Saints","Minnesota Vikings"}
NOISE_LOUD = {"Philadelphia Eagles","Buffalo Bills","Pittsburgh Steelers","Baltimore Ravens","Denver Broncos","Green Bay Packers"}
# Longstanding rivalry pairs (mostly division games with real history, plus a few
# cross-division grudge matches). Order doesn't matter -- stored as frozensets and
# looked up by {home,away}. Rivalry games tend to run closer than the rating gap
# alone suggests (extra motivation for the "worse" team), so rating_margin() below
# shrinks the projected spread toward pick'em when a matchup lands in this set.
RIVALRIES = {
    frozenset({"Dallas Cowboys","Washington Commanders"}),
    frozenset({"Dallas Cowboys","Philadelphia Eagles"}),
    frozenset({"Dallas Cowboys","New York Giants"}),
    frozenset({"Philadelphia Eagles","Washington Commanders"}),
    frozenset({"Philadelphia Eagles","New York Giants"}),
    frozenset({"New York Giants","Washington Commanders"}),
    frozenset({"Green Bay Packers","Chicago Bears"}),
    frozenset({"Green Bay Packers","Minnesota Vikings"}),
    frozenset({"Green Bay Packers","Detroit Lions"}),
    frozenset({"Chicago Bears","Detroit Lions"}),
    frozenset({"Chicago Bears","Minnesota Vikings"}),
    frozenset({"Minnesota Vikings","Detroit Lions"}),
    frozenset({"Pittsburgh Steelers","Baltimore Ravens"}),
    frozenset({"Pittsburgh Steelers","Cleveland Browns"}),
    frozenset({"Cleveland Browns","Baltimore Ravens"}),
    frozenset({"Cleveland Browns","Cincinnati Bengals"}),
    frozenset({"Baltimore Ravens","Cincinnati Bengals"}),
    frozenset({"Kansas City Chiefs","Denver Broncos"}),
    frozenset({"Kansas City Chiefs","Las Vegas Raiders"}),
    frozenset({"Kansas City Chiefs","Los Angeles Chargers"}),
    frozenset({"Denver Broncos","Las Vegas Raiders"}),
    frozenset({"Los Angeles Chargers","Las Vegas Raiders"}),
    frozenset({"New England Patriots","New York Jets"}),
    frozenset({"New England Patriots","Buffalo Bills"}),
    frozenset({"New England Patriots","Miami Dolphins"}),
    frozenset({"Buffalo Bills","Miami Dolphins"}),
    frozenset({"Buffalo Bills","New York Jets"}),
    frozenset({"San Francisco 49ers","Seattle Seahawks"}),
    frozenset({"San Francisco 49ers","Los Angeles Rams"}),
    frozenset({"Seattle Seahawks","Los Angeles Rams"}),
    frozenset({"Arizona Cardinals","Seattle Seahawks"}),
    frozenset({"Arizona Cardinals","Los Angeles Rams"}),
    frozenset({"Tennessee Titans","Indianapolis Colts"}),
    frozenset({"Indianapolis Colts","Houston Texans"}),
    frozenset({"Houston Texans","Jacksonville Jaguars"}),
    frozenset({"Tennessee Titans","Jacksonville Jaguars"}),
    frozenset({"New Orleans Saints","Atlanta Falcons"}),
    frozenset({"New Orleans Saints","Tampa Bay Buccaneers"}),
    frozenset({"New Orleans Saints","Carolina Panthers"}),
    frozenset({"Atlanta Falcons","Carolina Panthers"}),
    frozenset({"Tampa Bay Buccaneers","Carolina Panthers"}),
    frozenset({"Atlanta Falcons","Tampa Bay Buccaneers"}),
}

PREFS_PATH = APP_DIR / "prefs.json"
def load_prefs() -> dict:
    try: return json.loads(PREFS_PATH.read_text(encoding="utf-8"))
    except Exception: return {}
def save_prefs(prefs: dict) -> None:
    try: PREFS_PATH.write_text(json.dumps(prefs), encoding="utf-8")
    except Exception: pass  # a failed preference save should never block using the app

# Every color the board uses, named once so light/dark are just two lookups instead
# of two copies of every screen. Swap or add a theme here -- nothing that reads
# self.C needs to change.
THEMES = {
"light": {
    "bg":"#eeeeee","header_bg":"#0b0b0d","header_fg":"#ffffff",
    "tabs_bg":"#19191c","tab_inactive":"#303036","tab_fg":"#ffffff","tabs_label":"#d9d9dd",
    "accent":"#c7132d","accent_hover":"#e33a50",
    "subtitle_fg":"#5d5d63","col_header_fg":"#62626a",
    "card_bg":"#ffffff","card_border":"#d9d9dd","card_selected":"#fff4d8","card_selected_star":"#e6f5e9",
    "card_won":"#d9f2e1","card_lost":"#f6d9dc",
    "team_fg":"#1b1b20","meta_fg":"#71717a",
    "warn_fg":"#b66c00","bad_fg":"#b01d2d","good_fg":"#16884a","info_fg":"#2f6688",
    "mid_bg":"#fbfbfc","mid_border":"#e2e2e5",
    "kickoff_fg":"#6f6f77","total_fg":"#202026","bets_fg":"#62626b","final_fg":"#151519",
    "badge_bg":"#e7e7ea","badge_fg":"#202025","badge_selected":"#f2d98c",
    "bet_bg":"#202025","bet_fg":"#ffffff",
    "status_bg":"#161619","status_fg":"#d8d8dc",
    "tracker_bg":"#1b1b20","tracker_fg":"#f0f0f2",
    "heading_bg":"#18181c","heading_fg":"#e3e3e6",
    "row_alt_bg":"#f7f7f8","row_bg":"#ffffff","sources_fg":"#666670",
    "empty_fg":"#655f70","move_up":"#16884a","move_down":"#b01d2d","move_flat":"#71717a",
},
"dark": {
    "bg":"#121214","header_bg":"#0a0a0c","header_fg":"#f4f4f6",
    "tabs_bg":"#1a1a1e","tab_inactive":"#2a2a30","tab_fg":"#f4f4f6","tabs_label":"#a8a8b2",
    "accent":"#e0264a","accent_hover":"#ff4064",
    "subtitle_fg":"#9a9aa4","col_header_fg":"#8a8a94",
    "card_bg":"#1c1c20","card_border":"#2c2c32","card_selected":"#3a301a","card_selected_star":"#123322",
    "card_won":"#123322","card_lost":"#3a161c",
    "team_fg":"#f4f4f6","meta_fg":"#9a9aa4",
    "warn_fg":"#e0a530","bad_fg":"#ef4a5c","good_fg":"#2fd478","info_fg":"#5fb0e8",
    "mid_bg":"#19191d","mid_border":"#2c2c32",
    "kickoff_fg":"#a8a8b2","total_fg":"#f0f0f2","bets_fg":"#9a9aa4","final_fg":"#f4f4f6",
    "badge_bg":"#2a2a30","badge_fg":"#e8e8ea","badge_selected":"#c9902c",
    "bet_bg":"#2a2a30","bet_fg":"#f4f4f6",
    "status_bg":"#0a0a0c","status_fg":"#c8c8d0",
    "tracker_bg":"#1a1a1e","tracker_fg":"#f0f0f2",
    "heading_bg":"#1a1a1e","heading_fg":"#e3e3e6",
    "row_alt_bg":"#1c1c20","row_bg":"#161619","sources_fg":"#8a8a94",
    "empty_fg":"#8a8a94","move_up":"#2fd478","move_down":"#ef4a5c","move_flat":"#8a8a94",
},
}
WEEK1_2026 = [
    (1,"2026-09-09T20:20:00-04:00","Seattle Seahawks","New England Patriots"),
    (1,"2026-09-10T20:35:00-04:00","Los Angeles Rams","San Francisco 49ers"),
    (1,"2026-09-13T13:00:00-04:00","Indianapolis Colts","Baltimore Ravens"),(1,"2026-09-13T13:00:00-04:00","Houston Texans","Buffalo Bills"),(1,"2026-09-13T13:00:00-04:00","Jacksonville Jaguars","Cleveland Browns"),(1,"2026-09-13T13:00:00-04:00","Tennessee Titans","New York Jets"),(1,"2026-09-13T13:00:00-04:00","Pittsburgh Steelers","Atlanta Falcons"),(1,"2026-09-13T13:00:00-04:00","Carolina Panthers","Chicago Bears"),(1,"2026-09-13T13:00:00-04:00","Detroit Lions","New Orleans Saints"),(1,"2026-09-13T13:00:00-04:00","Cincinnati Bengals","Tampa Bay Buccaneers"),
    (1,"2026-09-13T16:25:00-04:00","Los Angeles Chargers","Arizona Cardinals"),(1,"2026-09-13T16:25:00-04:00","Minnesota Vikings","Green Bay Packers"),(1,"2026-09-13T16:25:00-04:00","Las Vegas Raiders","Miami Dolphins"),(1,"2026-09-13T16:25:00-04:00","Philadelphia Eagles","Washington Commanders"),(1,"2026-09-13T20:20:00-04:00","New York Giants","Dallas Cowboys"),(1,"2026-09-14T20:15:00-04:00","Kansas City Chiefs","Denver Broncos")]
# Snapshot from the DraftKings Network NFL Spread board. The live refresh below replaces it whenever the page is available.
DK_WEEK1_SPREADS = {
    "Seattle Seahawks":(-3,55,42,45,58), "Los Angeles Rams":(-3.5,70,75,30,25), "Carolina Panthers":(3,12,14,88,86), "Detroit Lions":(-7,87,82,13,18),
    "Indianapolis Colts":(3.5,32,29,68,71), "Houston Texans":(1.5,29,24,71,76), "Jacksonville Jaguars":(-8.5,72,73,28,27), "Cincinnati Bengals":(-3.5,59,53,41,47),
    "Pittsburgh Steelers":(-3.5,84,72,16,28), "Tennessee Titans":(-1.5,69,53,31,47), "Philadelphia Eagles":(-5.5,96,68,4,32), "Minnesota Vikings":(-1.5,29,30,71,70),
    "Las Vegas Raiders":(-3.5,63,64,37,36), "Los Angeles Chargers":(-9.5,36,63,64,37), "New York Giants":(3,9,22,91,78), "Kansas City Chiefs":(-2.5,13,27,87,73)}
DK_WEEK1_TOTALS = {"Seattle Seahawks":44.5,"Los Angeles Rams":48.5,"Cincinnati Bengals":50.5,"Houston Texans":44.5,"Carolina Panthers":47.5,"Detroit Lions":49.5,"Jacksonville Jaguars":40.5,"Indianapolis Colts":47.5,"Pittsburgh Steelers":42.5,"Tennessee Titans":39.5}

class InjuryParser(HTMLParser):
    """Small, dependency-free reader for ESPN's team injury tables."""
    def __init__(self):
        super().__init__(); self.team=None; self.in_row=False; self.cell=False; self.cells=[]; self.rows=[]
    def handle_starttag(self, tag, attrs):
        if tag == "tr": self.in_row=True; self.cells=[]
        elif tag in ("td","th") and self.in_row: self.cell=True
    def handle_endtag(self, tag):
        if tag in ("td","th"): self.cell=False
        elif tag == "tr":
            if self.team and len(self.cells) >= 4 and self.cells[0].upper() != "NAME": self.rows.append((self.team,*self.cells[:5]))
            self.in_row=False; self.cells=[]
    def handle_data(self, data):
        text=" ".join(data.split())
        if not text:return
        if text in dict(TEAMS): self.team=text
        elif self.cell: self.cells.append(text)

def get_json(url: str) -> dict:
    request = Request(url, headers={"User-Agent":"Personal-NFL-Handicapping-Board/1.0"})
    with urlopen(request, timeout=25) as response: return json.load(response)

def round_half(x: float) -> float:
    """Round to the nearest 0.5 -- cleaner-looking consensus ratings (7.5, not 7.3)."""
    return round(x * 2) / 2

def weather_icon(code, wind) -> str:
    """One tiny glyph for a WMO weather code + wind speed (mph). Priority: snow,
    then fog, then rain/storm, then strong wind (notable on its own for a football
    game even under clear skies), then plain sun."""
    if code is None: return ""
    if code in (71, 73, 75, 77, 85, 86): return "❄️"
    if code in (45, 48): return "🌫️"
    if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99): return "🌧️"
    if wind is not None and wind >= 20: return "💨"
    return "☀️"

def haversine_miles(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance between two points, in miles -- used to estimate how
    far a traveling team has flown for the Confidence Score's situational factor."""
    r = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi, dlambda = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))

# Guards every DB connection app-wide. `with conn:` on a bare sqlite3
# connection only commits/rolls back the transaction -- it never closes the
# handle -- so the UI thread (a pick button click) and a background worker
# (injuries/lines/power-rankings, each on its own daemon thread) could end
# up mid-write on separate connections at the same moment. SQLite allows
# only one writer at a time, and that collision is what raised
# "database is locked" from save_pick. Serializing every con() block behind
# one lock removes the race outright, and closing the connection on exit
# stops handles from piling up.
_db_lock = threading.Lock()

class _LockedConnection:
    def __enter__(self):
        _db_lock.acquire()
        self._raw = sqlite3.connect(DB, timeout=30)
        self._raw.row_factory = sqlite3.Row
        return self._raw.__enter__()
    def __exit__(self, exc_type, exc, tb):
        try: return self._raw.__exit__(exc_type, exc, tb)
        finally:
            self._raw.close()
            _db_lock.release()

def con() -> sqlite3.Connection:
    return _LockedConnection()

def update_consensus(c) -> list:
    """Recompute every team's consensus_rating (average source rank mapped onto the
    1-10 scale) from only the RANKING_SOURCES that refreshed within
    RANKING_STALE_DAYS, so a source whose site stopped updating ages out instead of
    pinning the consensus to an old week. Leaves ratings untouched when no source is
    fresh (first launch after an upgrade, before the first refresh). Returns the
    labels of the sources used."""
    cutoff=(datetime.now()-timedelta(days=RANKING_STALE_DAYS)).isoformat(timespec="minutes")
    fresh={r["column_name"] for r in c.execute("SELECT column_name FROM ranking_sources WHERE updated_at>=?",(cutoff,))}
    columns=[column for column,_ in RANKING_SOURCES if column in fresh]
    if not columns: return []
    for row in c.execute(f"SELECT name,{','.join(columns)} FROM teams").fetchall():
        ranks=[row[column] for column in columns if row[column] is not None]
        if ranks: c.execute("UPDATE teams SET consensus_rating=? WHERE name=?",(round(10-(sum(ranks)/len(ranks)-1)*9/31,1),row["name"]))
    return [label for column,label in RANKING_SOURCES if column in fresh]

def init_db() -> None:
    with con() as c:
        # WAL lets readers and writers run concurrently instead of blocking
        # on the single rollback-journal lock used by the default mode.
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript("""
        CREATE TABLE IF NOT EXISTS teams (name TEXT PRIMARY KEY, abbr TEXT NOT NULL, espn_rank INTEGER, cbs_rank INTEGER, sharp_rank INTEGER, fox_rank INTEGER, nfl_rank INTEGER, kalshi_rank INTEGER, pfm_rank INTEGER, consensus_rating REAL);
        CREATE TABLE IF NOT EXISTS games (event_id TEXT PRIMARY KEY, week INTEGER NOT NULL, kickoff TEXT NOT NULL, home TEXT NOT NULL, away TEXT NOT NULL, dk_spread REAL, dk_total REAL, line_source TEXT, home_bets INTEGER, home_handle INTEGER, away_bets INTEGER, away_handle INTEGER, line_checked TEXT, home_score INTEGER, away_score INTEGER, game_status TEXT, pick_side TEXT, bet_star INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS line_history (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL, checked_at TEXT NOT NULL, home_spread REAL, total REAL, home_bets INTEGER, home_handle INTEGER, away_bets INTEGER, away_handle INTEGER, UNIQUE(event_id, checked_at));
        CREATE TABLE IF NOT EXISTS injuries (team TEXT NOT NULL, player TEXT NOT NULL, position TEXT, return_date TEXT, status TEXT, comment TEXT, updated_at TEXT NOT NULL, PRIMARY KEY(team, player));
        CREATE TABLE IF NOT EXISTS ranking_history (season_week INTEGER NOT NULL, team TEXT NOT NULL, rating REAL NOT NULL, recorded_at TEXT NOT NULL, PRIMARY KEY(season_week, team));
        CREATE TABLE IF NOT EXISTS referee_stats (referee TEXT PRIMARY KEY, games INTEGER, home_ats_pct REAL, over_pct REAL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS referee_assignments (season_week INTEGER NOT NULL, home TEXT NOT NULL, away TEXT NOT NULL, referee TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(season_week, home, away));
        CREATE TABLE IF NOT EXISTS weather (event_id TEXT PRIMARY KEY, temperature REAL, wind REAL, precipitation REAL, weather_code INTEGER, dome INTEGER NOT NULL DEFAULT 0, checked_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ranking_sources (column_name TEXT PRIMARY KEY, label TEXT NOT NULL, detail TEXT, updated_at TEXT NOT NULL);
        """)
        weather_columns={r[1] for r in c.execute("PRAGMA table_info(weather)")}
        if "alert" not in weather_columns: c.execute("ALTER TABLE weather ADD COLUMN alert TEXT")
        # One-time cleanup of a dead feature (the Odds API integration was fully
        # coded but never wired to any button -- removed as dead code). Guarded the
        # same way as the ADD COLUMN loop below, just inverted, so this is a no-op
        # on every launch after the first. (weather WAS dropped here too for the
        # same reason, back when it was unused scaffolding -- it's since been
        # wired up for real, so it stays.)
        # Preserve legacy settings for inspection; do not erase user data on startup.
        columns={r[1] for r in c.execute("PRAGMA table_info(games)")}
        for name, definition in (("line_source", "TEXT"), ("home_bets", "INTEGER"), ("home_handle", "INTEGER"), ("away_bets", "INTEGER"), ("away_handle", "INTEGER"), ("line_checked", "TEXT"), ("home_score", "INTEGER"), ("away_score", "INTEGER"), ("game_status", "TEXT"), ("pick_side", "TEXT"), ("bet_star", "INTEGER NOT NULL DEFAULT 0"), ("ou_pick", "TEXT"), ("pick_spread", "REAL"), ("pick_recorded", "TEXT"), ("auto_pick_side", "TEXT"), ("auto_pick_spread", "REAL"), ("auto_pick_recorded", "TEXT"), ("over_bets", "INTEGER"), ("over_handle", "INTEGER"), ("under_bets", "INTEGER"), ("under_handle", "INTEGER")):
            if name not in columns: c.execute(f"ALTER TABLE games ADD COLUMN {name} {definition}")
        for name in ("fd_spread", "fd_total"):
            pass  # Legacy FanDuel columns retained for audit, not used by the model.
        for name, abbr in TEAMS: c.execute("INSERT OR IGNORE INTO teams(name,abbr) VALUES(?,?)", (name,abbr))
        team_columns={r[1] for r in c.execute("PRAGMA table_info(teams)")}
        for name, definition in (("sharp_rank","INTEGER"),("fox_rank","INTEGER"),("nfl_rank","INTEGER"),("kalshi_rank","INTEGER"),("pfm_rank","INTEGER"),("tr_rank","INTEGER"),("sagarin_rank","INTEGER"),("consensus_rating","REAL")):
            if name not in team_columns:c.execute(f"ALTER TABLE teams ADD COLUMN {name} {definition}")
        # fox_rank/espn_rank (preseason-only articles) and kalshi_rank (a betting market)
        # are retired: their columns stay for audit but no longer feed the consensus.
        update_consensus(c)
        stamp=datetime.now().isoformat(timespec="minutes")
        for row in c.execute("SELECT name,consensus_rating FROM teams WHERE consensus_rating IS NOT NULL").fetchall():
            c.execute("INSERT OR IGNORE INTO ranking_history(season_week,team,rating,recorded_at) VALUES(1,?,?,?)",(row["name"],row["consensus_rating"],stamp))
        if c.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0:
            for number, kickoff, home, away in WEEK1_2026:
                c.execute("INSERT INTO games(event_id,week,kickoff,home,away) VALUES(?,?,?,?,?)",(f"week1-2026-{home[:3]}-{away[:3]}",number,kickoff,home,away))
        for home,(spread,home_handle,home_bets,away_handle,away_bets) in DK_WEEK1_SPREADS.items():
            c.execute("UPDATE games SET dk_spread=?,home_handle=?,home_bets=?,away_handle=?,away_bets=?,line_source='DraftKings',line_checked=? WHERE home=? AND week=1 AND kickoff LIKE '2026-%' AND (line_source IS NULL OR dk_spread IS NULL)",(spread,home_handle,home_bets,away_handle,away_bets,"2026-09-08",home))
        for home,total in DK_WEEK1_TOTALS.items():
            c.execute("UPDATE games SET dk_total=?,line_source='DraftKings',line_checked=? WHERE home=? AND week=1 AND kickoff LIKE '2026-%' AND dk_total IS NULL",(total,"2026-09-08",home))
        c.execute("INSERT OR IGNORE INTO line_history(event_id,checked_at,home_spread,total,home_bets,home_handle,away_bets,away_handle) SELECT event_id,line_checked,dk_spread,dk_total,home_bets,home_handle,away_bets,away_handle FROM games WHERE line_checked IS NOT NULL")
        # One-time cleanup: a prior version could leave two rows for the same real game
        # (a made-up "week1-2026-..." fallback ID plus ESPN's real ID once the live pull
        # confirmed it). Harmless to run every launch -- once cleaned up, there's nothing
        # left to find. Keeps whichever row has ESPN's real event_id, folding any picks
        # from the duplicate into it before removing the duplicate.
        for group in c.execute("SELECT week,home,away FROM games GROUP BY week,home,away HAVING COUNT(*)>1").fetchall():
            rows=c.execute("SELECT * FROM games WHERE week=? AND home=? AND away=? ORDER BY (event_id LIKE 'week1-2026-%')",(group["week"],group["home"],group["away"])).fetchall()
            keep,extras=rows[0],rows[1:]
            for dup in extras:
                c.execute("UPDATE games SET pick_side=COALESCE(pick_side,?),bet_star=MAX(bet_star,?),ou_pick=COALESCE(ou_pick,?),dk_spread=COALESCE(dk_spread,?),dk_total=COALESCE(dk_total,?) WHERE event_id=?",(dup["pick_side"],dup["bet_star"],dup["ou_pick"],dup["dk_spread"],dup["dk_total"],keep["event_id"]))
                c.execute("UPDATE games SET pick_spread=COALESCE(pick_spread,?),pick_recorded=COALESCE(pick_recorded,?) WHERE event_id=?",(dup["pick_spread"],dup["pick_recorded"],keep["event_id"]))
                c.execute("DELETE FROM games WHERE event_id=?",(dup["event_id"],))

class Board(tk.Tk):
    def __init__(self):
        super().__init__(); self.title("My NFL Handicapping Board"); self.geometry("1220x790"); self.minsize(980,620)
        try: self.iconbitmap(str(APP_DIR / "nfl-board-red-icon.ico"))
        except Exception: pass  # missing/invalid icon file should never block the app from opening
        # The old dashboard/My Picks tkinter window is no longer shown -- the print
        # page (print_week.html, kept current by _refresh_print_html) is now the
        # only view. The window is still built and kept alive (withdrawn, not
        # destroyed) because it's what runs the background data refreshes and
        # generates that page; there's just nothing left to look at in it.
        self.withdraw()
        self.theme_name=load_prefs().get("theme","dark"); self.C=THEMES[self.theme_name]
        self.configure(bg=self.C["bg"]); self.logo_images={}; self.fetching=set(); self._render_waiting=False; self.status=tk.StringVar(value="Week 1 is ready. Checking DraftKings betting splits…")
        self.week=tk.IntVar(value=1); self.view="schedule"; self.week_buttons={}; self.style=ttk.Style(self); self.style.theme_use("clam")
        # Every background worker (injuries/lines/rankings/schedule/logos, each on its
        # own daemon thread) needs to touch the UI when it finishes. Tcl/Tk is not
        # thread-safe, so calling widget methods (or even .after()) directly from
        # those threads races the main thread's own Tcl calls -- that's what was
        # causing this app to silently freeze and get killed as "Not Responding"
        # (Windows logged it as AppHang repeatedly). Workers now only ever put a
        # callback on this queue; _pump_ui_queue, running solely on the main thread,
        # is the only thing that ever calls it.
        self._ui_queue=queue.Queue(); self.unit_grades={}; self.stats_state="Stats not loaded"; self._stats_loading=False; self._auto_update_job=None
        # build() + a render()/logo-fetch pass used to run synchronously here for the
        # on-screen dashboard, costing ~2s before the window even appeared. That
        # dashboard is now withdrawn and never shown, so none of that needs to run
        # at all -- build() just creates the (invisible) widgets background workers
        # still address, and the print page below gets its data straight from SQL.
        self.build(); self.refresh_weeks(); self.after(50, self._pump_ui_queue); self.after(200, self.refresh_schedule); self.after(700, self.refresh_injuries); self.after(1200, self.refresh_public_lines); self.after(1700, self.refresh_power_rankings); self.after(2200, self.refresh_weather); self.after(2700, self.refresh_unit_grades); self.after(3200, self.refresh_referees); self._auto_update_job=self.after(AUTO_UPDATE_MS, self.auto_update)
        # Open the print page almost immediately instead of showing the (now
        # withdrawn) dashboard window. Most of the fetches above will still be in
        # flight at this point -- that's fine, _refresh_print_html rewrites the
        # file as each one lands and the page reloads itself every 2 minutes.
        self.after(150, self.print_week)

    def _pump_ui_queue(self):
        try:
            while True: self._ui_queue.get_nowait()()
        except queue.Empty: pass
        finally: self.after(50, self._pump_ui_queue)

    def build(self):
        C=self.C
        top=tk.Frame(self,bg=C["header_bg"],padx=22,pady=16); top.pack(fill="x")
        tk.Frame(top,bg=C["accent"],width=6,height=34).pack(side="left",padx=(0,14))
        tk.Label(top,text="MY NFL HANDICAPPER",font=("Segoe UI",18,"bold"),fg=C["header_fg"],bg=C["header_bg"]).pack(side="left")
        tools=tk.Frame(top,bg=C["header_bg"]); tools.pack(side="right")
        theme_label="☀ LIGHT MODE" if self.theme_name=="dark" else "🌙 DARK MODE"
        self.theme_button=tk.Button(tools,text=theme_label,relief="flat",bd=0,font=("Segoe UI",9,"bold"),bg=C["tab_inactive"],fg=C["tab_fg"],activebackground=C["accent"],activeforeground="white",command=self.toggle_theme)
        self.theme_button.pack(side="left")
        tabs=tk.Frame(self,bg=C["tabs_bg"],padx=14,pady=8);tabs.pack(fill="x")
        tk.Label(tabs,text="WEEKS",bg=C["tabs_bg"],fg=C["tabs_label"],font=("Segoe UI",9,"bold")).pack(side="left",padx=(0,9))
        for number in range(1,19):
            button=tk.Button(tabs,text=str(number),width=3,relief="flat",bd=0,font=("Segoe UI",9,"bold"),command=lambda n=number:self.set_week(n))
            button.pack(side="left",padx=2); self.week_buttons[number]=button
        self.power_button=tk.Button(tabs,text="POWER RANKINGS",width=16,relief="flat",bd=0,font=("Segoe UI",9,"bold"),command=self.show_rankings)
        self.power_button.pack(side="right",padx=(12,0))
        self.picks_button=tk.Button(tabs,text="MY PICKS",width=12,relief="flat",bd=0,font=("Segoe UI",9,"bold"),command=self.show_picks)
        self.picks_button.pack(side="right",padx=(12,0))
        # An action, not a view toggle like the two buttons above -- fixed styling
        # (not touched by paint_week_tabs) so it doesn't read as a third tab.
        self.print_button=tk.Button(tabs,text="PRINT WEEK",width=12,relief="flat",bd=0,font=("Segoe UI",9,"bold"),bg=C["tab_inactive"],fg=C["tab_fg"],activebackground=C["accent_hover"],activeforeground="white",command=self.print_week)
        self.print_button.pack(side="right",padx=(12,0))
        self.subtitle=tk.Label(self,text="Teams, DraftKings line, total, public action, and final cover result",bg=C["bg"],fg=C["subtitle_fg"],anchor="w",padx=22,pady=9); self.subtitle.pack(fill="x")
        h=tk.Frame(self,bg=C["bg"],padx=28); h.pack(fill="x"); self.matchup_header=h
        tk.Label(h,text="HOME",bg=C["bg"],fg=C["col_header_fg"],font=("Segoe UI",9,"bold")).pack(side="left")
        tk.Label(h,text="DRAFTKINGS LINES",bg=C["bg"],fg=C["col_header_fg"],font=("Segoe UI",9,"bold")).pack(side="left",expand=True)
        tk.Label(h,text="AWAY",bg=C["bg"],fg=C["col_header_fg"],font=("Segoe UI",9,"bold")).pack(side="right")
        outer=tk.Frame(self,bg=C["bg"]); outer.pack(fill="both",expand=True); self.outer=outer
        self.canvas=tk.Canvas(outer,bg=C["bg"],highlightthickness=0); bar=ttk.Scrollbar(outer,orient="vertical",command=self.canvas.yview); self.canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right",fill="y"); self.canvas.pack(side="left",fill="both",expand=True)
        self.rows=tk.Frame(self.canvas,bg=C["bg"]); self.window=self.canvas.create_window((0,0),window=self.rows,anchor="nw")
        # Debounced: during a live window-resize drag, Windows fires <Configure>
        # dozens of times per second. Each one was triggering an immediate
        # width change + scrollregion recompute across ~300 widgets, and that
        # reflow work couldn't keep up with the event rate -- which is what
        # "goes to Not Responding while shrinking the window" looks like.
        # Coalescing to one recompute ~80ms after resizing activity pauses
        # keeps a drag responsive regardless of how many events fire during it.
        self._resize_job=None
        def _debounced_reflow(event=None,width=None):
            if self._resize_job is not None: self.after_cancel(self._resize_job)
            self._resize_job=self.after(80,lambda:self._apply_reflow(width))
        self.rows.bind("<Configure>",lambda _e:_debounced_reflow())
        self.canvas.bind("<Configure>",lambda e:_debounced_reflow(width=e.width))
        self._debounced_reflow=_debounced_reflow
        # Apply to every child card so a laptop trackpad works over team names and line fields too.
        self.bind_all("<MouseWheel>",self._mousewheel)
        self.bind_all("<Button-4>",lambda _e:self.canvas.yview_scroll(-3,"units"))
        self.bind_all("<Button-5>",lambda _e:self.canvas.yview_scroll(3,"units"))
        self.bind_all("<Prior>",lambda _e:self.canvas.yview_scroll(-1,"pages"),add="+")
        self.bind_all("<Next>",lambda _e:self.canvas.yview_scroll(1,"pages"),add="+")
        self.bind_all("<Home>",lambda _e:self.canvas.yview_moveto(0),add="+")
        self.bind_all("<End>",lambda _e:self.canvas.yview_moveto(1),add="+")
        tk.Label(self,textvariable=self.status,bg=C["status_bg"],fg=C["status_fg"],anchor="w",padx=16,pady=7).pack(fill="x",side="bottom")
        self._bind_wheel_recursive(self.outer)

    def _apply_reflow(self,width):
        if width is not None:
            try: self.canvas.itemconfigure(self.window,width=width)
            except tk.TclError: return
        try: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        except tk.TclError: pass

    def _mousewheel(self,event):
        # TEMPORARY diagnostic: the last two fixes for "scroll only works over
        # the scrollbar" didn't actually fix it, so guessing a third blind fix
        # isn't worth your time. This logs every wheel event this handler
        # actually receives -- widget, delta, position -- to scroll_debug.txt
        # next to the database. Scroll over a few different spots (a team
        # name, a button, empty space, the scrollbar itself), then send that
        # file back and the real fix can be precise instead of another guess.
        try:
            with open(APP_DIR/"scroll_debug.txt","a",encoding="utf-8") as f:
                f.write(f"{datetime.now().strftime('%H:%M:%S')}  widget={event.widget}  class={event.widget.winfo_class() if hasattr(event.widget,'winfo_class') else '?'}  delta={event.delta}  x={event.x_root} y={event.y_root}\n")
        except Exception: pass
        # Windows mice report +/-120; precision trackpads often report much smaller values.
        # One canvas unit per event keeps both responsive without the old large jumps.
        if event.delta: self.canvas.yview_scroll(-1 if event.delta > 0 else 1,"units")
        return "break"

    def _bind_wheel_recursive(self,widget):
        """Bind the wheel directly on this widget and every widget inside it.
        bind_all (above) is supposed to cover the whole app, but it sits at
        the lowest-priority bind tag and can be shadowed by a widget's own
        class bindings, an open dialog stealing focus, or DPI/coordinate
        quirks. A direct binding on each widget doesn't depend on any of
        that, so it's the reliable fallback for cards/labels/entries the
        cursor is actually resting on."""
        widget.bind("<MouseWheel>",self._mousewheel,add="+")
        widget.bind("<Button-4>",lambda _e:self.canvas.yview_scroll(-3,"units"),add="+")
        widget.bind("<Button-5>",lambda _e:self.canvas.yview_scroll(3,"units"),add="+")
        for child in widget.winfo_children(): self._bind_wheel_recursive(child)

    def queue_render(self):
        """Coalesce background updates so they cannot repeatedly interrupt a scroll."""
        if self._render_waiting: return
        self._render_waiting=True
        self.after(180,self._finish_queued_render)

    def _finish_queued_render(self):
        # render() (the on-screen dashboard) is deliberately skipped here -- that
        # window is withdrawn and never shown, so rebuilding its ~16 matchup cards
        # on every background refresh (schedule/injuries/lines/rankings/weather/
        # grades/referees, each of which calls queue_render when it lands) was 2s
        # of pure waste per call. Only the print page, which is what's actually
        # visible, gets regenerated.
        self._render_waiting=False
        try: self._refresh_print_html()
        except Exception: pass

    def refresh_weeks(self):
        if not 1 <= self.week.get() <= 18: self.week.set(1)

    def set_week(self, number):
        self.week.set(number); self.unit_grades={}; self.refresh_unit_grades(); self.view="schedule"; self.subtitle.configure(text="Teams, DraftKings line, total, public action, and final cover result")
        if not self.matchup_header.winfo_manager(): self.matchup_header.pack(fill="x",before=self.outer)
        self.render(preserve_scroll=False)

    def show_rankings(self):
        self.view="rankings"; self.subtitle.configure(text=f"WEEK {self.week.get()} CONSENSUS POWER RANKINGS  ·  COLOR SCALE: 10 DARK GREEN  →  NEON GREEN  →  YELLOW  →  ORANGE  →  1 RED")
        self.matchup_header.pack_forget(); self.render(preserve_scroll=False)

    def show_picks(self):
        self.view="picks"; self.subtitle.configure(text="YOUR PICKS AGAINST THE SPREAD, EVERY WEEK THIS SEASON — RECORDED AUTOMATICALLY WHEN YOU TAP A TEAM")
        self.matchup_header.pack_forget(); self.render(preserve_scroll=False)

    def toggle_theme(self):
        """Tk has no CSS-style live re-theme -- every widget's colors are baked in
        at creation. Rebuilding the whole UI from scratch is simpler and far less
        error-prone than hunting down and reconfiguring ~150 individual widgets."""
        self.theme_name="light" if self.theme_name=="dark" else "dark"
        self.C=THEMES[self.theme_name]
        save_prefs({"theme":self.theme_name})
        for w in self.winfo_children(): w.destroy()
        self.configure(bg=self.C["bg"])
        self.build()
        if self.view=="rankings": self.show_rankings()
        elif self.view=="picks": self.show_picks()
        else: self.set_week(self.week.get())

    def _compute_top_plays(self, games):
        """Games where the automatic Lean Index has a real lean (|score-5|>=1),
        sorted strongest-lean-first. Shared by the live app's summary banner
        and the print page's Top Plays box so the two can never disagree."""
        top_plays=[]
        for g in games:
            home_team,away_team=self.team(g["home"]),self.team(g["away"])
            if home_team["consensus_rating"] is None or away_team["consensus_rating"] is None or g["dk_spread"] is None: continue
            home_rest,away_rest=self.days_rest(g["home"],g["kickoff"]),self.days_rest(g["away"],g["kickoff"])
            projection=self.projected_game(g,home_team,away_team,home_rest,away_rest)
            score,_breakdown=self.confidence_score(g,home_team,away_team,home_rest,away_rest)
            if abs(score-5.0)<1: continue
            fav=g["home"] if projection["home_spread"]<0 else g["away"]
            opp=g["away"] if fav==g["home"] else g["home"]
            fav_spread=projection["home_spread"] if projection["home_spread"]<0 else -projection["home_spread"]
            try:
                dt=datetime.fromisoformat(g["kickoff"].replace("Z","+00:00")).astimezone()
                hour12=dt.hour%12 or 12
                kickoff=f"{dt.month}/{dt.day}/{dt.year} {hour12}:{dt:%M %p}"
            except Exception: kickoff=g["kickoff"]
            top_plays.append({"kickoff":kickoff,"score":score,"fav":fav.split()[-1].upper(),"fav_spread":fav_spread,"opp":opp.split()[-1].upper()})
        top_plays.sort(key=lambda p: abs(p["score"]-5.0), reverse=True)
        return top_plays

    def _build_print_page(self,week_number):
        """Builds the printable page HTML (string) for the given week -- same
        layout, colors, and PICK/BET badges the old on-screen dashboard used.
        Pure string-building, no file I/O or browser launch -- see print_week
        (writes + opens once) and _refresh_print_html (writes only, used to
        keep an already-open browser tab current as new data comes in)."""
        self.ensure_unit_grades()
        self._compute_team_ranks()
        with con() as c:
            games=c.execute("SELECT * FROM games WHERE week=? ORDER BY kickoff",(week_number,)).fetchall()

        def esc(s): return html.escape(str(s))
        def side_html(team_row,extra,questionable,out,side_bg,side_class,weather_line=None,spacer_lines=0,ref_line=None,rest_days=None):
            rating="—" if team_row["consensus_rating"] is None else f"{team_row['consensus_rating']:.1f}"
            color=self.rating_color(team_row["consensus_rating"])
            my_rank=getattr(self,"_team_rank",{}).get(team_row["name"])
            rank_count=getattr(self,"_team_rank_count",0)
            rank_suffix=f"  ·  #{my_rank}/{rank_count}" if rating!="—" and my_rank is not None else ""
            parts=[f'<div class="team">{esc(team_row["name"].upper())}</div>']

            if extra: parts.append(f'<div class="meta gray">{esc(extra)}</div>')
            if weather_line: parts.append(f'<div class="meta {"warn" if "ALERT" in weather_line else "gray"}">{esc(weather_line)}</div>')
            if ref_line: parts.append(f'<div class="meta gray">{esc(ref_line)}</div>')
            # The home side's venue (+ often weather + often ref) line(s) have no
            # away-side equivalent -- without a matching spacer, RANKING/QUESTIONABLE/
            # OUT-IR sit at different heights between the two columns. Mirror the line count.
            parts.extend('<div class="meta gray">&nbsp;</div>' for _ in range(spacer_lines))
            parts.append(f'<div class="rating-grades"><div class="meta rank" style="color:{color}">RANKING  {esc(rating)}{rank_suffix}</div>{self.grade_html(team_row["name"])}</div>')
            if questionable: parts.append(f'<div class="meta warn">QUESTIONABLE  {esc(questionable)}</div>')
            if out: parts.append(f'<div class="meta bad">OUT / IR  {esc(out)}</div>')
            if rest_days is not None:
                parts.append(f'<div class="meta {"info" if rest_days>=10 else "gray"}">{rest_days} DAYS REST</div>')
            watermark=f'<img class="watermark" src="https://a.espncdn.com/i/teamlogos/nfl/500/{esc(team_row["abbr"])}.png" alt="">'
            return f'<td class="side {side_class}" style="background:{side_bg}">{watermark}{"".join(parts)}</td>'

        cards=[]
        for g in games:
            try:
                dt=datetime.fromisoformat(g["kickoff"].replace("Z","+00:00")).astimezone()
                hour12=dt.hour%12 or 12
                kickoff=f"{dt.month}/{dt.day}/{dt.year} {hour12}:{dt:%M %p}"
            except Exception: kickoff=g["kickoff"]
            home_team,away_team=self.team(g["home"]),self.team(g["away"])
            venue=self.venue_label(g["home"],g["away"])
            home_questionable,home_out=self.injury_lines(g["home"])
            away_questionable,away_out=self.injury_lines(g["away"])
            home_selected,away_selected=g["pick_side"]=="home",g["pick_side"]=="away"
            starred=bool(g["bet_star"])
            home_bg="#e6f5e9" if home_selected and starred else ("#fff4d8" if home_selected else "#fff")
            away_bg="#e6f5e9" if away_selected and starred else ("#fff4d8" if away_selected else "#fff")
            neutral=venue.startswith("NEUTRAL SITE")
            loud=" 🔊" if g["home"] in NOISE_ELITE and not neutral else ""
            home_extra=(venue if neutral else "HOME FIELD  ·  "+venue)+loud
            w=self.weather(g["event_id"])
            weather_line=None
            if w:
                if w["dome"]: weather_line="DOME / ROOF ASSUMED CLOSED"
                elif w["temperature"] is not None: weather_line=self.weather_text(w)
            home_rest,away_rest=self.days_rest(g["home"],g["kickoff"]),self.days_rest(g["away"],g["kickoff"])
            assignment=self.referee_assignment(week_number,g["home"],g["away"])
            ref_line=None
            if assignment:
                if assignment["home_ats_pct"] is None:
                    ref_line=f"REF  {assignment['referee']}  ·  no career sample yet"
                elif assignment["over_pct"] is not None:
                    ref_line=f"REF  {assignment['referee']}  ·  Home ATS {assignment['home_ats_pct']:.1f}%  ·  Over {assignment['over_pct']:.1f}%"
                else:
                    ref_line=f"REF  {assignment['referee']}  ·  Home ATS {assignment['home_ats_pct']:.1f}%"
            home_html=side_html(home_team,home_extra,home_questionable,home_out,home_bg,"home"+(" selected" if home_selected else ""),weather_line,ref_line=ref_line,rest_days=home_rest)
            away_html=side_html(away_team,None,away_questionable,away_out,away_bg,"away"+(" selected" if away_selected else ""),spacer_lines=1+(1 if weather_line else 0)+(1 if ref_line else 0),rest_days=away_rest)

            spread="—" if g["dk_spread"] is None else f"{g['home'].split()[-1].upper()} {g['dk_spread']:+g}"
            total="O/U —" if g["dk_total"] is None else f"O/U {g['dk_total']:g}"
            mid=[f'<div class="kickoff">{esc(kickoff)}</div>']
            if frozenset({g["home"],g["away"]}) in RIVALRIES:
                mid.append('<div class="meta info">⚔ RIVALRY GAME</div>')
            score,_breakdown=self.confidence_score(g,home_team,away_team,home_rest,away_rest)
            score_cls="good" if score>5.5 else ("bad" if score<4.5 else "gray")
            if home_team["consensus_rating"] is not None and away_team["consensus_rating"] is not None:
                projection=self.projected_game(g,home_team,away_team,home_rest,away_rest)
                if projection["home_spread"]==0: your_spread_txt="PICK 'EM"
                else:
                    fav=g["home"] if projection["home_spread"]<0 else g["away"]
                    fav_spread=projection["home_spread"] if projection["home_spread"]<0 else -projection["home_spread"]
                    your_spread_txt=f"{fav.split()[-1].upper()} {fav_spread:+g}"
                # One line: the model's own spread plus how strong its overall lean
                # is (Lean Index also weighs line movement/recent form, so it can
                # legitimately disagree with the favorite named here -- that shows
                # up as a low/opposite-feeling number rather than a second team name).
                combined_line=f"YOUR SPREAD: {your_spread_txt}  ·  LEAN {score:g}/10"
            else:
                score_label,_fav_team=self.confidence_label(score,g["home"],g["away"])
                combined_line=f"LEAN INDEX {score:g}/10 — {score_label}"
            mid.append(f'<div class="meta {score_cls}">{esc(combined_line)}</div>')
            mid.append(f'<div class="spread">{esc(spread)}</div>')
            mid.append(f'<div class="total">{esc(total)}</div>')
            opening=self.opening_line(g["event_id"])
            if opening and g["dk_spread"] is not None and opening["home_spread"] is not None and opening["home_spread"]!=g["dk_spread"]:
                mid.append(f'<div class="meta gray">OPENED {esc(g["home"].split()[-1].upper())} {opening["home_spread"]:+g} → NOW {g["dk_spread"]:+g}</div>')
            if g["home_bets"] is not None:
                mid.append(f'<div class="meta gray">{esc(g["home"].split()[-1])}  {g["home_bets"]}% bets · {g["home_handle"]}% money</div>')
                mid.append(f'<div class="meta gray">{esc(g["away"].split()[-1])}  {g["away_bets"]}% bets · {g["away_handle"]}% money</div>')
            if g["home_score"] is not None and g["away_score"] is not None:
                if g["dk_spread"] is None: cover="FINAL"
                else:
                    result=g["home_score"]-g["away_score"]+g["dk_spread"]
                    cover=f"{g['home'].split()[-1].upper()} COVERED" if result>0 else (f"{g['away'].split()[-1].upper()} COVERED" if result<0 else "PUSH")
                mid.append(f'<div class="meta dark">FINAL {g["home_score"]}–{g["away_score"]}  ·  {esc(cover)}</div>')
            ou=self.ou_result(g)
            if ou:
                ou_cls="good" if ou=="WON" else ("bad" if ou=="LOST" else "gray")
                mid.append(f'<div class="meta {ou_cls} pick-only">YOUR O/U PICK ({esc(g["ou_pick"].upper())}): {ou}</div>')
            if g["auto_pick_side"]:
                auto_team=(g["home"] if g["auto_pick_side"]=="home" else g["away"]).split()[-1].upper()
                auto_result=self.auto_pick_result(g)
                auto_cls="good" if auto_result=="WON" else ("bad" if auto_result=="LOST" else "gray")
                auto_glyph="✓ " if auto_result=="WON" else ("✗ " if auto_result=="LOST" else "")
                mid.append(f'<div class="meta {auto_cls}">{auto_glyph}★ ALGORITHM PICK: {esc(auto_team)}</div>')
                mid.append(f'<div class="meta gray">{esc(self.auto_pick_note(g,home_rest,away_rest))}</div>')

            cards.append(f'<table class="game"><tr>{home_html}<td class="mid">{"".join(mid)}</td>{away_html}</tr></table>')

        css="""
:root{--page:#eeeeee;--ink:#000}
body{background:var(--page);color:#1b1b20;font-family:"Segoe UI",Arial,Helvetica,sans-serif;padding:0 0 24px 0;margin:0}
.masthead{background:#0b0b0d;color:#fff;padding:16px 24px;display:flex;align-items:center;gap:14px}
.masthead .bar{width:6px;height:28px;background:#c7132d;display:inline-block}
.masthead h1{font-size:18px;margin:0;letter-spacing:.5px}
.controls{background:#19191c;color:#d9d9dd;padding:10px 24px;font-size:11px;display:flex;align-items:center;gap:10px}
.controls button{font-family:inherit;font-size:11px;font-weight:bold;padding:5px 12px;border:0;border-radius:3px;background:#303036;color:#fff;cursor:pointer}
.controls button.active{background:#c7132d}
p.sub{font-size:11px;color:#5d5d63;margin:0;padding:9px 24px;background:var(--page)}
.week-wrap{padding:0 16px}
table.game{border-collapse:collapse;width:100%;margin:6px 0;background:#fff;border:1px solid #d9d9dd}
table.game td{vertical-align:top;padding:10px 16px;width:34%}
table.game td.side{position:relative;z-index:0}
table.game td.side.away{text-align:right}
table.game td.mid{width:32%;background:#fbfbfc;border-left:1px solid #e2e2e5;border-right:1px solid #e2e2e5;text-align:center}
.watermark{position:absolute;bottom:4px;width:90px;opacity:.14;z-index:-1;pointer-events:none}
td.side.home .watermark{right:4px}
td.side.away .watermark{left:4px}
.rating-grades{display:flex;align-items:flex-start;justify-content:space-between;gap:6px;margin-top:6px}
td.side.away .rating-grades{flex-direction:row-reverse}
.rating-grades>.rank{padding-top:8px;white-space:nowrap}
table.game td.side{padding-bottom:100px}
.unit-grades{margin-top:0;text-align:right}
td.side.away .unit-grades{text-align:left}
.grade-chip{display:inline-block;min-width:56px;padding:5px 9px;margin:0 2px;text-align:center;border-radius:5px}
.grade-title{display:block;font-size:8px;font-weight:700;letter-spacing:.3px}
.grade-chip strong{display:block;font-size:25px;font-weight:800;line-height:1.2}
.grade-basis{font-size:9px;color:#61616b;margin-top:3px}
body.bw .grade-chip{color:#000 !important;background:#fff !important;border:1px solid #000}
body.bw .grade-basis{color:#000 !important}
.team{font-size:15px;font-weight:bold;color:#1b1b20}
.meta{font-size:8pt;margin-top:2px;font-weight:bold}
.meta.gray{color:#71717a;font-weight:normal}
.meta.dark{color:#151519}
.meta.info{color:#2f6688}
.meta.warn{color:#b66c00}
.meta.bad,.bad{color:#b01d2d}
.meta.good,.good{color:#16884a}
.kickoff{font-size:9pt;font-weight:bold;color:#6f6f77}
.spread{font-size:15pt;font-weight:bold;color:#c7132d;margin-top:4px}
.total{font-size:11pt;font-weight:bold;color:#202026}
@media print{.controls{display:none}}
body.bw{--page:#fff}
body.bw .masthead{background:#fff !important;color:#000 !important;border-bottom:2px solid #000}
body.bw .masthead .bar{background:#000 !important}
body.bw p.sub{color:#000 !important}
body.bw table.game{border:1px solid #000 !important}
body.bw table.game td.mid{background:#fff !important;border-left:1px solid #000 !important;border-right:1px solid #000 !important}
body.bw td.side{background:#fff !important}
body.bw td.side.selected{border:2px solid #000}
body.bw .team,body.bw .spread,body.bw .total,body.bw .kickoff{color:#000 !important}
body.bw .meta,body.bw .meta.gray,body.bw .meta.dark,body.bw .meta.info,body.bw .meta.warn,body.bw .meta.bad,body.bw .meta.good,body.bw .bad,body.bw .good{color:#000 !important;font-weight:bold}
body.bw .watermark{display:none}
.top-plays{margin:12px 16px 14px;background:#151519;border-radius:6px;padding:12px 18px;color:#fff}
.top-plays h2{margin:0 0 8px;font-size:12px;letter-spacing:.5px;color:#f2d98c}
.top-plays ul{list-style:none;margin:0;padding:0;display:flex;flex-wrap:wrap;gap:8px}
.top-plays li{background:rgba(255,255,255,.08);border-radius:4px;padding:8px 12px;font-size:11px;font-weight:bold;min-width:150px}
.top-plays li .tp-kickoff{display:block;font-weight:normal;font-size:9px;opacity:.75}
.top-plays li .tp-edge{display:block;font-weight:normal;font-size:9px;color:#8fd6a8;margin-top:3px}
body.bw .top-plays{background:#fff !important;color:#000 !important;border:2px solid #000}
body.bw .top-plays h2{color:#000 !important}
body.bw .top-plays li{background:#fff !important;border:1px solid #000}
body.bw .top-plays li .tp-edge{color:#000 !important}
"""
        script="""
function setMode(bw){
  document.body.classList.toggle('bw',bw);
  document.getElementById('btn-color').classList.toggle('active',!bw);
  document.getElementById('btn-bw').classList.toggle('active',bw);
  try{localStorage.setItem('printMode',bw?'bw':'color');}catch(e){}
}
document.addEventListener('DOMContentLoaded',function(){
  var saved='color';
  try{saved=localStorage.getItem('printMode')||'color';}catch(e){}
  setMode(saved==='bw');
});
"""
        top_plays=self._compute_top_plays(games)
        if top_plays:
            items=[]
            for p in top_plays[:5]:
                items.append(f'<li><span class="tp-kickoff">{esc(p["kickoff"])}</span>{esc(p["fav"])} {p["fav_spread"]:+g}<span class="tp-edge">vs {esc(p["opp"])}  ·  {p["score"]:g}/10 lean index</span></li>')
            top_plays_html=f'<div class="top-plays"><h2>MOST FAVORED GAMES THIS WEEK</h2><ul>{"".join(items)}</ul></div>'
        else:
            top_plays_html=""
        page=f"""<!doctype html><html><head><meta charset="utf-8"><title>Week {week_number} Matchups</title>
<meta http-equiv="refresh" content="120">
<style>{css}</style></head><body>
<p style="font:12px Arial">Offense/defense: EPA efficiency grades, with prior-season shrinkage; descriptive, not an ATS probability. Grades reflect the selected week; pick history shows current selected-week grades. Weather alerts cover kickoff through +3 hours. Roof status is assumed.</p>
<div class="masthead"><span class="bar"></span><h1>MY NFL HANDICAPPER — WEEK {week_number}</h1></div>
<div class="controls">
<button id="btn-color" onclick="setMode(false)">COLOR</button>
<button id="btn-bw" onclick="setMode(true)">INK-SAVING (BLACK &amp; WHITE)</button>
</div>
<p class="sub">Teams, DraftKings line, total, public action, and your picks &middot; printed {datetime.now():%m/%d/%Y %I:%M %p}</p>
{top_plays_html}
<div class="week-wrap">{"".join(cards) if cards else "<p>No games loaded for this week yet.</p>"}</div>
<script>{script}</script>
</body></html>"""
        return page

    def _refresh_print_html(self):
        """Rewrites print_week.html in place for whichever week is current, without
        opening a new browser window/tab. Called after every background data
        refresh so the page the browser already has open (which self-reloads every
        two minutes, see the meta refresh tag above) stays current -- lines,
        scores, injuries, weather, referees, rankings -- with no dashboard UI
        involved at all."""
        page=self._build_print_page(self.week.get())
        (APP_DIR/"print_week.html").write_text(page,encoding="utf-8")

    def print_week(self):
        """Writes the printable page for the current week and opens it in the
        default browser so Ctrl+P (with its own "Save as PDF" option) does the
        printing, instead of this app talking to a printer driver directly."""
        week_number=self.week.get()
        page=self._build_print_page(week_number)
        path=APP_DIR/"print_week.html"
        path.write_text(page,encoding="utf-8")
        # os.startfile hands the file straight to Windows' registered default-browser
        # handler -- more reliable than webbrowser.open here, which on some default-browser
        # setups (e.g. Edge already running) opens a fresh, still-loading blank tab
        # instead of navigating an existing one to the file.
        try: os.startfile(str(path))
        except Exception: webbrowser.open(path.as_uri())
        self.status.set(f"Printable Week {week_number} page opened in your browser — pick Color or Ink-Saving, then Ctrl+P.")

    def paint_week_tabs(self):
        C=self.C
        for number, button in self.week_buttons.items():
            active=number == self.week.get()
            button.configure(bg=C["accent"] if active else C["tab_inactive"],fg="white",activebackground=C["accent_hover"],activeforeground="white")
        self.power_button.configure(bg=C["accent"] if self.view=="rankings" else C["tab_inactive"],fg="white",activebackground=C["accent_hover"],activeforeground="white")
        self.picks_button.configure(bg=C["accent"] if self.view=="picks" else C["tab_inactive"],fg="white",activebackground=C["accent_hover"],activeforeground="white")

    def team(self,name):
        with con() as c: return c.execute("SELECT * FROM teams WHERE name=?",(name,)).fetchone()

    def team_grade_values(self, name):
        abbr=dict(TEAMS).get(name, "").upper()
        abbr={"LAR":"LA","WSH":"WAS"}.get(abbr,abbr)
        item=self.unit_grades.get(abbr,{})
        off=item.get("off_grade","N/A"); defense=item.get("def_grade","N/A")
        basis=f"2026: {item['games']}g + 2025 prior" if item.get("games") else "2025 prior"
        if not item: basis="stats unavailable"
        return off,defense,basis

    def team_epa_net(self, name):
        """Net EPA power (offense z-score minus defense z-score, prior-season-blended,
        shrinking toward pure current-season data as unit_grades accumulates real
        2026 games -- see grades()). None until the league has enough sample for
        relative grading. Used as a secondary signal in rating_margin() alongside
        the scraped consensus_rating, since EPA-vs-market alone backtested to a
        similar but not identical edge as human power rankings."""
        abbr=dict(TEAMS).get(name, "").upper()
        abbr={"LAR":"LA","WSH":"WAS"}.get(abbr,abbr)
        item=self.unit_grades.get(abbr,{})
        off_z=item.get('off_z'); def_z=item.get('def_z')
        if off_z is None and def_z is None: return None
        # def value is already stored as -epa_allowed (see team_units), so higher
        # def_z is a better defense too -- net strength adds the two, doesn't subtract.
        return (off_z or 0.0) + (def_z or 0.0)

    def grade_line(self, name, short=False):
        off,defense,basis=self.team_grade_values(name)
        if short: return f"O {off} / D {defense}"
        return f"OFF {off}  ·  DEF {defense}  ({basis})"

    def grade_colors(self, grade, print_mode=False):
        # Each grade keeps its letter, so the meaning does not depend on color.
        light={"A":("#137333","#e6f4ea"),"B":("#47751b","#eef5df"),
               "C":("#805b00","#fff4cf"),"D":("#ad4b00","#ffeadb"),
               "F":("#b4232c","#fde7e9"),"N/A":("#61616b","#ededf0")}
        dark={"A":("#58d987","#153b26"),"B":("#abd66f","#2c3c1d"),
              "C":("#f2d16b","#413719"),"D":("#ffad6c","#482b19"),
              "F":("#ff7d85","#4a2028"),"N/A":("#b6b6c0","#303036")}
        palette=light if print_mode or self.theme_name=="light" else dark
        return palette.get(grade,palette["N/A"])

    def grade_badges(self, parent, name, side, background):
        off,defense,basis=self.team_grade_values(name)
        area=tk.Frame(parent,bg=background)
        area.pack(side="right" if side=="home" else "left",pady=(2,3))
        inner=tk.Frame(area,bg=background)
        inner.pack(side="right" if side=="home" else "left")
        cells=tk.Frame(inner,bg=background); cells.pack()
        for label,grade in (("OFFENSE",off),("DEFENSE",defense)):
            fg,bg=self.grade_colors(grade)
            cell=tk.Frame(cells,bg=bg,padx=12,pady=5)
            cell.pack(side="left",padx=3)
            tk.Label(cell,text=label,bg=bg,fg=fg,font=("Segoe UI",8,"bold")).pack()
            tk.Label(cell,text=grade,bg=bg,fg=fg,font=("Segoe UI",22,"bold")).pack()
        tk.Label(inner,text=basis,bg=background,fg=self.C["meta_fg"],font=("Segoe UI",8),wraplength=205).pack(pady=(3,0))

    def grade_html(self, name):
        off,defense,basis=self.team_grade_values(name)
        parts=[]
        for label,grade in (("OFFENSE",off),("DEFENSE",defense)):
            fg,bg=self.grade_colors(grade,print_mode=True)
            parts.append(f'<span class="grade-chip" style="color:{fg};background:{bg}"><span class="grade-title">{label}</span><strong>{html.escape(grade)}</strong></span>')
        return '<div class="unit-grades"><div>'+''.join(parts)+'</div><div class="grade-basis">'+html.escape(basis)+'</div></div>'

    def _load_unit_grades(self, week):
        """Fetch/parse the offense-defense grade inputs for `week` (cached CSVs,
        refreshed once a day) and return (unit_grades_dict, status_text). Shared by
        the threaded refresh_unit_grades() and the synchronous ensure_unit_grades()
        (used right before printing, so a printed page never shows stale N/A grades
        just because the background refresh hadn't landed yet)."""
        cache=APP_DIR / "stats_cache"; cache.mkdir(exist_ok=True)
        data={}; notes=[]
        for year in (2025,2026):
            path=cache / f"team_{year}.csv"
            fresh=path.exists() and datetime.now().timestamp()-path.stat().st_mtime < 86400
            if not fresh:
                try:
                    with urlopen(Request(STATS_URL.format(year=year),headers={"User-Agent":"NFL personal board"}),timeout=30) as response: raw=response.read().decode("utf-8")
                    team_units(raw,year)
                    path.write_text(raw,encoding="utf-8")
                except Exception:
                    notes.append(f"{year} feed unavailable" + ("; cached data" if path.exists() else ""))
            data[year]=team_units(path.read_text(encoding="utf-8"),year,week if year==2026 else 99) if path.exists() else {}
        result=grades(data[2025],data[2026])
        state=f"nflverse EPA · before Week {week} · daily downloads; checks every 15 min while open · " + ("; ".join(notes) if notes else "cached feeds available")
        return result,state

    def refresh_unit_grades(self):
        if self._stats_loading: return
        self._stats_loading=True
        week=self.week.get()
        def worker():
            try:
                result,state=self._load_unit_grades(week)
                def done():
                    self._stats_loading=False
                    if self.week.get()!=week: self.refresh_unit_grades(); return
                    self.unit_grades=result; self.stats_state=state; self.queue_render()
                self._ui_queue.put(done)
            except Exception as ex:
                def failed(ex=ex):
                    self._stats_loading=False; self.stats_state=f"Stats failed: {ex}"; self.queue_render()
                self._ui_queue.put(failed)
        threading.Thread(target=worker,daemon=True).start()

    def ensure_unit_grades(self):
        """Block briefly (usually instant, thanks to the day-long CSV cache) to
        guarantee self.unit_grades is populated before print_week() reads it --
        otherwise a page printed right after switching weeks (before the
        background refresh_unit_grades() thread finishes) would freeze in every
        grade as N/A forever, since the printed HTML is a static snapshot that
        never gets the screen's later queue_render() update."""
        if self.unit_grades: return
        try:
            result,state=self._load_unit_grades(self.week.get())
            self.unit_grades=result; self.stats_state=state
        except Exception as ex:
            self.stats_state=f"Stats failed: {ex}"

    def weather_border_color(self,event_id):
        """Card-border color from this game's weather_flags() alert string (see
        weather_flags) -- most-specific condition wins so a snowy, windy game reads
        as snow rather than getting buried under the more common wind flag. Red is
        the fallback for anything flagged (e.g. precip chance) that isn't
        specifically rain/snow/wind. Domes and calm games get no border at all."""
        w=self.weather(event_id)
        if not w or w["dome"]: return None
        alert=w["alert"] if "alert" in w.keys() else None
        if not alert: return None
        if "SNOW" in alert: return "#ffffff"
        if "RAIN" in alert: return "#2f6fdb"
        if "WIND" in alert or "GUSTS" in alert: return "#8a8a92"
        return "#c7132d"

    def weather_text(self,w):
        if w["dome"]: return "DOME / roof assumed closed"
        stale=False
        try: stale=(datetime.now()-datetime.fromisoformat(w["checked_at"])).total_seconds()>10800
        except (TypeError,ValueError): stale=True
        if w["temperature"] is None or w["wind"] is None: return "Forecast unavailable"
        base=f"{w['temperature']:.0f}°F · game max wind {w['wind']:.0f} mph"
        alert=w["alert"] if "alert" in w.keys() else None
        return base + ("\nWEATHER ALERT: "+alert if alert else "") + ("\nSTALE forecast — refresh required" if stale else "")

    def rating_color(self,rating):
        """A 10-step heat scale: elite dark green through neon/lime, then warm colors.
        The two darkest extremes (10 and 1) get brighter dark-mode variants -- the
        light-theme shades are too low-contrast against a dark card background."""
        if rating is None:return self.C["meta_fg"]
        colors={
            10:"#075f35", # forest green
            9:"#008c4d",  # emerald
            8:"#00b85c",  # vivid green
            7:"#38d467",  # neon green
            6:"#91da31",  # lime
            5:"#d2c62a",  # yellow-green
            4:"#e9a51d",  # gold
            3:"#e67618",  # orange
            2:"#d94724",  # red-orange
            1:"#ad1f2d",  # red
        }
        if self.theme_name=="dark": colors.update({10:"#1ea968",1:"#e8465c"})
        return colors[max(1,min(10,int(round(rating))))]

    def rating_margin(self, home_rating, away_rating, home_name, away_name=None):
        """Projected home spread (negative means the home team is favored).

        Ratings are a 1–10 consensus *ranking* scale, not point ratings, so their
        difference is deliberately compressed.  This is a transparent estimate,
        not a market replacement or a claim of ATS accuracy.
        """
        # Loud-stadium noise bonus lives solely in automated_factors()'s "Home
        # crowd / stadium" line, which projected_game() sums on top of this
        # margin -- baking it in here too double-counted it for NOISE_ELITE /
        # NOISE_LOUD teams.
        home_field = 1.5
        if (home_name,away_name) in NEUTRAL_SITES: home_field=0.0
        margin = -((home_rating - away_rating) * 1.15 + home_field)
        # Rivalry games run closer than the rating gap says they should -- shave
        # up to RIVALRY_SHRINK points off whichever side is favored, never enough
        # to flip the favorite. See RIVALRIES above for the pair list.
        if away_name is not None and frozenset({home_name,away_name}) in RIVALRIES:
            RIVALRY_SHRINK = 1.0
            shrink = min(abs(margin), RIVALRY_SHRINK)
            margin -= shrink if margin > 0 else -shrink
        return round_half(margin)

    def injury_factor(self, home, away):
        """Small, report-based directional adjustment; never assumes a backup is a starter."""
        weights={"QB":1.25,"OT":0.35,"OG":0.30,"C":0.30,"WR":0.25,"RB":0.20,"TE":0.15,
                 "EDGE":0.25,"DE":0.25,"DT":0.20,"LB":0.15,"CB":0.20,"S":0.15}
        def burden(team):
            with con() as c:
                rows=c.execute("SELECT position,status FROM injuries WHERE team=?",(team,)).fetchall()
            total=0.0
            for row in rows:
                status=(row["status"] or "").lower()
                if status not in ("out","doubtful","questionable","injured reserve","ir"): continue
                multiplier=1.0 if status in ("out","doubtful","injured reserve","ir") else 0.35
                total += weights.get((row["position"] or "").upper(),0.08)*multiplier
            return min(1.5,total)
        return round_half(burden(away)-burden(home))

    def automated_factors(self, g, home_rest, away_rest):
        """All score-game inputs that can be observed locally, signed for the home team."""
        home,away=g["home"],g["away"]
        factors=[("Injury report",self.injury_factor(home,away))]
        venue=0.0 if (home,away) in NEUTRAL_SITES else (0.50 if home in NOISE_ELITE else (0.25 if home in NOISE_LOUD else 0.0))
        factors.append(("Home crowd / stadium",venue))
        rest=0.0
        if home_rest is not None and away_rest is not None:
            if home_rest>=13 and away_rest<13: rest+=0.75
            elif away_rest>=13 and home_rest<13: rest-=0.75
            if home_rest<=4 and away_rest>4: rest-=0.50
            elif away_rest<=4 and home_rest>4: rest+=0.50
        factors.append(("Rest / bye week",rest))
        short_week_overlap=0.0
        if home_rest is not None and away_rest is not None:
            if home_rest<=4 and away_rest>4: short_week_overlap=-0.50
            elif away_rest<=4 and home_rest>4: short_week_overlap=0.50
        factors.append(("Travel",self.situational_factor(home,away,g["kickoff"],home_rest,away_rest) - short_week_overlap))
        weather=self.weather(g["event_id"])
        weather_nudge=0.0
        if weather and not weather["dome"] and (weather["wind"] or 0)>=15: weather_nudge=0.10
        factors.append(("Weather",weather_nudge))
        home_net=self.team_epa_net(home); away_net=self.team_epa_net(away)
        epa=0.0
        if home_net is not None and away_net is not None:
            # Secondary power signal alongside consensus_rating: prior-season-blended
            # EPA net rating (see team_epa_net), scaled/capped to a magnitude similar
            # to the other situational factors rather than dominating rating_margin's
            # consensus-driven number.
            epa=max(-1.5,min(1.5,(home_net-away_net)*0.6))
        factors.append(("EPA power (blended, prior+current season)",epa))
        factors.append(("Referee crew (career home ATS lean)",self.referee_factor(g["week"],home,away)))
        return factors

    def projected_game(self, g, home_team, away_team, home_rest, away_rest):
        """Return transparent estimated score, total, and market-comparison lean."""
        margin=-self.rating_margin(home_team["consensus_rating"],away_team["consensus_rating"],g["home"],g["away"])
        factors=self.automated_factors(g,home_rest,away_rest)
        margin=round_half(margin+sum(value for _,value in factors))
        off_h,def_h,_=self.team_grade_values(g["home"]); off_a,def_a,_=self.team_grade_values(g["away"])
        grade_value={"A":1.0,"B":0.4,"C":0.0,"D":-0.4,"F":-1.0,"N/A":0.0}
        total=43.0 + grade_value.get(off_h,0)+grade_value.get(off_a,0)+0.5*(grade_value.get(def_h,0)+grade_value.get(def_a,0))
        weather=self.weather(g["event_id"])
        if weather and not weather["dome"]: total-=min(3.0,max(0.0,((weather["wind"] or 0)-12)*0.18)+(weather["precipitation"] or 0)*0.35)
        assignment=self.referee_assignment(g["week"],g["home"],g["away"])
        if assignment and assignment["over_pct"] is not None and (assignment["games"] or 0)>=10:
            total+=max(-1.5,min(1.5,(assignment["over_pct"]-50.0)/50.0*3.0))
        total=max(30.0,min(58.0,round_half(total)))
        home_score=max(10,int(round((total+margin)/2))); away_score=max(10,int(round((total-margin)/2)))
        projected_spread=-margin
        edge=None if g["dk_spread"] is None else round_half(g["dk_spread"]-projected_spread)
        return dict(home_score=home_score,away_score=away_score,total=total,home_spread=projected_spread,edge=edge,factors=factors)

    def venue_coords(self, home, away):
        neutral = NEUTRAL_SITES.get((home, away))
        if neutral: return (neutral[0], neutral[1])
        stadium = STADIUMS.get(home)
        return (stadium[0], stadium[1]) if stadium else None

    def situational_factor(self, home, away, kickoff, home_rest, away_rest):
        """+/-1 -- short week and long-distance travel, the two situational spots
        that are actually measurable from data already on hand (days_rest and the
        STADIUMS coordinates). Trap games / letdown spots are real but need a human
        read on schedule context, so they're deliberately left out rather than
        guessed at here."""
        if (home,away) in NEUTRAL_SITES: return 0.0  # both teams travel; no one-sided bonus
        value = 0.0
        if home_rest is not None and away_rest is not None:
            if home_rest <= 4 and away_rest > 4: value -= 0.5
            elif away_rest <= 4 and home_rest > 4: value += 0.5
        coords = self.venue_coords(home, away)
        away_stadium = STADIUMS.get(away)
        if coords and away_stadium:
            if haversine_miles(away_stadium[0], away_stadium[1], coords[0], coords[1]) > 1500: value += 0.5
        return max(-1.0, min(1.0, value))

    def line_movement_factor(self, event_id, dk_spread):
        """+/-1 -- sharp money shows up as the line moving, and line_history already
        records every check. Positive means the number has moved toward the home
        team (i.e. money is coming in on home) since it was first tracked."""
        opening = self.opening_line(event_id)
        if not opening or dk_spread is None or opening["home_spread"] is None: return 0.0
        return max(-1.0, min(1.0, (opening["home_spread"] - dk_spread) / 3))

    def form_factor(self, team, before_kickoff):
        """Recent form for one team, roughly -1..+1: each of the last 3 completed
        games contributes a signed, opponent-quality-adjusted nudge (a win over a
        good team counts for more than a win over a bad one, and vice versa for a
        loss). Not a full model -- just enough signal to separate a team that's
        trending than one that's cooling off."""
        with con() as c:
            games=c.execute("SELECT home,away,home_score,away_score FROM games WHERE (home=? OR away=?) AND kickoff<? AND game_status='FINAL' AND home_score IS NOT NULL AND away_score IS NOT NULL ORDER BY kickoff DESC LIMIT 3",(team,team,before_kickoff)).fetchall()
        if not games: return 0.0
        total=0.0
        for g in games:
            is_home=g["home"]==team
            my_score,opp_score=(g["home_score"],g["away_score"]) if is_home else (g["away_score"],g["home_score"])
            opponent=g["away"] if is_home else g["home"]
            opp_row=self.team(opponent)
            opp_rating=opp_row["consensus_rating"] if opp_row and opp_row["consensus_rating"] is not None else 5.0
            margin=my_score-opp_score
            direction=1 if margin>0 else (-1 if margin<0 else 0)
            per_game=direction*min(1.0,abs(margin)/17) + 0.15*((opp_rating-5.0)/5.0)
            total+=per_game
        return max(-1.0,min(1.0,total/len(games)))

    def confidence_score(self, g, home_team, away_team, home_rest, away_rest):
        """Automatic 1–10 directional lean index; it is not a win probability."""
        breakdown=[]
        power=0.0
        if home_team["consensus_rating"] is not None and away_team["consensus_rating"] is not None and g["dk_spread"] is not None:
            projection=self.projected_game(g,home_team,away_team,home_rest,away_rest)
            edge=projection["edge"] or 0.0
            power=max(-2.0,min(2.0,edge*(2/3)))
        breakdown.append(("Projected spread vs market",power,True))
        # These are already folded into projected_game()'s margin, which is what
        # "Projected spread vs market" above is built from -- listed again here
        # only so the popup shows what's driving that number, not as additional
        # points on top of it (that would double-count each one into the total).
        for label,value in self.automated_factors(g,home_rest,away_rest):
            breakdown.append((label,max(-1.0,min(1.0,value)),False))
        breakdown.append(("Line movement",self.line_movement_factor(g["event_id"],g["dk_spread"]),True))
        form=self.form_factor(g["home"],g["kickoff"])-self.form_factor(g["away"],g["kickoff"])
        breakdown.append(("Recent form",max(-1.0,min(1.0,form/2)),True))
        total=round_half(max(1.0,min(10.0,5.0+sum(v for _,v,counted in breakdown if counted))))
        return total,breakdown

    def confidence_label(self, score, home, away):
        diff=score-5.0
        team=(home if diff>0 else away).split()[-1].upper()
        if abs(diff)<1: return "PASS — no real edge",team
        strength="LARGE LEAN" if abs(diff)>=3 else "LEAN"
        return f"{strength} {team}",team

    def sync_auto_pick(self, g, score):
        """Lock in the algorithm's own side + spread the first time this game shows
        a real lean (|score-5| >= 1, same PASS threshold as confidence_label), so
        its tracked record grades against the number it actually leaned on --
        not whatever the market has drifted to by kickoff."""
        if g["auto_pick_side"] is not None or g["dk_spread"] is None: return
        diff=score-5.0
        if abs(diff)<1: return
        side="home" if diff>0 else "away"
        with con() as c:
            c.execute("UPDATE games SET auto_pick_side=?,auto_pick_spread=?,auto_pick_recorded=? WHERE event_id=? AND auto_pick_side IS NULL",(side,g["dk_spread"],datetime.now(timezone.utc).isoformat(),g["event_id"]))

    def auto_pick_note(self, g, home_rest, away_rest):
        """Plain-English scouting gloss for the star pick -- unit-grade matchups,
        injuries, crowd noise, and rest, in football terms rather than points or
        percentages. Deliberately leaves out the edge/spread math already shown
        elsewhere on the card; this is the "why", not the "how much"."""
        home,away=g["home"],g["away"]
        home_short=home.split()[-1].upper(); away_short=away.split()[-1].upper()
        parts=[]
        if (home,away) in NEUTRAL_SITES:
            parts.append("This is a neutral-site game, so the usual home-field/travel read doesn't really apply -- treat it as more of an unknown than a normal home game.")
        order={"A":5,"B":4,"C":3,"D":2,"F":1}
        off_h,def_h,_=self.team_grade_values(home); off_a,def_a,_=self.team_grade_values(away)
        def lopsided(off_grade,def_grade):
            if off_grade not in order or def_grade not in order: return None
            return order[off_grade]-order[def_grade]
        gap=lopsided(off_a,def_h)
        if gap is not None and gap>=2: parts.append(f"{away_short}'s offense ({off_a} grade) looks well ahead of {home_short}'s defense ({def_h}) -- expect them to move the ball.")
        elif gap is not None and gap<=-2: parts.append(f"{home_short}'s defense ({def_h} grade) looks well ahead of {away_short}'s offense ({off_a}) -- expect it to give them trouble.")
        gap=lopsided(off_h,def_a)
        if gap is not None and gap>=2: parts.append(f"{home_short}'s offense ({off_h} grade) looks well ahead of {away_short}'s defense ({def_a}) -- expect them to move the ball.")
        elif gap is not None and gap<=-2: parts.append(f"{away_short}'s defense ({def_a} grade) looks well ahead of {home_short}'s offense ({off_h}) -- expect it to give them trouble.")
        with con() as c:
            qb_rows={team:c.execute("SELECT player,status FROM injuries WHERE team=? AND position='QB' AND status IN ('Out','Doubtful','Questionable')",(team,)).fetchone() for team in (home,away)}
        for team,label in ((home,home_short),(away,away_short)):
            qb=qb_rows[team]
            if qb: parts.append(f"{label}'s starting QB situation is in question -- {qb['player']} is listed {qb['status'].lower()}.")
        _,out=self.injury_lines(home)
        if out: parts.append(f"{home_short} are banged up: {out}.")
        _,out=self.injury_lines(away)
        if out: parts.append(f"{away_short} are banged up: {out}.")
        if home in NOISE_ELITE: parts.append(f"{home_short} play in one of the league's loudest buildings -- that tends to disrupt a visiting offense's snap count.")
        elif home in NOISE_LOUD: parts.append(f"{home_short}'s home crowd is a real factor for a visiting offense.")
        if home_rest is not None and away_rest is not None:
            if home_rest>=13 and away_rest<13: parts.append(f"{home_short} are coming off extra rest.")
            elif away_rest>=13 and home_rest<13: parts.append(f"{away_short} are coming off extra rest.")
            elif home_rest<=4 and away_rest>4: parts.append(f"{home_short} are on a short week.")
            elif away_rest<=4 and home_rest>4: parts.append(f"{away_short} are on a short week.")
        return " ".join(parts) if parts else "Nothing stands out on paper -- the two teams grade out close and both look healthy. This lean is mostly the market number looking off, not a clear matchup edge."

    def auto_pick_result(self, g):
        if not g["auto_pick_side"] or g["auto_pick_spread"] is None or g["game_status"] != "FINAL" or g["home_score"] is None or g["away_score"] is None: return None
        result=g["home_score"]-g["away_score"]+g["auto_pick_spread"]
        if result==0: return "PUSH"
        winner="home" if result>0 else "away"
        return "WON" if winner==g["auto_pick_side"] else "LOST"

    def injury_lines(self, team):
        """Readable designations for the matchup board itself. ESPN's news blurb
        (in `comment`) almost always names the body part right after the player,
        e.g. "Love (ankle) ..." -- pulling that out lets the card show what's
        actually wrong, not just who's banged up. Sorted by position so the names
        that actually matter (QB/RB/WR/TE, then defensive skill positions) show
        first -- if a team's list is long enough to truncate, it's the O-line and
        specialists that get folded into "+N", not the playmakers."""
        POSITION_PRIORITY={"QB":0,"RB":1,"WR":1,"TE":1,"EDGE":2,"DE":2,"DT":2,"LB":2,"CB":2,"S":2,"FS":2,"SS":2,"DB":2,"K":4,"P":4,"LS":5}
        with con() as c:
            rows=c.execute("SELECT player,position,status,comment FROM injuries WHERE team=? ORDER BY player",(team,)).fetchall()
        rows=sorted(rows,key=lambda r:POSITION_PRIORITY.get((r["position"] or "").upper(),3))
        def entry(r):
            tag=re.search(r"\(([^)]{2,30})\)",r["comment"] or "")
            return f'{r["player"]} ({tag.group(1)})' if tag else r["player"]
        questionable=[entry(r) for r in rows if (r["status"] or "").lower()=="questionable"]
        out=[entry(r) for r in rows if (r["status"] or "").lower() in ("out","injured reserve")]
        def names(items,limit=10): return ", ".join(items[:limit])+(f" +{len(items)-limit}" if len(items)>limit else "")
        return names(questionable),names(out)

    def refresh_injuries(self):
        self.status.set("Refreshing ESPN injury report…"); threading.Thread(target=self._injury_worker,daemon=True).start()

    def _injury_worker(self):
        try:
            request=Request(ESPN_INJURIES,headers={"User-Agent":"Mozilla/5.0 (personal NFL board)"})
            with urlopen(request,timeout=30) as response: source=response.read().decode("utf-8","ignore")
            parser=InjuryParser(); parser.feed(source); timestamp=datetime.now().isoformat(timespec="minutes")
            with con() as c:
                for team,player,position,return_date,status,*rest in parser.rows:
                    comment=rest[0] if rest else ""
                    c.execute("INSERT INTO injuries(team,player,position,return_date,status,comment,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(team,player) DO UPDATE SET position=excluded.position,return_date=excluded.return_date,status=excluded.status,comment=excluded.comment,updated_at=excluded.updated_at",(team,player,position,return_date,status,comment,timestamp))
            self._ui_queue.put(lambda:(self.queue_render(),self.status.set(f"ESPN injuries updated: {len(parser.rows)} player records.")))
        except Exception as ex:self._ui_queue.put(lambda ex=ex:self.status.set(f"ESPN injury refresh failed: {ex}"))

    def auto_update(self):
        """Hourly background poll, also triggered on demand by REFRESH NOW -- cancels
        any pending scheduled run first so clicking the button doesn't stack a second,
        overlapping hourly loop on top of the original one."""
        if self._auto_update_job is not None:
            self.after_cancel(self._auto_update_job); self._auto_update_job=None
        self.refresh_schedule()
        self.refresh_injuries()
        self.refresh_public_lines()
        self.refresh_power_rankings()
        self.refresh_weather()
        self.refresh_unit_grades()
        self.refresh_referees()
        self._auto_update_job=self.after(AUTO_UPDATE_MS, self.auto_update)

    def refresh_power_rankings(self):
        threading.Thread(target=self._power_rankings_worker,args=(self.week.get(),),daemon=True).start()

    def _power_rankings_worker(self,season_week):
        """Pull every RANKING_SOURCES list, store each one that yields a clean 1-32
        ranking (stamping ranking_sources so staleness can be judged), then rebuild
        the consensus from whichever sources are still fresh -- see update_consensus."""
        try:
            fetchers={"cbs_rank":self._ranks_cbs,"sharp_rank":self._ranks_sharp,"nfl_rank":self._ranks_nfl,
                      "pfm_rank":self._ranks_pfm,"tr_rank":self._ranks_teamrankings,"sagarin_rank":self._ranks_sagarin}
            per_source=[]
            for column,label in RANKING_SOURCES:
                try: ranks,detail=fetchers[column]()
                except Exception: ranks,detail={},""
                ok=len(ranks)==32 and set(ranks.values())==set(range(1,33))
                per_source.append(f"{label} {'ok' if ok else 'failed'}")
                if not ok: continue
                with con() as c:
                    for team,rank in ranks.items(): c.execute(f"UPDATE teams SET {column}=? WHERE name=?",(rank,team))
                    c.execute("INSERT INTO ranking_sources(column_name,label,detail,updated_at) VALUES(?,?,?,?) ON CONFLICT(column_name) DO UPDATE SET label=excluded.label,detail=excluded.detail,updated_at=excluded.updated_at",(column,label,detail,datetime.now().isoformat(timespec="minutes")))
            with con() as c:
                used=update_consensus(c)
                stamp=datetime.now().isoformat(timespec="minutes")
                for row in c.execute("SELECT name,consensus_rating FROM teams WHERE consensus_rating IS NOT NULL").fetchall():
                    c.execute("INSERT INTO ranking_history(season_week,team,rating,recorded_at) VALUES(?,?,?,?) ON CONFLICT(season_week,team) DO UPDATE SET rating=excluded.rating,recorded_at=excluded.recorded_at",(season_week,row["name"],row["consensus_rating"],stamp))
            self._ui_queue.put(lambda:(self.queue_render(),self.status.set(f"Season consensus updated from {len(used)} live source(s) ({', '.join(per_source)}).")))
        except Exception as ex:self._ui_queue.put(lambda ex=ex:self.status.set(f"Power-ranking update failed: {ex}"))

    def _fetch_page(self, url):
        request=Request(url,headers={"User-Agent":"Mozilla/5.0 (personal NFL board)"})
        with urlopen(request,timeout=30) as response: return response.read().decode("utf-8","ignore")

    def _page_text(self, raw):
        return re.sub(r"\s+"," ",html.unescape(re.sub(r"<[^>]+>"," ",raw))).strip()

    def _resolve_team(self, label):
        """Map however a ranking site labels a team ("LA Rams", "Tampa Bay", Sagarin's
        "Washington Redskins") to the board's full team name, or None."""
        label=re.sub(r"\(.*?\)","",self._page_text(label)).strip()
        if label in STADIUMS: return label
        for nickname,name in TEAM_NICKNAMES.items():
            if re.search(rf"\b{re.escape(nickname)}\b",label,re.I): return name
        for city,name in TEAM_CITIES.items():
            if label==city or label.startswith(city+" "): return name
        return None

    def _ranks_in_order(self, pairs):
        """[(rank, label)] in page order -> {team: rank}; first sighting of a team or rank wins."""
        ranks={}; used=set()
        for rank,label in pairs:
            team=self._resolve_team(label)
            if team and team not in ranks and 1<=rank<=32 and rank not in used: ranks[team]=rank; used.add(rank)
        return ranks

    def _ranks_by_rating(self, pairs):
        """[(label, rating)] -> {team: rank}, highest rating first; first sighting of a team wins."""
        ratings={}
        for label,rating in pairs:
            team=self._resolve_team(label)
            if team and team not in ratings: ratings[team]=rating
        return {team:index for index,(team,_rating) in enumerate(sorted(ratings.items(),key=lambda item:-item[1]),1)}

    # Each _ranks_* source returns ({team: rank}, detail note shown in the rankings view).
    def _ranks_cbs(self):
        """Pete Prisco's weekly list -- CBS's power-rankings hub always shows the latest one."""
        raw=self._fetch_page(CBS_RANKINGS)
        name_pattern=re.compile(r'class="(?:[^"]*\s)?team-name(?:\s[^"]*)?"[^>]*>(.*?)</(?:div|span|td|h\d)>',re.S)
        pairs=[]
        for match in re.finditer(r'class="(?:[^"]*\s)?rank(?:\s[^"]*)?"[^>]*>(.*?)</',raw,re.S):
            rank=re.search(r"\d{1,2}",self._page_text(match.group(1))); name=name_pattern.search(raw,match.end())
            if rank and name: pairs.append((int(rank.group()),name.group(1)))
        title=re.search(r"<title[^>]*>(.*?)</title>",raw,re.S)
        return self._ranks_in_order(pairs),(self._page_text(title.group(1))[:80] if title else "")

    def _ranks_sharp(self):
        text=self._page_text(self._fetch_page(SHARP_RANKINGS))
        updated=re.search(r"Updated:\s*([A-Z][a-z]+ \d{1,2})",text)
        return self._extract_ranks(text),(f"updated {updated.group(1)}" if updated else "")

    def _ranks_nfl(self):
        """NFL.com posts each week's list as a new article (new URL); its power-rankings
        hub links the newest first. Entries read "Rank 3 — No Rank change Seattle
        Seahawks" or "Rank 4 Caret Up Rank increased by 1 Buffalo Bills"."""
        hub=self._fetch_page(NFL_RANKINGS_HUB)
        url=re.search(r'href="(https://www\.nfl\.com/news/nfl-power-rankings-[^"#?]+)"',hub).group(1)
        text=self._page_text(self._fetch_page(url))
        names="|".join(re.escape(name) for name,_abbr in TEAMS)
        pairs=[(int(match.group(1)),match.group(2)) for match in re.finditer(rf"\bRank\s+(\d{{1,2}})\b[^.\"\\]{{0,45}}?\b({names})\b",text)]
        return self._ranks_in_order(pairs),url.rsplit("/",1)[-1]

    def _ranks_pfm(self):
        raw=self._fetch_page(PFM_RANKINGS)
        updated=re.search(r"ratings updated ([A-Z][a-z]+ \d{1,2}, \d{4})",self._page_text(raw))
        pairs=[(int(rank),team) for rank,team in re.findall(r'pfm-power-rank[^"]*">\s*(\d{1,2})\s*</span>.*?data-sort-value="([^"]+)"',raw,re.S)]
        return self._ranks_in_order(pairs),(f"updated {updated.group(1)}" if updated else "")

    def _ranks_teamrankings(self):
        """TeamRankings' predictive rating: results-based and schedule-adjusted, with a
        stats-regression preseason prior (no betting-market input)."""
        raw=self._fetch_page(TEAMRANKINGS_RATINGS); pairs=[]
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>",raw,re.S):
            cells=[self._page_text(cell) for cell in re.findall(r"<td[^>]*>(.*?)</td>",row,re.S)]
            if len(cells)>=3 and cells[0].isdigit():
                try: pairs.append((cells[1],float(cells[2])))
                except ValueError: pass
        return self._ranks_by_rating(pairs),""

    def _ranks_sagarin(self):
        """Jeff Sagarin's results-based computer ratings, published as a plain-text page."""
        text=html.unescape(re.sub(r"<[^>]+>","",self._fetch_page(SAGARIN_RATINGS)))
        pairs=[(match.group(2),float(match.group(3))) for match in (re.match(r"\s*(\d{1,2})\s+([A-Za-z0-9 .']+?)\s+=\s*(-?\d+\.\d+)",line) for line in text.splitlines()) if match]
        through=re.search(r"Ratings through (.*)",text)
        return self._ranks_by_rating(pairs),(through.group(1).strip()[:60] if through else "")

    def _extract_ranks(self, text):
        # Collect every (position-in-article, rank, team) match instead of
        # picking a winner as we go. An earlier version looped rank-first, then
        # teams in alphabetical order, and took whichever team matched
        # first for that rank number -- so an alphabetically-early team
        # (e.g. "Arizona Cardinals") with a coincidental nearby number could
        # silently steal a rank that belonged to the real team further
        # down the list, with no protection against a team's rank getting
        # overwritten again by a later, spurious match.
        #
        # This also runs 32x fewer regex searches than testing every one of
        # 32 possible ranks separately per team: each pattern now captures
        # whichever 1-2 digit number is actually attached to the team name
        # in one search, instead of asking "is it specifically rank 1? rank
        # 2? ... rank 32?" one at a time. Tight windows between the number
        # and the team name (a real ranked-list entry reads "1. Team", "#1
        # Team", "1 | Team" with nothing in between) are what keeps false
        # positives out, not the number of searches.
        candidates=[]
        for team,_abbr in TEAMS:
            team_pattern=re.escape(team)
            patterns=(rf"#\s*(\d{{1,2}})\b[\s:.\-–—]{{0,6}}{team_pattern}",rf"\bRank\s*(\d{{1,2}})\b[\s:.\-–—]{{0,6}}{team_pattern}",rf"\b(\d{{1,2}})\.\s{{0,3}}{team_pattern}",rf"\b(\d{{1,2}})\s*\|\s*{team_pattern}")
            for pattern in patterns:
                match=re.search(pattern,text,re.I)
                if match:
                    rank=int(match.group(1))
                    if 1<=rank<=32: candidates.append((match.start(),rank,team)); break
        candidates.sort(key=lambda item:item[0])
        ranks={}; used_ranks=set()
        for _position,rank,team in candidates:
            if team in ranks or rank in used_ranks: continue
            ranks[team]=rank; used_ranks.add(rank)
        return ranks

    def refresh_referees(self):
        self.status.set("Refreshing referee assignments/trends…"); threading.Thread(target=self._referee_worker,args=(self.week.get(),),daemon=True).start()

    def _load_nflverse_games(self):
        """Cached locally for REFEREE_GAMES_CACHE_HOURS -- this is nflverse's whole
        game-by-game history (7000+ rows back to 1999), so there's no reason to
        re-pull it on every 15-minute auto_update tick."""
        if REFEREE_GAMES_CACHE.exists():
            age_hours=(datetime.now().timestamp()-REFEREE_GAMES_CACHE.stat().st_mtime)/3600
            if age_hours<REFEREE_GAMES_CACHE_HOURS:
                return list(csv.DictReader(io.StringIO(REFEREE_GAMES_CACHE.read_text(encoding="utf-8"))))
        request=Request(NFLVERSE_GAMES,headers={"User-Agent":"Mozilla/5.0 (personal NFL board)"})
        with urlopen(request,timeout=30) as response: text=response.read().decode("utf-8","ignore")
        REFEREE_GAMES_CACHE.write_text(text,encoding="utf-8")
        return list(csv.DictReader(io.StringIO(text)))

    def _compute_referee_stats(self, rows):
        """Career Home ATS% / Over% per referee, straight from real closing lines and
        final scores -- pushes excluded from ATS%, same convention as everywhere else
        in this file that grades against a spread."""
        agg={}
        for row in rows:
            referee=(row.get("referee") or "").strip()
            if not referee or row["game_type"]!="REG" or not row["home_score"] or not row["away_score"]: continue
            d=agg.setdefault(referee,dict(games=0,home_covers=0,ats_games=0,overs=0,ou_games=0))
            if row["spread_line"]:
                result=int(row["home_score"])-int(row["away_score"])-float(row["spread_line"])
                if result!=0:
                    d["ats_games"]+=1
                    if result>0: d["home_covers"]+=1
            if row["total_line"] and row["total"]:
                total_result=float(row["total"])-float(row["total_line"])
                if total_result!=0:
                    d["ou_games"]+=1
                    if total_result>0: d["overs"]+=1
            d["games"]+=1
        stats={}
        for referee,d in agg.items():
            if d["ats_games"]<10: continue
            stats[referee]=dict(games=d["ats_games"],
                home_ats_pct=round(d["home_covers"]/d["ats_games"]*100,1),
                over_pct=round(d["overs"]/d["ou_games"]*100,1) if d["ou_games"] else None)
        return stats

    def _referee_worker(self, season_week):
        """Rotowire publishes this week's crew-per-game assignments (a new article
        each week, same rough table shape) -- nflverse has no way to know those
        ahead of kickoff, so that part still has to be scraped. Per-crew career
        trends, though, come straight from nflverse's own game-by-game history
        (see _compute_referee_stats), which can't drift or redesign itself out
        from under this."""
        assignments={}
        try:
            request=Request(ROTOWIRE_REFS,headers={"User-Agent":"Mozilla/5.0 (personal NFL board)"})
            with urlopen(request,timeout=30) as response: raw=response.read().decode("utf-8","ignore")
            match=re.search(r"<th><strong>Matchup</strong></th><th><strong>Referee</strong></th></tr></thead><tbody>(.*?)</tbody>",raw,re.S)
            if match:
                for away_nick,home_nick,referee in re.findall(r'<td[^>]*>([^<@]+?)\s*@\s*([^<]+?)</td><td[^>]*>([^<]+)</td>',match.group(1)):
                    home=TEAM_NICKNAMES.get(home_nick.strip()); away=TEAM_NICKNAMES.get(away_nick.strip())
                    if home and away: assignments[(home,away)]=html.unescape(referee.strip())
        except Exception as ex:
            self._ui_queue.put(lambda ex=ex:self.status.set(f"Rotowire referee assignments failed: {ex}"))
        stats={}
        try:
            stats=self._compute_referee_stats(self._load_nflverse_games())
        except Exception as ex:
            self._ui_queue.put(lambda ex=ex:self.status.set(f"nflverse referee history failed: {ex}"))
        if not assignments and not stats: return
        timestamp=datetime.now().isoformat(timespec="minutes")
        with con() as c:
            for referee,row in stats.items():
                c.execute("INSERT INTO referee_stats(referee,games,home_ats_pct,over_pct,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(referee) DO UPDATE SET games=excluded.games,home_ats_pct=excluded.home_ats_pct,over_pct=excluded.over_pct,updated_at=excluded.updated_at",(referee,row["games"],row["home_ats_pct"],row["over_pct"],timestamp))
            for (home,away),referee in assignments.items():
                c.execute("INSERT INTO referee_assignments(season_week,home,away,referee,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(season_week,home,away) DO UPDATE SET referee=excluded.referee,updated_at=excluded.updated_at",(season_week,home,away,referee,timestamp))
        self._ui_queue.put(lambda:(self.queue_render(),self.status.set(f"Referee data updated: {len(assignments)} Week {season_week} assignments, {len(stats)} crew trend rows.")))

    def referee_assignment(self, season_week, home, away):
        with con() as c:
            row=c.execute("SELECT referee FROM referee_assignments WHERE season_week=? AND home=? AND away=?",(season_week,home,away)).fetchone()
        if not row: return None
        with con() as c:
            stats=c.execute("SELECT games,home_ats_pct,over_pct FROM referee_stats WHERE referee=?",(row["referee"],)).fetchone()
        return dict(referee=row["referee"],games=stats["games"] if stats else None,home_ats_pct=stats["home_ats_pct"] if stats else None,over_pct=stats["over_pct"] if stats else None)

    def referee_factor(self, season_week, home, away):
        """+/-0.4 -- a crew's career Home ATS% lean, folded in at a fraction of its
        face value since a few points of career cover rate is a soft signal next to
        injuries/rest/travel. Requires a real sample (10+ games); untested new
        referees or a week with no scraped assignment contribute nothing."""
        assignment=self.referee_assignment(season_week,home,away)
        if not assignment or assignment["home_ats_pct"] is None or (assignment["games"] or 0)<10: return 0.0
        return round(max(-0.4,min(0.4,(assignment["home_ats_pct"]-50.0)/50.0*0.8)),2)

    def show_injuries(self, home, away, week=None):
        C=self.C
        window=tk.Toplevel(self);window.title(f"Game Intel — {away} at {home}");window.geometry("820x700");window.configure(bg=C["bg"])
        tk.Label(window,text=f"GAME INTEL  •  {away.upper()} AT {home.upper()}",bg=C["header_bg"],fg=C["header_fg"],font=("Segoe UI",12,"bold"),padx=16,pady=12).pack(fill="x")
        assignment=self.referee_assignment(week,home,away) if week is not None else None
        if assignment:
            over_text=f"{assignment['over_pct']:.1f}%" if assignment["over_pct"] is not None else "n/a"
            games_text=f"{assignment['games']} games" if assignment["games"] is not None else "no career sample yet"
            tk.Label(window,text=f"ON THE CALL: {assignment['referee'].upper()}  ·  career Home ATS {assignment['home_ats_pct']:.1f}% ({games_text})  ·  Over {over_text}" if assignment["home_ats_pct"] is not None else f"ON THE CALL: {assignment['referee'].upper()}  ·  {games_text}",bg=C["bg"],fg=C["info_fg"],font=("Segoe UI",10,"bold"),padx=18,anchor="w",justify="left",wraplength=784).pack(fill="x",pady=(10,0))
        text=tk.Text(window,wrap="word",font=("Segoe UI",10),bg=C["bg"],fg=C["total_fg"],relief="flat",padx=18,pady=14);text.pack(fill="both",expand=True)

        # Algorithm notes -- the same automatic Game Score inputs/projection shown in
        # the separate "Automatic Game Score" popup, folded in here too so everything
        # about a matchup (injuries, referee, and the algorithm's own reasoning) lives
        # in one place instead of two dialogs the user has to open separately.
        with con() as c: g=c.execute("SELECT * FROM games WHERE home=? AND away=? AND week=?",(home,away,week)).fetchone() if week is not None else None
        if g:
            home_team,away_team=self.team(home),self.team(away)
            home_rest,away_rest=self.days_rest(home,g["kickoff"]),self.days_rest(away,g["kickoff"])
            score,breakdown=self.confidence_score(g,home_team,away_team,home_rest,away_rest)
            projection=self.projected_game(g,home_team,away_team,home_rest,away_rest)
            label,_=self.confidence_label(score,home,away)
            text.insert("end","ALGORITHM NOTES\n",("h1",))
            text.insert("end",f"Projected score:  {away.split()[-1].upper()} {projection['away_score']}  ·  {home.split()[-1].upper()} {projection['home_score']}\n")
            text.insert("end",f"Projected line:  {home.split()[-1].upper()} {projection['home_spread']:+g}   ·   Projected total:  {projection['total']:g}\n")
            if projection["edge"] is not None:
                text.insert("end",f"Edge vs market spread ({g['dk_spread']:+g}):  {projection['edge']:+g} pts\n")
            text.insert("end",f"Consensus rating:  {away.split()[-1].upper()} {away_team['consensus_rating']}  ·  {home.split()[-1].upper()} {home_team['consensus_rating']}\n" if home_team["consensus_rating"] is not None and away_team["consensus_rating"] is not None else "Consensus rating:  not set for one or both teams\n")
            text.insert("end",f"Days rest:  {away.split()[-1].upper()} {away_rest if away_rest is not None else '—'}  ·  {home.split()[-1].upper()} {home_rest if home_rest is not None else '—'}\n\n")
            text.insert("end","Automatic inputs (positive favors home, negative favors away):\n",("h2",))
            for factor_label,value,counted in breakdown:
                tag="counted" if counted else "folded"
                note="" if counted else "  (folded into projected spread above, shown for reference only)"
                text.insert("end",f"  • {factor_label}: {value:+.2f}{note}\n",(tag,))
            text.insert("end",f"\nLEAN INDEX:  {score:g} / 10  —  {label}\n",("h2",))
            text.insert("end","Directional only -- not a win probability or a betting recommendation. Ratings are a 1-10 consensus ranking scale; the projected spread/total are transparent estimates built from the inputs above, not a market replacement.\n\n")
        else:
            text.insert("end","ALGORITHM NOTES\n",("h1",))
            text.insert("end","No game/week context available for a projection.\n\n")

        text.insert("end","INJURY REPORT (ESPN)\n",("h1",))
        with con() as c:
            for club in (away,home):
                text.insert("end",club.upper()+"\n",("h2",))
                rows=c.execute("SELECT player,position,status,return_date,comment FROM injuries WHERE team=? ORDER BY CASE status WHEN 'Out' THEN 0 WHEN 'Questionable' THEN 1 ELSE 2 END, player",(club,)).fetchall()
                if not rows:text.insert("end","No saved ESPN entries yet. The ESPN report loads automatically when the board opens.\n\n");continue
                for row in rows:
                    estimated=f" · return: {row['return_date']}" if row["return_date"] else ""
                    text.insert("end",f"• {row['player']} ({row['position']}) — {row['status']}{estimated}\n")
                text.insert("end","\n")
        text.tag_configure("h1",font=("Segoe UI",11,"bold"),foreground=C["accent"],spacing3=4)
        text.tag_configure("h2",font=("Segoe UI",10,"bold"),foreground=C["team_fg"],spacing1=4)
        text.tag_configure("counted",foreground=C["info_fg"])
        text.tag_configure("folded",foreground=C["meta_fg"])
        text.configure(state="disabled")

    def venue_label(self, home, away):
        return (NEUTRAL_SITES.get((home,away)) or (None,None,None,VENUES.get(home,"stadium location")))[3]

    def days_rest(self, team, kickoff):
        """Days since this team's previous game -- informational only, no wager math attached."""
        try:
            current=datetime.fromisoformat(kickoff.replace("Z","+00:00"))
            with con() as c:
                previous=c.execute("SELECT kickoff FROM games WHERE (home=? OR away=?) AND kickoff<? ORDER BY kickoff DESC LIMIT 1",(team,team,kickoff)).fetchone()
            if not previous: return None
            return max(0,(current-datetime.fromisoformat(previous["kickoff"].replace("Z","+00:00"))).days)
        except Exception: return None

    def opening_line(self, event_id):
        """The first spread/total line_history ever recorded for this game -- lets the
        board show how far the number has moved since it was first tracked."""
        with con() as c:
            return c.execute("SELECT home_spread,total FROM line_history WHERE event_id=? ORDER BY checked_at ASC LIMIT 1",(event_id,)).fetchone()

    def weather(self, event_id):
        with con() as c:
            return c.execute("SELECT * FROM weather WHERE event_id=?",(event_id,)).fetchone()

    def open_weather(self, place):
        """Open the detailed hourly forecast at this game's actual venue."""
        webbrowser.open("https://www.google.com/search?q="+quote_plus(place+" hourly weather forecast"))

    def logo(self,abbr):
        """A faded team-logo watermark, ~14% opacity, for the free space at the
        bottom of each matchup card's team panel. Needs Pillow (see PIL_AVAILABLE
        at the top of the file) to actually fade the image -- without it, this
        just never populates logo_images and the card renders exactly as before."""
        if not PIL_AVAILABLE: return None
        if abbr in self.logo_images:return self.logo_images[abbr]
        if abbr not in self.fetching:
            self.fetching.add(abbr); threading.Thread(target=self._download_logo,args=(abbr,),daemon=True).start()
        return None

    def _download_logo(self,abbr):
        try:
            raw=urlopen(Request(f"https://a.espncdn.com/i/teamlogos/nfl/500/{abbr}.png",headers={"User-Agent":"Mozilla/5.0"}),timeout=12).read()
            image=_PILImage.open(io.BytesIO(raw)).convert("RGBA").resize((110,110),_PILImage.LANCZOS)
            image.putalpha(image.getchannel("A").point(lambda a:int(a*0.14)))
            self._ui_queue.put(lambda:self._store_logo(abbr,image))
        except Exception: self.fetching.discard(abbr)

    def _store_logo(self,abbr,pil_image):
        try:self.logo_images[abbr]=_PILImageTk.PhotoImage(pil_image)
        except Exception:pass
        self.fetching.discard(abbr)
        # All 32 team logos finish downloading in a tight burst on first launch.
        # queue_render() coalesces that burst into one re-render 180ms after the
        # last arrival, instead of a full widget-tree rebuild per logo -- calling
        # render() directly here (like the old, never-exercised dead code did) is
        # exactly what froze the app: up to 32 full rebuilds back-to-back inside
        # one _pump_ui_queue tick, long enough for Windows to flag it as hung.
        self.queue_render()

    def club(self,parent,t,side,venue=None,selected=False,starred=False,rest_days=None,event_id=None,pick_result=None,away=None,week=None,home_name=None):
        C=self.C
        if selected and pick_result=="WON": background=C["card_won"]
        elif selected and pick_result=="LOST": background=C["card_lost"]
        else: background=C["card_selected_star"] if selected and starred else (C["card_selected"] if selected else C["card_bg"])
        f=tk.Frame(parent,bg=background,padx=22,pady=7); f.grid(row=0,column=0 if side=="home" else 2,sticky="nsew")
        if side=="away": f.grid_columnconfigure(0,weight=1)
        # A faded team-logo watermark, anchored to the bottom of the panel so it
        # sits in whatever free space is left below the actual info (this frame
        # gets stretched taller than its own content by the grid row, to match
        # whichever side/mid column is tallest for this game) -- never on top of
        # real text, since `info` below is opaque and packs from the top down.
        mark=self.logo(t["abbr"])
        # Bottom corner OPPOSITE the text's own anchor -- home text hugs the left
        # (anchor="w"), so the bottom-right corner is the spot least likely to ever
        # have a wrapped injury line reach into it, and vice versa for away.
        if mark: tk.Label(f,image=mark,bg=background,bd=0).place(relx=(1.0 if side=="home" else 0.0),rely=1.0,anchor=("se" if side=="home" else "sw"))
        info=tk.Frame(f,bg=background)
        glyph=" ✓" if pick_result=="WON" else (" ✗" if pick_result=="LOST" else "")
        glyph_color=C["good_fg"] if pick_result=="WON" else (C["bad_fg"] if pick_result=="LOST" else C["team_fg"])
        name_row=tk.Frame(info,bg=background);name_row.pack(fill="x")
        tk.Label(name_row,text=t["name"].upper(),bg=background,fg=C["team_fg"],font=("Segoe UI",15,"bold"),anchor="w" if side=="home" else "e").pack(side="left" if side=="home" else "right")
        if glyph: tk.Label(name_row,text=glyph,bg=background,fg=glyph_color,font=("Segoe UI",15,"bold")).pack(side="left" if side=="home" else "right")
        if side=="home":
            neutral=venue and venue.startswith("NEUTRAL SITE")
            loud=" 🔊" if t["name"] in NOISE_ELITE and not neutral else ""
            place=venue or VENUES.get(t["name"],"stadium location")
            tk.Label(info,text=(venue if neutral else "HOME FIELD  ·  "+place)+loud,bg=background,fg=C["meta_fg"],font=("Segoe UI",8),anchor="w").pack(fill="x",pady=(2,0))
            if event_id:
                w=self.weather(event_id)
                if w:
                    if w["dome"]:
                        tk.Label(info,text="DOME / ROOF ASSUMED CLOSED",bg=background,fg=C["meta_fg"],font=("Segoe UI",8),anchor="w").pack(fill="x",pady=(1,0))
                    elif w["temperature"] is not None:
                        icon=weather_icon(w["weather_code"],w["wind"])
                        # Clickable through to the full hourly forecast -- underline +
                        # info-accent color signal that, unlike the plain meta lines
                        # around it, this one responds to a click.
                        link=tk.Label(info,text=self.weather_text(w),wraplength=340,justify="left",bg=background,fg=C["warn_fg"] if w["alert"] else C["info_fg"],font=("Segoe UI",8,"underline"),anchor="w",cursor="hand2")
                        link.pack(fill="x",pady=(1,0))
                        link.bind("<Button-1>",lambda _e,p=place:self.open_weather(p))
            if away is not None and week is not None:
                assignment=self.referee_assignment(week,t["name"],away)
                if assignment:
                    if assignment["home_ats_pct"] is None:
                        ref_text=f"REF  {assignment['referee']}  ·  no career sample yet"
                    else:
                        ref_text=f"REF  {assignment['referee']}  ·  Home ATS {assignment['home_ats_pct']:.1f}%  ·  Over {assignment['over_pct']:.1f}%" if assignment["over_pct"] is not None else f"REF  {assignment['referee']}  ·  Home ATS {assignment['home_ats_pct']:.1f}%"
                    tk.Label(info,text=ref_text,bg=background,fg=C["meta_fg"],font=("Segoe UI",8),anchor="w").pack(fill="x",pady=(1,0))
        else:
            # The home side always carries a venue line (and often a weather line)
            # that the away side has no equivalent for -- without a matching spacer
            # here, RANKING/QUESTIONABLE/OUT-IR end up sitting at different heights
            # between the two columns. Mirror the same line count so they line up.
            tk.Label(info,text="",bg=background,font=("Segoe UI",8)).pack(fill="x",pady=(2,0))
            if event_id and self.weather(event_id):
                tk.Label(info,text="",bg=background,font=("Segoe UI",8)).pack(fill="x",pady=(1,0))
            if home_name is not None and week is not None and self.referee_assignment(week,home_name,t["name"]):
                tk.Label(info,text="",bg=background,font=("Segoe UI",8)).pack(fill="x",pady=(1,0))
        rating=t["consensus_rating"]
        color=self.rating_color(rating)
        my_rank=getattr(self,"_team_rank",{}).get(t["name"])
        rank_count=getattr(self,"_team_rank_count",0)
        if rating is None: ranking_text="RANKING  —"
        elif my_rank is not None: ranking_text=f"RANKING  {rating:.1f}  ·  #{my_rank}/{rank_count}"
        else: ranking_text=f"RANKING  {rating:.1f}"
        rank_row=tk.Frame(info,bg=background)
        rank_row.pack(fill="x",pady=(5,2))
        tk.Label(rank_row,text=ranking_text,bg=background,fg=color,font=("Segoe UI",9,"bold")).pack(side="left" if side=="home" else "right",anchor="n",pady=8)
        self.grade_badges(rank_row,t["name"],side,background)

        questionable, out=self.injury_lines(t["name"])
        anchor="w" if side=="home" else "e"
        if questionable:
            tk.Label(info,text=f"QUESTIONABLE  {questionable}",bg=background,fg=C["warn_fg"],font=("Segoe UI",8,"bold"),anchor=anchor,justify="left" if side=="home" else "right",wraplength=360).pack(fill="x",pady=(3,0))
        if out:
            tk.Label(info,text=f"OUT / IR  {out}",bg=background,fg=C["bad_fg"],font=("Segoe UI",8,"bold"),anchor=anchor,justify="left" if side=="home" else "right",wraplength=360).pack(fill="x",pady=(2,0))
        if rest_days is not None:
            tk.Label(info,text=f"{rest_days} DAYS REST",bg=background,fg=C["info_fg"] if rest_days>=10 else C["meta_fg"],font=("Segoe UI",8,"bold"),anchor=anchor).pack(fill="x",pady=(2,0))
        # External padding leaves the parent watermark visible below the content.
        info.pack(side="top",fill="x",pady=(0,96 if mark else 0))

    def save_pick(self,event_id,side):
        with con() as c:c.execute("UPDATE games SET pick_spread=CASE WHEN pick_side=? AND pick_recorded IS NOT NULL THEN pick_spread ELSE dk_spread END,pick_recorded=CASE WHEN pick_side=? AND pick_recorded IS NOT NULL THEN pick_recorded ELSE ? END,pick_side=?,bet_star=CASE WHEN pick_side=? THEN bet_star ELSE 0 END WHERE event_id=?",(side,side,datetime.now(timezone.utc).isoformat(),side,side,event_id))
        self.render()

    def toggle_bet(self,event_id):
        with con() as c:
            game=c.execute("SELECT pick_side,bet_star FROM games WHERE event_id=?",(event_id,)).fetchone()
            if not game or not game["pick_side"]:
                messagebox.showinfo("Choose a side first","Choose the team you lean toward before starring it as a bet."); return
            c.execute("UPDATE games SET bet_star=? WHERE event_id=?",(0 if game["bet_star"] else 1,event_id))
        self.render()

    def pick_result(self,g):
        if not g["pick_side"] or g["pick_spread"] is None or g["game_status"] != "FINAL" or g["home_score"] is None or g["away_score"] is None: return None
        result=g["home_score"]-g["away_score"]+g["pick_spread"]
        if result==0:return "PUSH"
        winner="home" if result>0 else "away"
        return "WON" if winner==g["pick_side"] else "LOST"

    def ou_result(self,g):
        if not g["ou_pick"] or g["dk_total"] is None or g["home_score"] is None or g["away_score"] is None: return None
        if g["game_status"] != "FINAL": return None
        actual=g["home_score"]+g["away_score"]
        if actual==g["dk_total"]:return "PUSH"
        winner="over" if actual>g["dk_total"] else "under"
        return "WON" if winner==g["ou_pick"] else "LOST"

    def render_rankings(self):
        C=self.C
        with con() as c:
            teams=c.execute(f"SELECT name,consensus_rating,{','.join(column for column,_ in RANKING_SOURCES)} FROM teams WHERE consensus_rating IS NOT NULL ORDER BY consensus_rating DESC,name").fetchall()
            refreshed={r["column_name"]:r["updated_at"] for r in c.execute("SELECT column_name,updated_at FROM ranking_sources").fetchall()}
            previous=c.execute("SELECT MAX(season_week) AS week FROM ranking_history WHERE season_week<?",(self.week.get(),)).fetchone()["week"]
            old={}
            if previous:
                old={r["team"]:r["rating"] for r in c.execute("SELECT team,rating FROM ranking_history WHERE season_week=?",(previous,)).fetchall()}
        old_order={name:index+1 for index,(name,_rating) in enumerate(sorted(old.items(),key=lambda item:(-item[1],item[0])))}
        cutoff=(datetime.now()-timedelta(days=RANKING_STALE_DAYS)).isoformat(timespec="minutes")
        def freshness(column):
            if column not in refreshed: return "never loaded"
            return ("STALE, last " if refreshed[column]<cutoff else "")+datetime.fromisoformat(refreshed[column]).strftime("%b %d %I:%M%p")
        source_state=" · ".join(f"{label} {freshness(column)}" for column,label in RANKING_SOURCES)
        tk.Label(self.rows,text=GRADE_GUIDE+"\n"+self.stats_state+"\nRanking sources refreshed: "+source_state,bg=C["bg"],fg=C["meta_fg"],wraplength=1100,justify="left").pack(fill="x",padx=22)
        columns=(("RANK",50),("TEAM",220),("CONSENSUS",94),("OFFENSE",78),("DEFENSE",78),("MOVE",64),("SOURCES",235))
        team_count=len(teams)
        def row_frame(background,pady):
            frame=tk.Frame(self.rows,bg=background,padx=18,pady=pady)
            frame.pack(fill="x",padx=16,pady=1)
            for col,(_,width) in enumerate(columns):
                frame.grid_columnconfigure(col,minsize=width,weight=1 if col==6 else 0)
            return frame
        heading=row_frame(C["heading_bg"],9)
        for col,(label,_) in enumerate(columns):
            tk.Label(heading,text=label,bg=C["heading_bg"],fg=C["heading_fg"],font=("Segoe UI",9,"bold"),anchor="w" if col in (0,1,6) else "center").grid(row=0,column=col,sticky="ew")
        for index,t in enumerate(teams,1):
            background=C["row_bg"] if index%2 else C["row_alt_bg"]
            row=row_frame(background,8)
            rating=t["consensus_rating"];color=self.rating_color(rating)
            prior=old_order.get(t["name"])
            if prior is None: move="NEW";move_color=C["move_flat"]
            elif prior==index: move="—";move_color=C["move_flat"]
            elif prior>index: move=f"▲ {prior-index}";move_color=C["move_up"]
            else: move=f"▼ {index-prior}";move_color=C["move_down"]
            sources=" · ".join(f"{label} {t[column] if t[column] else '—'}" for column,label in RANKING_SOURCES)
            tk.Label(row,text=f"{index}/{team_count}",bg=background,fg=C["meta_fg"],font=("Segoe UI",10,"bold"),anchor="w").grid(row=0,column=0,sticky="ew")
            tk.Label(row,text=t["name"].upper(),bg=background,fg=C["team_fg"],font=("Segoe UI",11,"bold"),wraplength=215,justify="left",anchor="w").grid(row=0,column=1,sticky="ew")
            tk.Label(row,text=f"{rating:.1f} / 10",bg=background,fg=color,font=("Segoe UI",11,"bold")).grid(row=0,column=2,sticky="ew")
            off,defense,_basis=self.team_grade_values(t["name"])
            for col,grade in ((3,off),(4,defense)):
                fg,bg=self.grade_colors(grade)
                tk.Label(row,text=grade,bg=bg,fg=fg,font=("Segoe UI",18,"bold"),padx=8,pady=2).grid(row=0,column=col,sticky="ew",padx=7)
            tk.Label(row,text=move,bg=background,fg=move_color,font=("Segoe UI",10,"bold")).grid(row=0,column=5,sticky="ew")
            tk.Label(row,text=sources,bg=background,fg=C["sources_fg"],font=("Segoe UI",8),wraplength=235,justify="left",anchor="w").grid(row=0,column=6,sticky="ew")

    def render_picks(self):
        C=self.C
        with con() as c:
            games=c.execute("SELECT * FROM games WHERE pick_side IS NOT NULL OR ou_pick IS NOT NULL OR auto_pick_side IS NOT NULL ORDER BY week,kickoff").fetchall()
        if not games:
            tk.Label(self.rows,text="No picks recorded yet. Tap a team on any matchup card to start tracking your season.",bg=C["bg"],fg=C["empty_fg"],font=("Segoe UI",13),pady=55).pack();return
        def record(results):
            return sum(r=="WON" for r in results),sum(r=="LOST" for r in results),sum(r=="PUSH" for r in results)
        def record_text(results):
            w,l,p=record(results)
            return f"{w}-{l}"+(f"-{p}" if p else "")
        bet_games=[g for g in games if g["pick_side"] and g["bet_star"]]
        pick_games=[g for g in games if g["pick_side"]]
        ou_results=[r for g in games if g["ou_pick"] and (r:=self.ou_result(g))]
        auto_results=[r for g in games if g["auto_pick_side"] and (r:=self.auto_pick_result(g))]

        def heading(container):
            head=tk.Frame(container,bg=C["heading_bg"],padx=22,pady=9);head.pack(fill="x",padx=16,pady=(3,1))
            for text,width,anchor in (("WK",5,"w"),("MATCHUP",30,"w"),("PICK",14,"w"),("LINE",8,"center"),("RESULT",14,"center"),("O/U PICK",16,"center")):
                tk.Label(head,text=text,bg=C["heading_bg"],fg=C["heading_fg"],font=("Segoe UI",9,"bold"),width=width,anchor=anchor).pack(side="left")

        def pick_row(container,g,index):
            background=C["row_bg"] if index%2 else C["row_alt_bg"]
            outer=tk.Frame(container,bg=background);outer.pack(fill="x",padx=16,pady=1)
            row=tk.Frame(outer,bg=background,padx=22,pady=8);row.pack(fill="x")
            matchup=f"{g['away'].split()[-1].upper()} @ {g['home'].split()[-1].upper()}"
            matchup += "\n" + self.grade_line(g["away"],True) + " / " + self.grade_line(g["home"],True)
            pick_team=(g["home"] if g["pick_side"]=="home" else g["away"]) if g["pick_side"] else None
            pick_label=(("★ " if g["bet_star"] else "")+pick_team.split()[-1].upper()) if pick_team else "—"
            line=f"{(g['pick_spread'] if g['pick_side']=='home' else -g['pick_spread']):+g}" if g["pick_spread"] is not None and g["pick_side"] else "N/A"
            result=self.pick_result(g)
            if result: result_text,result_color=(("✓ WON" if result=="WON" else "✗ LOST" if result=="LOST" else result),(C["good_fg"] if result=="WON" else C["bad_fg"] if result=="LOST" else C["meta_fg"]))
            elif g["pick_side"]: result_text,result_color=("LINE UNKNOWN" if g["pick_spread"] is None else "PENDING"),C["meta_fg"]
            else: result_text,result_color="—",C["meta_fg"]
            ou_text="—"
            if g["ou_pick"]:
                ou_result=self.ou_result(g)
                ou_text=f"{g['ou_pick'].upper()} ({ou_result})" if ou_result else f"{g['ou_pick'].upper()} (PENDING)"
            tk.Label(row,text=f"W{g['week']}",bg=background,fg=C["meta_fg"],font=("Segoe UI",10,"bold"),width=5,anchor="w").pack(side="left")
            tk.Label(row,text=matchup,bg=background,fg=C["team_fg"],font=("Segoe UI",11,"bold"),width=30,anchor="w").pack(side="left")
            tk.Label(row,text=pick_label,bg=background,fg=C["team_fg"],font=("Segoe UI",10,"bold"),width=14,anchor="w").pack(side="left")
            tk.Label(row,text=line,bg=background,fg=C["sources_fg"],font=("Segoe UI",10),width=8,anchor="center").pack(side="left")
            tk.Label(row,text=result_text,bg=background,fg=result_color,font=("Segoe UI",10,"bold"),width=14,anchor="center").pack(side="left")
            tk.Label(row,text=ou_text,bg=background,fg=C["info_fg"],font=("Segoe UI",9,"bold"),width=16,anchor="center").pack(side="left")
            if g["auto_pick_side"]:
                auto_team=(g["home"] if g["auto_pick_side"]=="home" else g["away"]).split()[-1].upper()
                auto_result=self.auto_pick_result(g)
                auto_glyph="✓ " if auto_result=="WON" else ("✗ " if auto_result=="LOST" else "")
                auto_color=C["good_fg"] if auto_result=="WON" else (C["bad_fg"] if auto_result=="LOST" else C["meta_fg"])
                tk.Label(outer,text=f"{auto_glyph}★ algorithm would have picked: {auto_team}"+(f" ({auto_result})" if auto_result else " (pending)"),bg=background,fg=auto_color,font=("Segoe UI",7,"bold"),anchor="w",padx=22).pack(fill="x",pady=(0,5))

        tk.Label(self.rows,text=f"★ MY BETS RECORD (ATS)  {record_text([r for g in bet_games if (r:=self.pick_result(g))])}",bg=C["tracker_bg"],fg=C["tracker_fg"],font=("Segoe UI",9,"bold"),anchor="w",padx=18,pady=8).pack(fill="x",padx=16,pady=(7,3))
        if bet_games:
            heading(self.rows)
            for index,g in enumerate(bet_games,1): pick_row(self.rows,g,index)
        else:
            tk.Label(self.rows,text="No starred bets yet.",bg=C["bg"],fg=C["empty_fg"],font=("Segoe UI",10),pady=10).pack()

        tk.Label(self.rows,text=f"MY PICKS RECORD (ATS)  {record_text([r for g in pick_games if (r:=self.pick_result(g))])}   ·   O/U RECORD  {record_text(ou_results)}",bg=C["tracker_bg"],fg=C["tracker_fg"],font=("Segoe UI",9,"bold"),anchor="w",padx=18,pady=8).pack(fill="x",padx=16,pady=(14,3))
        if pick_games:
            heading(self.rows)
            for index,g in enumerate(pick_games,1): pick_row(self.rows,g,index)
        else:
            tk.Label(self.rows,text="No picks made yet.",bg=C["bg"],fg=C["empty_fg"],font=("Segoe UI",10),pady=10).pack()

        tk.Label(self.rows,text=f"★ ALGORITHM PICKS RECORD (ATS)  {record_text(auto_results)}",bg=C["tracker_bg"],fg=C["tracker_fg"],font=("Segoe UI",9,"bold"),anchor="w",padx=18,pady=8).pack(fill="x",padx=16,pady=(14,3))
        tk.Label(self.rows,text="What the automatic Lean Index would have picked in every game it took a real side on -- graded against the spread at the moment it first leaned, tracked purely for comparison against your own picks above.",bg=C["bg"],fg=C["meta_fg"],font=("Segoe UI",8),wraplength=900,justify="left",padx=18).pack(fill="x",pady=(0,6))

    def _compute_team_ranks(self):
        """Our own power-ranking position (1 = best), same order as the POWER
        RANKINGS tab, shown alongside each team's raw 1-10 consensus rating on
        its card. Split out of render() so the print page can get this without
        paying for a full (and, since the dashboard is hidden, pointless)
        rebuild of every on-screen matchup card."""
        with con() as c:
            ranked_teams=c.execute("SELECT name FROM teams WHERE consensus_rating IS NOT NULL ORDER BY consensus_rating DESC,name").fetchall()
        self._team_rank={row["name"]:index for index,row in enumerate(ranked_teams,1)}
        self._team_rank_count=len(ranked_teams)

    def render(self,preserve_scroll=True):
        C=self.C
        # Do not throw a reader back to the top when one of the background data jobs completes.
        prior_position=self.canvas.yview()[0] if preserve_scroll and hasattr(self,"canvas") else 0
        self.paint_week_tabs()
        for w in self.rows.winfo_children():w.destroy()
        if self.view=="rankings":
            self.render_rankings(); self._bind_wheel_recursive(self.rows); self.after_idle(lambda:self.canvas.yview_moveto(prior_position)); return
        if self.view=="picks":
            self.render_picks(); self._bind_wheel_recursive(self.rows); self.after_idle(lambda:self.canvas.yview_moveto(prior_position)); return
        with con() as c:
            games=c.execute("SELECT * FROM games WHERE week=? ORDER BY kickoff",(self.week.get(),)).fetchall()
        self._compute_team_ranks()
        if not games:
            tk.Label(self.rows,text=f"Week {self.week.get()} will appear here after the full 2026 schedule imports.",bg=C["bg"],fg=C["empty_fg"],font=("Segoe UI",13),pady=55).pack();self._bind_wheel_recursive(self.rows);self.after_idle(lambda:self.canvas.yview_moveto(prior_position));return
        settled=[self.pick_result(g) for g in games if g["bet_star"]]
        won=sum(result=="WON" for result in settled);lost=sum(result=="LOST" for result in settled);push=sum(result=="PUSH" for result in settled)
        starred=sum(bool(g["bet_star"]) for g in games)
        tracker=tk.Label(self.rows,text=f"YOUR WEEK {self.week.get()} BET TRACKER   ★ {starred} BET{'S' if starred!=1 else ''}   ·   RESULTS {won}–{lost}"+(f"–{push} PUSH" if push else ""),bg=C["tracker_bg"],fg=C["tracker_fg"],font=("Segoe UI",9,"bold"),anchor="w",padx=18,pady=8)
        tracker.pack(fill="x",padx=16,pady=(7,3))
        top_plays=self._compute_top_plays(games)
        if top_plays:
            top_text="MOST FAVORED GAMES THIS WEEK   "+"   ·   ".join(f"{p['fav']} {p['fav_spread']:+g}  ({p['score']:g}/10)" for p in top_plays[:5])
            tk.Label(self.rows,text=top_text,bg=C["tracker_bg"],fg=C["accent"],font=("Segoe UI",9,"bold"),anchor="w",padx=18,pady=8,wraplength=1100,justify="left").pack(fill="x",padx=16,pady=(0,3))
        for g in games:
            weather_border=self.weather_border_color(g["event_id"])
            card=tk.Frame(self.rows,bg=C["card_bg"],highlightbackground=weather_border or C["card_border"],highlightthickness=3 if weather_border else 1,padx=0,pady=0);card.pack(fill="x",padx=16,pady=3);card.grid_columnconfigure(0,weight=5);card.grid_columnconfigure(1,weight=3);card.grid_columnconfigure(2,weight=5)
            venue=self.venue_label(g["home"],g["away"])
            home_team,away_team=self.team(g["home"]),self.team(g["away"])
            home_rest,away_rest=self.days_rest(g["home"],g["kickoff"]),self.days_rest(g["away"],g["kickoff"])
            card_pick_result=self.pick_result(g)
            self.club(card,home_team,"home",venue,g["pick_side"]=="home",bool(g["bet_star"]) and g["pick_side"]=="home",rest_days=home_rest,event_id=g["event_id"],pick_result=card_pick_result if g["pick_side"]=="home" else None,away=g["away"],week=g["week"])
            mid=tk.Frame(card,bg=C["mid_bg"],padx=10,pady=8,highlightbackground=C["mid_border"],highlightthickness=1);mid.grid(row=0,column=1,sticky="nsew")
            try:
                # %-d / %-I (drop the leading zero) are Unix-only strftime extensions.
                # On Windows this raises and silently fell back to the raw ISO string --
                # that's the "wrong date/time format" bug. Building it by hand instead
                # works identically on every platform.
                dt=datetime.fromisoformat(g["kickoff"].replace("Z","+00:00")).astimezone()
                hour12=dt.hour%12 or 12
                display=f"{dt.month}/{dt.day}/{dt.year} {hour12}:{dt:%M %p}"
            except Exception:display=g["kickoff"]
            # Consistent vertical rhythm down the mid column: a small 1px gap between
            # lines that belong to the same group (e.g. spread+total), a bigger 4px
            # gap when a new group starts. Which lines actually appear varies game to
            # game (no score yet, no pick made, etc.) -- this keeps whatever IS shown
            # spaced the same way every time, instead of some lines packing tight and
            # others packing flush by accident.
            GAP,SECTION=(1,0),(4,0)
            tk.Label(mid,text=display,bg=C["mid_bg"],fg=C["kickoff_fg"],font=("Segoe UI",9,"bold")).pack(pady=(0,3))
            if frozenset({g["home"],g["away"]}) in RIVALRIES:
                tk.Label(mid,text="⚔ RIVALRY GAME",bg=C["mid_bg"],fg=C["info_fg"],font=("Segoe UI",8,"bold")).pack(pady=GAP)
            # Full algorithm reasoning (rating gap, projected score, factor breakdown,
            # lean index) now lives entirely in the GAME INTEL popup -- see
            # show_injuries's "ALGORITHM NOTES" section -- so it isn't duplicated here.
            # The dash keeps just the algorithm's own spread, since that's the one
            # number worth comparing to the market line at a glance.
            projection=None
            if home_team["consensus_rating"] is not None and away_team["consensus_rating"] is not None:
                projection=self.projected_game(g,home_team,away_team,home_rest,away_rest)
                fav=g["home"] if projection["home_spread"]<0 else g["away"]
                fav_spread=projection["home_spread"] if projection["home_spread"]<0 else -projection["home_spread"]
                your_spread_line="YOUR SPREAD: PICK 'EM" if projection["home_spread"]==0 else f"YOUR SPREAD: {fav.split()[-1].upper()} {fav_spread:+g}"
                tk.Label(mid,text=your_spread_line,bg=C["mid_bg"],fg=C["info_fg"],font=("Segoe UI",8,"bold")).pack(pady=GAP)
            score,breakdown=self.confidence_score(g,home_team,away_team,home_rest,away_rest)
            self.sync_auto_pick(g,score)
            spread = "—" if g["dk_spread"] is None else f"{g['home'].split()[-1].upper()} {g['dk_spread']:+g}"
            total = "O/U —" if g["dk_total"] is None else f"O/U {g['dk_total']:g}"
            tk.Label(mid,text=spread,bg=C["mid_bg"],fg=C["accent"],font=("Segoe UI",15,"bold")).pack(pady=SECTION)
            tk.Label(mid,text=total,bg=C["mid_bg"],fg=C["total_fg"],font=("Segoe UI",11,"bold")).pack(pady=GAP)
            opening=self.opening_line(g["event_id"])
            if opening and g["dk_spread"] is not None and opening["home_spread"] is not None and opening["home_spread"]!=g["dk_spread"]:
                tk.Label(mid,text=f"OPENED {g['home'].split()[-1].upper()} {opening['home_spread']:+g} → NOW {g['dk_spread']:+g}",bg=C["mid_bg"],fg=C["meta_fg"],font=("Segoe UI",8)).pack(pady=GAP)
            if g["home_bets"] is not None:
                tk.Label(mid,text=f"{g['home'].split()[-1]}  {g['home_bets']}% bets · {g['home_handle']}% money",bg=C["mid_bg"],fg=C["bets_fg"],font=("Segoe UI",8)).pack(pady=SECTION)
                tk.Label(mid,text=f"{g['away'].split()[-1]}  {g['away_bets']}% bets · {g['away_handle']}% money",bg=C["mid_bg"],fg=C["bets_fg"],font=("Segoe UI",8)).pack(pady=GAP)
            results_started=False
            def result_pady():
                nonlocal results_started
                if results_started: return GAP
                results_started=True
                return SECTION
            if g["home_score"] is not None and g["away_score"] is not None:
                if g["dk_spread"] is None: cover="FINAL"
                else:
                    result=g["home_score"]-g["away_score"]+g["dk_spread"]
                    cover=f"{g['home'].split()[-1].upper()} COVERED" if result>0 else (f"{g['away'].split()[-1].upper()} COVERED" if result<0 else "PUSH")
                tk.Label(mid,text=f"FINAL {g['home_score']}–{g['away_score']}  ·  {cover}",bg=C["mid_bg"],fg=C["final_fg"],font=("Segoe UI",9,"bold")).pack(pady=result_pady())
            pick=self.pick_result(g)
            if pick:
                pick_color=C["good_fg"] if pick=="WON" else (C["bad_fg"] if pick=="LOST" else C["meta_fg"])
                pick_glyph="✓ " if pick=="WON" else ("✗ " if pick=="LOST" else "")
                tk.Label(mid,text=f"{pick_glyph}YOUR {'BET' if g['bet_star'] else 'PICK'}: {pick}",bg=C["mid_bg"],fg=pick_color,font=("Segoe UI",9,"bold")).pack(pady=result_pady())
            ou=self.ou_result(g)
            if ou:
                ou_color=C["good_fg"] if ou=="WON" else (C["bad_fg"] if ou=="LOST" else C["meta_fg"])
                tk.Label(mid,text=f"YOUR O/U PICK ({g['ou_pick'].upper()}): {ou}",bg=C["mid_bg"],fg=ou_color,font=("Segoe UI",9,"bold")).pack(pady=result_pady())
            picks=tk.Frame(mid,bg=C["mid_bg"]);picks.pack(pady=SECTION)
            home_short=g["home"].split()[-1].upper();away_short=g["away"].split()[-1].upper()
            tk.Button(picks,text=home_short,command=lambda e=g["event_id"]:self.save_pick(e,"home"),bg=C["badge_selected"] if g["pick_side"]=="home" else C["badge_bg"],fg=C["badge_fg"],relief="flat",font=("Segoe UI",7,"bold"),padx=4,pady=3).pack(side="left",padx=(0,2))
            tk.Button(picks,text=away_short,command=lambda e=g["event_id"]:self.save_pick(e,"away"),bg=C["badge_selected"] if g["pick_side"]=="away" else C["badge_bg"],fg=C["badge_fg"],relief="flat",font=("Segoe UI",7,"bold"),padx=4,pady=3).pack(side="left")
            tk.Button(mid,text="★ BET" if not g["bet_star"] else "★ BET SAVED",command=lambda e=g["event_id"]:self.toggle_bet(e),bg=C["good_fg"] if g["bet_star"] else C["bet_bg"],fg="white" if g["bet_star"] else C["bet_fg"],activebackground=C["accent"],activeforeground="white",relief="flat",font=("Segoe UI",8,"bold"),padx=8,pady=3).pack(pady=(3,0))
            tk.Button(mid,text="GAME INTEL",command=lambda h=g["home"],a=g["away"],wk=g["week"]:self.show_injuries(h,a,wk),bg=C["bet_bg"],fg=C["bet_fg"],activebackground=C["accent"],activeforeground="white",relief="flat",font=("Segoe UI",8,"bold"),padx=8,pady=3).pack(pady=(3,0))
            if g["auto_pick_side"]:
                auto_team=(g["home"] if g["auto_pick_side"]=="home" else g["away"]).split()[-1].upper()
                auto_result=self.auto_pick_result(g)
                auto_glyph="✓ " if auto_result=="WON" else ("✗ " if auto_result=="LOST" else "")
                auto_color=C["good_fg"] if auto_result=="WON" else (C["bad_fg"] if auto_result=="LOST" else C["meta_fg"])
                tk.Label(mid,text=f"{auto_glyph}★ ALGORITHM PICK: {auto_team}",bg=C["mid_bg"],fg=auto_color,font=("Segoe UI",7,"bold")).pack(pady=(6,0))
                tk.Label(mid,text=self.auto_pick_note(g,home_rest,away_rest),bg=C["mid_bg"],fg=C["meta_fg"],font=("Segoe UI",7),wraplength=260,justify="left").pack(pady=(2,0))
            self.club(card,away_team,"away",selected=g["pick_side"]=="away",starred=bool(g["bet_star"]) and g["pick_side"]=="away",rest_days=away_rest,event_id=g["event_id"],pick_result=card_pick_result if g["pick_side"]=="away" else None,week=g["week"],home_name=g["home"])
        self._bind_wheel_recursive(self.rows)
        self.after_idle(lambda:self.canvas.yview_moveto(prior_position))

    def refresh_schedule(self):
        self.status.set("Refreshing the full NFL regular-season schedule from ESPN…");threading.Thread(target=self._schedule_worker,daemon=True).start()

    def _schedule_worker(self):
        year=datetime.now().year; saved=0; failed_weeks=[]
        # Each week gets its own connection/transaction. One bad week (a
        # timeout, a malformed response) used to blow up the single shared
        # transaction and roll back every week already saved in this run --
        # that's why only the Week 1 fallback ever stuck around. Isolating
        # weeks means a bad week is skipped, not catastrophic.
        for requested_week in range(1,19):
            try:
                first_day=date(year,9,9)+timedelta(days=(requested_week-1)*7)
                last_day=first_day+timedelta(days=7)
                data=get_json(ESPN_SCOREBOARD+"?"+urlencode({"limit":100,"dates":year,"seasontype":2,"week":requested_week}))
                with con() as c:
                    for e in data.get("events",[]):
                        if e.get("season",{}).get("type")!=2:continue
                        comp=e["competitions"][0]; cs=comp["competitors"];home_team=next(x for x in cs if x["homeAway"]=="home");away_team=next(x for x in cs if x["homeAway"]=="away");home=home_team["team"]["displayName"];away=away_team["team"]["displayName"]
                        if home not in dict(TEAMS) or away not in dict(TEAMS):continue
                        completed=comp.get("status",{}).get("type",{}).get("completed",False)
                        home_score=int(home_team.get("score",0)) if completed else None;away_score=int(away_team.get("score",0)) if completed else None
                        status="FINAL" if completed else comp.get("status",{}).get("type",{}).get("shortDetail","")
                        c.execute("INSERT INTO games(event_id,week,kickoff,home,away,home_score,away_score,game_status) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET week=excluded.week,kickoff=excluded.kickoff,home=excluded.home,away=excluded.away,home_score=COALESCE(excluded.home_score,games.home_score),away_score=COALESCE(excluded.away_score,games.away_score),game_status=excluded.game_status",(e["id"],requested_week,e["date"],home,away,home_score,away_score,status));saved+=1
                        # The Week 1 fallback row seeded at first launch used a made-up event_id
                        # ("week1-2026-...") since it doesn't know ESPN's real one yet. Once the
                        # live pull confirms the same matchup with ESPN's real ID, that fallback
                        # row becomes a second, duplicate card for the same game -- carry any
                        # picks over to the row we're keeping, then remove the duplicate.
                        duplicate=c.execute("SELECT * FROM games WHERE week=? AND home=? AND away=? AND event_id!=?",(requested_week,home,away,e["id"])).fetchone()
                        if duplicate:
                            c.execute("UPDATE games SET pick_side=COALESCE(pick_side,?),bet_star=MAX(bet_star,?),ou_pick=COALESCE(ou_pick,?),dk_spread=COALESCE(dk_spread,?),dk_total=COALESCE(dk_total,?),home_bets=COALESCE(home_bets,?),home_handle=COALESCE(home_handle,?),away_bets=COALESCE(away_bets,?),away_handle=COALESCE(away_handle,?),line_source=COALESCE(line_source,?),line_checked=COALESCE(line_checked,?) WHERE event_id=?",(duplicate["pick_side"],duplicate["bet_star"],duplicate["ou_pick"],duplicate["dk_spread"],duplicate["dk_total"],duplicate["home_bets"],duplicate["home_handle"],duplicate["away_bets"],duplicate["away_handle"],duplicate["line_source"],duplicate["line_checked"],e["id"]))
                            c.execute("UPDATE games SET pick_spread=COALESCE(pick_spread,?),pick_recorded=COALESCE(pick_recorded,?) WHERE event_id=?",(duplicate["pick_spread"],duplicate["pick_recorded"],e["id"]))
                            c.execute("DELETE FROM games WHERE event_id=?",(duplicate["event_id"],))
            except Exception:
                failed_weeks.append(requested_week)
        if saved==0:
            try: saved=self._import_nflverse_schedule(year)
            except Exception as ex:
                self._ui_queue.put(lambda ex=ex:self.status.set(f"Schedule refresh failed: {ex}")); return
        note=f" (weeks {', '.join(map(str,failed_weeks))} didn't respond -- will retry next refresh)" if failed_weeks else ""
        self._ui_queue.put(lambda:self._schedule_done(saved,note))

    def _import_nflverse_schedule(self,year):
        """Fallback: NFLverse publishes a complete game/schedule CSV for the season."""
        request=Request(NFLVERSE_GAMES,headers={"User-Agent":"Personal-NFL-Handicapping-Board/1.0"})
        with urlopen(request,timeout=35) as response:
            rows=csv.DictReader(io.TextIOWrapper(response,encoding="utf-8"))
            abbr_to_name=dict((abbr,name) for name,abbr in TEAMS);saved=0
            with con() as c:
                for row in rows:
                    if row.get("season")!=str(year) or row.get("game_type")!="REG":continue
                    home=abbr_to_name.get((row.get("home_team") or "").lower());away=abbr_to_name.get((row.get("away_team") or "").lower())
                    if not home or not away:continue
                    game_id="nflverse-"+(row.get("game_id") or f"{year}-{row['week']}-{home}-{away}")
                    kickoff=f"{row.get('gameday','')}T{row.get('gametime') or '00:00'}:00"
                    c.execute("INSERT INTO games(event_id,week,kickoff,home,away,dk_spread,dk_total) VALUES(?,?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET week=excluded.week,kickoff=excluded.kickoff,home=excluded.home,away=excluded.away",(game_id,int(row["week"]),kickoff,home,away,row.get("spread_line") or None,row.get("total_line") or None));saved+=1
        return saved

    def _schedule_done(self,saved,note=""):
        self.refresh_weeks()
        with con() as c:r=c.execute("SELECT week FROM games WHERE kickoff>=? ORDER BY kickoff LIMIT 1",(datetime.now(timezone.utc).isoformat(),)).fetchone()
        if r:self.week.set(r[0])
        self.render();self.status.set(f"Schedule ready: {saved} games loaded{note}. Checking DraftKings betting splits…");self.refresh_public_lines()

    def refresh_weather(self):
        """Live kickoff-hour forecast for this week's outdoor stadiums, via Open-Meteo
        (free, no key). Domes get a one-time "climate controlled" flag instead of a
        live fetch. Forecasts only exist for the near future, so games more than
        ~2 weeks out are silently skipped until they're back in range -- this just
        refreshes on every open + every 15 min like the rest of the board."""
        threading.Thread(target=self._weather_worker,args=(self.week.get(),),daemon=True).start()

    def _weather_worker(self,season_week):
        try:
            with con() as c:
                games=c.execute("SELECT event_id,home,away,kickoff FROM games WHERE week=?",(season_week,)).fetchall()
            updated=0
            for g in games:
                neutral=NEUTRAL_SITES.get((g["home"],g["away"]))
                if neutral: lat,lon,indoors=neutral[0],neutral[1],neutral[2]
                else:
                    stadium=STADIUMS.get(g["home"])
                    if not stadium: continue
                    lat,lon,indoors=stadium
                checked=datetime.now().isoformat(timespec="minutes")
                if indoors:
                    with con() as c:
                        c.execute("INSERT INTO weather(event_id,dome,checked_at) VALUES(?,1,?) ON CONFLICT(event_id) DO UPDATE SET dome=1,checked_at=excluded.checked_at",(g["event_id"],checked))
                    updated+=1
                    continue
                try:
                    dt=datetime.fromisoformat(g["kickoff"].replace("Z","+00:00")).astimezone(timezone.utc)
                except Exception:
                    continue
                date_str=dt.strftime("%Y-%m-%d")
                params=urlencode({"latitude":lat,"longitude":lon,"hourly":"temperature_2m,precipitation,windspeed_10m,windgusts_10m,precipitation_probability,weathercode","temperature_unit":"fahrenheit","windspeed_unit":"mph","start_date":date_str,"end_date":(dt+timedelta(hours=3)).strftime("%Y-%m-%d"),"timezone":"UTC"})
                try:
                    data=get_json(OPEN_METEO+"?"+params)
                except Exception:
                    continue  # forecast likely too far out yet -- try again on the next refresh
                hourly=data.get("hourly",{})
                try: forecast=forecast_window(hourly,dt)
                except ValueError: continue
                with con() as c:
                    c.execute("INSERT INTO weather(event_id,temperature,wind,precipitation,weather_code,dome,checked_at,alert) VALUES(?,?,?,?,?,0,?,?) ON CONFLICT(event_id) DO UPDATE SET temperature=excluded.temperature,wind=excluded.wind,precipitation=excluded.precipitation,weather_code=excluded.weather_code,dome=0,checked_at=excluded.checked_at,alert=excluded.alert",(g["event_id"],forecast["temperature"],forecast["wind"],forecast["precipitation"],forecast["weather_code"],checked,forecast["alert"]))
                updated+=1
            self._ui_queue.put(lambda:(self.queue_render(),self.status.set(f"Weather updated for {updated} Week {season_week} game(s).")))
        except Exception as ex:
            self._ui_queue.put(lambda ex=ex:self.status.set(f"Weather refresh failed: {ex}"))

    def refresh_public_lines(self):
        """Refresh selected-week lines and public split percentages from DK Network."""
        self.status.set("Refreshing DraftKings spread, total, and betting splits…")
        threading.Thread(target=self._public_lines_worker,args=(self.week.get(),),daemon=True).start()

    def _public_lines_worker(self,season_week):
        try:
            def load(market, page_number):
                url=DK_SPLITS+f"&tb_emt={market}&tb_page={page_number}"
                request=Request(url,headers={"User-Agent":"Mozilla/5.0 (personal NFL board)"})
                with urlopen(request,timeout=30) as response: raw=response.read().decode("utf-8","ignore")
                return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw)))
            # DraftKings publishes the NFL board in pages. Four pages cover a full NFL slate.
            spreads=" ".join(load("Spread", page) for page in range(1,5))
            totals=" ".join(load("Total", page) for page in range(1,5))
            # Everything that touches the database for this refresh happens inside
            # ONE connection/transaction. The previous version opened `c` just for
            # the SELECT, then kept using that same (already-exited) connection for
            # every UPDATE/INSERT in the loop below -- those writes were never
            # explicitly committed, so the transaction stayed open (and holding
            # SQLite's single-writer lock) until Python's garbage collector
            # eventually got around to the connection, at some unpredictable later
            # point. That's exactly what caused "database is locked" when clicking
            # a pick button elsewhere in the app.
            with con() as c:
                games=c.execute("SELECT * FROM games WHERE week=?",(season_week,)).fetchall()
                updated=0
                for game in games:
                    values=self._draftkings_game_values(spreads, totals, game["away"], game["home"])
                    if not values: continue
                    checked=datetime.now().isoformat(timespec="minutes")
                    line_values, split_values=values[:6],values[6:]
                    c.execute("UPDATE games SET dk_spread=?,dk_total=?,home_bets=?,home_handle=?,away_bets=?,away_handle=?,line_source=?,line_checked=?,over_bets=?,over_handle=?,under_bets=?,under_handle=? WHERE event_id=?",(*line_values,"DraftKings",checked,*split_values,game["event_id"]))
                    c.execute("INSERT OR IGNORE INTO line_history(event_id,checked_at,home_spread,total,home_bets,home_handle,away_bets,away_handle) VALUES(?,?,?,?,?,?,?,?)",(game["event_id"],checked,*line_values))
                    updated+=1
            self._ui_queue.put(lambda:(self.queue_render(),self.status.set(f"DraftKings update complete: {updated} Week {season_week} game(s) found. It refreshes automatically whenever the board opens.")))
        except Exception as ex:
            self._ui_queue.put(lambda:self.status.set(f"DraftKings update failed: {ex}"))

    def _draftkings_game_values(self, spread_page, total_page, away, home):
        """Read a single NFL game card from DK Network's public Betting Splits page.
        The page is public web content, not a private DraftKings account/API connection.
        """
        abbr=dict(TEAMS)
        def label(team):
            code={"wsh":"WAS","nyg":"NY","nyj":"NY"}.get(abbr[team],abbr[team].upper())
            return f"{code} {team.split()[-1]}"
        away_label, home_label=label(away),label(home)
        def game_card(page):
            found=re.search(rf"{re.escape(away_label)}\s*@\s*{re.escape(home_label)}",page,re.I)
            return page[found.start():found.start()+1500] if found else ""
        spread_card, total_card=game_card(spread_page),game_card(total_page)
        if not spread_card: return None
        # On the DK page each side is: TEAM +POINT AMERICAN-ODDS HANDLE% BETS%.
        figures=re.findall(r"([A-Z]{2,3})\s+[A-Za-z]+\s+([+−-]\d+(?:\.\d+)?)\s+[+−]\d+\s+(\d{1,3})%\s+(\d{1,3})%",spread_card)
        home_code={"wsh":"WAS","nyg":"NY","nyj":"NY"}.get(abbr[home],abbr[home].upper())
        away_code={"wsh":"WAS","nyg":"NY","nyj":"NY"}.get(abbr[away],abbr[away].upper())
        parsed={code:(float(point.replace("−","-")),int(handle),int(bets)) for code,point,handle,bets in figures}
        if home_code not in parsed or away_code not in parsed: return None
        home_line, home_handle, home_bets=parsed[home_code]
        _away_line, away_handle, away_bets=parsed[away_code]
        total_value=None
        over_bets=over_handle=under_bets=under_handle=None
        if total_card:
            # Same "TEAM POINT ODDS HANDLE% BETS%" shape as the spread card, just
            # with Over/Under standing in for the team code.
            found=re.search(r"Over\s+(\d+(?:\.\d+)?)\s+[+−]\d+\s+(\d{1,3})%\s+(\d{1,3})%",total_card,re.I)
            if found:
                total_value=float(found.group(1))
                over_handle,over_bets=int(found.group(2)),int(found.group(3))
            found=re.search(r"Under\s+\d+(?:\.\d+)?\s+[+−]\d+\s+(\d{1,3})%\s+(\d{1,3})%",total_card,re.I)
            if found:
                under_handle,under_bets=int(found.group(1)),int(found.group(2))
        return (home_line,total_value,home_bets,home_handle,away_bets,away_handle,over_bets,over_handle,under_bets,under_handle)

if __name__=="__main__":
    # .pyw runs with no console window, so an unhandled exception normally
    # just closes the app with zero visible output -- that's almost
    # certainly what a "random crash" looks like from the outside. Every
    # startup exception and every background-callback exception now gets
    # written to crash_log.txt next to the database, and startup failures
    # also pop a message box, so a crash is diagnosable instead of silent.
    import traceback
    LOG_PATH = APP_DIR / "crash_log.txt"
    def _log_crash(context: str) -> str:
        entry = f"\n--- {datetime.now().isoformat(timespec='seconds')} ({context}) ---\n{traceback.format_exc()}"
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f: f.write(entry)
        except Exception: pass
        return entry
    try:
        _fix_windows_dpi_scaling(); init_db(); app = Board()
        app.report_callback_exception = lambda exc, val, tb: _log_crash("background callback")
        app.mainloop()
    except Exception:
        entry = _log_crash("startup")
        try:
            root = tk.Tk(); root.withdraw()
            messagebox.showerror("NFL Handicapping Board crashed",
                f"Startup failed. Details were saved to:\n{LOG_PATH}\n\n{entry[-600:]}")
        except Exception: pass
