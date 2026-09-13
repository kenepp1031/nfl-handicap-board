"""Standalone weather refresh for the web snapshot pipeline.

export_web_snapshot.py only READS the `weather` table -- it never fetches new
forecasts itself, so if the desktop app hasn't been open recently the snapshot
can go out with stale or missing weather for games kicking off soon. This
script does the actual Open-Meteo fetch (same logic as the desktop app's
_weather_worker()/refresh_weather(), reused via import rather than duplicated)
for every game in the local database, so `python refresh_weather.py &&
python export_web_snapshot.py` always ships current hourly conditions.

Forecasts only exist for the near future (Open-Meteo has no data far out or
in the past), so games outside that window are silently skipped -- same
behavior as the desktop app.

Run:  python refresh_weather.py
"""
from __future__ import annotations
import importlib.util
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DESKTOP_PATH = APP_DIR / "NFL_Handicapping_Desktop.pyw"
DB_PATH = APP_DIR / "nfl_handicapping_board.db"


def _load_desktop_module():
    spec = importlib.util.spec_from_file_location("nfl_desktop_calc", DESKTOP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    D = _load_desktop_module()
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    games = con.execute("SELECT event_id, home, away, kickoff FROM games").fetchall()
    updated = skipped = 0
    for g in games:
        neutral = D.NEUTRAL_SITES.get((g["home"], g["away"]))
        if neutral:
            lat, lon, indoors = neutral[0], neutral[1], neutral[2]
        else:
            stadium = D.STADIUMS.get(g["home"])
            if not stadium:
                skipped += 1
                continue
            lat, lon, indoors = stadium

        checked = datetime.now().isoformat(timespec="minutes")
        if indoors:
            con.execute(
                "INSERT INTO weather(event_id,dome,checked_at) VALUES(?,1,?) "
                "ON CONFLICT(event_id) DO UPDATE SET dome=1,checked_at=excluded.checked_at",
                (g["event_id"], checked),
            )
            con.commit()
            updated += 1
            continue

        try:
            dt = datetime.fromisoformat(g["kickoff"].replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(timezone.utc)
        except Exception:
            skipped += 1
            continue

        from urllib.parse import urlencode
        date_str = dt.strftime("%Y-%m-%d")
        params = urlencode({
            "latitude": lat, "longitude": lon,
            "hourly": "temperature_2m,precipitation,windspeed_10m,windgusts_10m,precipitation_probability,weathercode",
            "temperature_unit": "fahrenheit", "windspeed_unit": "mph",
            "start_date": date_str, "end_date": (dt + timedelta(hours=3)).strftime("%Y-%m-%d"),
            "timezone": "UTC",
        })
        try:
            data = D.get_json(D.OPEN_METEO + "?" + params)
        except Exception as ex:
            skipped += 1
            print(f"  skip {g['home']} vs {g['away']}: fetch failed ({ex})")
            continue

        hourly = data.get("hourly", {})
        try:
            forecast = D.forecast_window(hourly, dt)
        except ValueError:
            skipped += 1
            print(f"  skip {g['home']} vs {g['away']}: forecast out of Open-Meteo's range for {date_str}")
            continue

        con.execute(
            "INSERT INTO weather(event_id,temperature,wind,precipitation,weather_code,dome,checked_at,alert) "
            "VALUES(?,?,?,?,?,0,?,?) ON CONFLICT(event_id) DO UPDATE SET "
            "temperature=excluded.temperature,wind=excluded.wind,precipitation=excluded.precipitation,"
            "weather_code=excluded.weather_code,dome=0,checked_at=excluded.checked_at,alert=excluded.alert",
            (g["event_id"], forecast["temperature"], forecast["wind"], forecast["precipitation"],
             forecast["weather_code"], checked, forecast["alert"]),
        )
        con.commit()
        updated += 1
        print(f"  {g['home']} vs {g['away']}: {forecast['temperature']:.0f}F, wind {forecast['wind']:.0f}mph"
              + (f", ALERT: {forecast['alert']}" if forecast["alert"] else ""))

    print(f"\nWeather refreshed for {updated} game(s), skipped {skipped} (too far out, indoor, or no stadium coords).")


if __name__ == "__main__":
    main()
