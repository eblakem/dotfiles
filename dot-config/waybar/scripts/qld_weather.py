#!/usr/bin/env python3
"""Weather lookups backing two different uses in qld_price_tui.py /
qld_price_history.py:

- Mean temperature feeds qld_price_history.weather_day_multiplier() - the
  day-level nudge to the historical hour-of-day usage projection based on
  how hot/cold a day is expected to be (see that function's docstring).
- Cloud cover and solar radiation are informational only (not fed into
  any projection): QLD has very high rooftop solar penetration, so a
  sunny day suppresses statewide midday grid demand (and the AEMO spot
  price with it) in a way temperature alone doesn't explain - see the
  "Sky" line/column these back in the TUI. Radiation
  (shortwave_radiation_sum, MJ/sq m) is the more informative of the two
  since it already folds cloud cover, day length and sun angle into one
  number; cloud cover (%) is shown alongside since it's a more familiar
  read at a glance.

Wraps Open-Meteo's free archive (past days) and forecast (next N days)
APIs, both keyed to a fixed lat/lon rather than the wifi/IP geolocation
weather.sh uses - this is a single-location personal energy monitor (same
reasoning as the hardcoded retail Plan in qld_price_tui.py), and a backend
script run from the TUI has no wifi scan to key off like weather.sh does.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

# Brisbane, QLD - matches where weather.sh's wifi/IP geolocation resolves
# this machine to. Fixed rather than geolocated per-call, since nothing
# else in this app is location-aware either.
LAT = -27.592268
LON = 153.06053

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT = 10
DAILY_FIELDS = "temperature_2m_mean,cloudcover_mean,shortwave_radiation_sum"

# Forecast changes slowly enough that re-fetching on every TUI keypress
# (as ungated navigation would) is pure waste - same reasoning as
# qld_price_tui.App._live_rows()'s LIVE_FEED_REFRESH.
FORECAST_CACHE_FILE = os.path.expanduser("~/.cache/qld_weather_forecast.json")
FORECAST_CACHE_MAX_AGE = timedelta(hours=1)

# A past day's recorded weather never changes once published, unlike the
# forecast above - so this is cached indefinitely (grows by whatever days
# get looked up) rather than on a TTL. Same incremental-merge shape as
# qld_price_history.ACTUAL_DATA_CACHE. Each entry is a
# {"temp": .., "cloud": .., "radiation": ..} dict, any field may be None
# if Open-Meteo didn't have it for that day.
HISTORICAL_CACHE_FILE = os.path.expanduser("~/.cache/qld_weather_historical.json")


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as resp:
        return json.load(resp)


def _daily_weather_from_response(data: dict) -> dict[date, dict]:
    daily = data["daily"]
    days = daily["time"]
    temps = daily.get("temperature_2m_mean", [None] * len(days))
    clouds = daily.get("cloudcover_mean", [None] * len(days))
    radiation = daily.get("shortwave_radiation_sum", [None] * len(days))
    result = {}
    for d, t, c, r in zip(days, temps, clouds, radiation):
        if t is None and c is None and r is None:
            continue
        result[datetime.strptime(d, "%Y-%m-%d").date()] = {"temp": t, "cloud": c, "radiation": r}
    return result


def _fetch_archive_weather(start: date, end: date) -> dict[date, dict]:
    url = (
        f"{ARCHIVE_URL}?latitude={LAT}&longitude={LON}"
        f"&start_date={start.isoformat()}&end_date={end.isoformat()}"
        f"&daily={DAILY_FIELDS}&timezone=auto"
    )
    try:
        return _daily_weather_from_response(_get_json(url))
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError, OSError):
        return {}


## In-process cache of HISTORICAL_CACHE_FILE's contents, keyed by nothing (one
# process-wide dict) - historical_daily_weather() is called on every day
# navigation in qld_price_tui.py (once directly for the viewed day, again
# indirectly via project_weather_multiplier()'s historical_daily_temps()
# call, which spans every day actual_data has usage for), and re-opening and
# JSON-parsing the on-disk cache file from scratch each time was showing up
# as part of what made day navigation sluggish. `None` means not loaded yet
# this process; loaded lazily on first call and kept in sync with the file
# by _historical_cache().
_historical_cache: dict[date, dict] | None = None


def _historical_cache_dict() -> dict[date, dict]:
    global _historical_cache
    if _historical_cache is None:
        try:
            with open(HISTORICAL_CACHE_FILE) as f:
                raw = json.load(f)
            _historical_cache = {
                datetime.strptime(d, "%Y-%m-%d").date(): v
                for d, v in raw.items()
                if isinstance(v, dict)
            }
        except (OSError, json.JSONDecodeError, ValueError):
            _historical_cache = {}
    return _historical_cache


def historical_daily_weather(start: date, end: date) -> dict[date, dict]:
    """Daily {"temp", "cloud", "radiation"} for every day in [start, end]
    that has already happened, via Open-Meteo's archive API. Backed by
    HISTORICAL_CACHE_FILE (and, within a process, by an in-memory copy of it
    - see _historical_cache_dict()) so a day already looked up in a previous
    run/call isn't re-fetched or even re-read off disk; only whatever
    sub-range is still missing is pulled. Returns whatever's available on a
    fetch failure (freshly missing days just stay missing rather than
    crashing - callers already treat a missing day as "no data for this
    day")."""
    cache = _historical_cache_dict()

    # The archive API only ever has days that have *already happened* (per
    # this function's own contract above) - today and any later date will
    # never be in it, so a `day not in cache` check alone treats "today" as
    # perpetually missing and re-fetches it over the network on every single
    # call, defeating the cache entirely (this was most of what made
    # qld_price_tui.py's day navigation slow - historical_daily_temps() below
    # always includes today in its range). Clamp the fetchable range so
    # today/future days are just left out of `cache` rather than retried.
    fetch_end = min(end, date.today() - timedelta(days=1))

    day, missing = start, []
    while day <= fetch_end:
        if day not in cache:
            missing.append(day)
        day += timedelta(days=1)

    if missing:
        fetched = _fetch_archive_weather(min(missing), max(missing))
        if fetched:
            cache.update(fetched)
            try:
                os.makedirs(os.path.dirname(HISTORICAL_CACHE_FILE), exist_ok=True)
                tmp = HISTORICAL_CACHE_FILE + ".tmp"
                with open(tmp, "w") as f:
                    json.dump({d.isoformat(): v for d, v in sorted(cache.items())}, f)
                os.replace(tmp, HISTORICAL_CACHE_FILE)
            except OSError:
                pass

    return {d: v for d, v in cache.items() if start <= d <= end}


def forecast_daily_weather(days: int = 16) -> dict[date, dict]:
    """Daily {"temp", "cloud", "radiation"} for today through
    today+`days`-1, from Open-Meteo's forecast API, cached to
    FORECAST_CACHE_FILE for FORECAST_CACHE_MAX_AGE. Returns {} on any
    fetch failure with nothing usable cached."""
    try:
        if os.path.exists(FORECAST_CACHE_FILE):
            age = datetime.now() - datetime.fromtimestamp(os.path.getmtime(FORECAST_CACHE_FILE))
            if age < FORECAST_CACHE_MAX_AGE:
                with open(FORECAST_CACHE_FILE) as f:
                    cached = json.load(f)
                return {
                    datetime.strptime(d, "%Y-%m-%d").date(): v
                    for d, v in cached.items()
                    if isinstance(v, dict)
                }
    except (OSError, json.JSONDecodeError, ValueError):
        pass

    url = (
        f"{FORECAST_URL}?latitude={LAT}&longitude={LON}"
        f"&daily={DAILY_FIELDS}&forecast_days={days}&timezone=auto"
    )
    try:
        result = _daily_weather_from_response(_get_json(url))
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError, OSError):
        return {}
    try:
        with open(FORECAST_CACHE_FILE, "w") as f:
            json.dump({d.isoformat(): v for d, v in result.items()}, f)
    except OSError:
        pass
    return result


def weather_for_day(day: date) -> dict | None:
    """The {"temp", "cloud", "radiation"} record for a single day: the
    actual recorded values if `day` has already happened
    (historical_daily_weather()), or forecast values if it's today or in
    the future (forecast_daily_weather()). Returns None if that day isn't
    covered by either (e.g. beyond the forecast's horizon, or the archive
    hasn't published it yet)."""
    if day < date.today():
        weather = historical_daily_weather(day, day)
    else:
        weather = forecast_daily_weather()
    return weather.get(day)


def historical_daily_temps(start: date, end: date) -> dict[date, float]:
    """Temperature-only view of historical_daily_weather(), for callers
    (qld_price_history.weather_day_multiplier()) that only care about
    temperature."""
    return {
        d: w["temp"]
        for d, w in historical_daily_weather(start, end).items()
        if w.get("temp") is not None
    }


def temp_for_day(day: date) -> float | None:
    """Temperature-only view of weather_for_day(), for callers that only
    care about temperature (see historical_daily_temps())."""
    weather = weather_for_day(day)
    return weather.get("temp") if weather else None
