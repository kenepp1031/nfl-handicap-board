"""Kickoff-hour forecast for a week's outdoor stadiums via Open-Meteo (free, no
key). Domes get a one-time flag instead of a live fetch. Ported near-verbatim
from the old app's forecast_window/weather_flags -- forecasts only exist for
the near future, so games more than ~2 weeks out are silently skipped."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).parent.parent))
from common import STADIUMS, fetch_json
from db.db import connect

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
SNOW_CODES = {71, 73, 75, 77, 85, 86}


def weather_flags(wind=None, gust=None, precipitation=None, probability=None, codes=()) -> str:
    flags = []
    codes = set(c for c in codes if c is not None)
    if wind is not None and wind >= 15:
        flags.append(f"WIND {wind:.0f} mph")
    if gust is not None and gust >= 15:
        flags.append(f"GUSTS {gust:.0f} mph")
    if codes & {56, 57, 66, 67}:
        flags.append("FREEZING RAIN / ICE RISK")
    if codes & SNOW_CODES:
        flags.append("SNOW / WINTRY PRECIP")
    if codes & {51, 53, 55, 61, 63, 65, 80, 81, 82, 95, 96, 99}:
        flags.append("RAIN / SHOWERS")
    if precipitation is not None and precipitation > 0 and not any("RAIN" in f or "SNOW" in f for f in flags):
        flags.append("PRECIPITATION")
    if probability is not None and probability >= 30:
        flags.append(f"PRECIP CHANCE {probability:.0f}%")
    return " | ".join(flags)


def forecast_window(hourly: dict, kickoff: datetime) -> dict:
    start = kickoff.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    times = hourly.get("time", [])
    wanted = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00") for i in range(4)]
    if any(t not in times for t in wanted):
        raise ValueError("Incomplete game-window forecast")
    ix = [times.index(t) for t in wanted]

    def vals(key):
        array = hourly.get(key, [])
        return [array[i] for i in ix if i < len(array) and array[i] is not None]

    def peak(key):
        v = vals(key)
        return max(v) if v else None

    codes = vals("weathercode")
    temperature = vals("temperature_2m")
    wind = peak("windspeed_10m")
    gust = peak("windgusts_10m")
    precip = vals("precipitation")
    probability = peak("precipitation_probability")
    if len(temperature) != 4 or len(vals("windspeed_10m")) != 4 or len(codes) != 4 or len(precip) != 4:
        raise ValueError("Missing forecast measurements")
    total = sum(precip)
    precip_type = None
    if total > 0:
        precip_type = "snow" if set(codes) & SNOW_CODES else "rain"
    return dict(
        temp_f=temperature[0], wind_mph=wind,
        precip_type=precip_type,
        precip_prob=probability,
        alert=weather_flags(wind, gust, total, probability, codes),
    )


def refresh_week(season: int, week: int) -> int:
    updated = 0
    checked = datetime.now(timezone.utc).isoformat(timespec="minutes")
    with connect() as con:
        games = con.execute(
            "SELECT game_id, home_abbr, kickoff_utc FROM games WHERE season=? AND week=?",
            (season, week),
        ).fetchall()
        for g in games:
            stadium = STADIUMS.get(g["home_abbr"])
            if not stadium:
                continue
            lat, lon, indoors = stadium
            if indoors:
                con.execute(
                    "INSERT INTO weather(game_id, checked_at) VALUES(?,?) "
                    "ON CONFLICT(game_id) DO UPDATE SET checked_at=excluded.checked_at",
                    (g["game_id"], checked),
                )
                updated += 1
                continue
            try:
                dt = datetime.fromisoformat(g["kickoff_utc"]).replace(tzinfo=timezone.utc)
            except Exception:
                continue
            params = urlencode({
                "latitude": lat, "longitude": lon,
                "hourly": "temperature_2m,precipitation,windspeed_10m,windgusts_10m,precipitation_probability,weathercode",
                "temperature_unit": "fahrenheit", "windspeed_unit": "mph",
                "start_date": dt.strftime("%Y-%m-%d"),
                "end_date": (dt + timedelta(hours=3)).strftime("%Y-%m-%d"),
                "timezone": "UTC",
            })
            try:
                data = fetch_json(OPEN_METEO + "?" + params)
                forecast = forecast_window(data.get("hourly", {}), dt)
            except Exception:
                continue  # forecast likely too far out yet -- try again next run
            con.execute(
                """INSERT INTO weather(game_id, temp_f, wind_mph, precip_type, precip_prob, alert_text, checked_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(game_id) DO UPDATE SET
                       temp_f=excluded.temp_f, wind_mph=excluded.wind_mph, precip_type=excluded.precip_type,
                       precip_prob=excluded.precip_prob, alert_text=excluded.alert_text, checked_at=excluded.checked_at""",
                (g["game_id"], forecast["temp_f"], forecast["wind_mph"], forecast["precip_type"],
                 forecast["precip_prob"], forecast["alert"], checked),
            )
            updated += 1
    return updated


if __name__ == "__main__":
    from db.db import init_db
    init_db()
    season, week = int(sys.argv[1]), int(sys.argv[2])
    print(f"Updated weather for {refresh_week(season, week)} games")
