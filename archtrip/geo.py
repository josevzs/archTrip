"""Geocoding (Nominatim), driving times (OSRM) and the pure-math fallbacks."""
import math
import threading
import time

import requests

USER_AGENT = "archTrip/0.1 (https://github.com/josev/archTrip; herramienta docente)"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
TIMEOUT = 10

# Nominatim's usage policy: at most 1 request/second. Enforced process-wide,
# which is why Docker runs a single gunicorn worker.
NOMINATIM_MIN_INTERVAL = 1.1
_nominatim_lock = threading.Lock()
_nominatim_last = 0.0

# Fallback when OSRM is unreachable: straight line * road factor at avg speed.
ROAD_FACTOR = 1.3
AVG_KMH = 70.0


# ------------------------------------------------------------------ pure

def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearest_stop(lat, lon, stops):
    """stops: iterable of dicts with lat/lon (None-coords are skipped). -> (stop, km) or (None, None)."""
    best, best_km = None, None
    for s in stops:
        if s.get("lat") is None or s.get("lon") is None:
            continue
        km = haversine_km(lat, lon, s["lat"], s["lon"])
        if best_km is None or km < best_km:
            best, best_km = s, km
    return best, best_km


def estimate_drive(straight_km):
    """-> (minutes, km) rough road estimate from a straight-line distance."""
    km = straight_km * ROAD_FACTOR
    return round(km / AVG_KMH * 60, 1), round(km, 1)


# --------------------------------------------------------------- external

def _throttle_nominatim():
    global _nominatim_last
    with _nominatim_lock:
        wait = NOMINATIM_MIN_INTERVAL - (time.monotonic() - _nominatim_last)
        if wait > 0:
            time.sleep(wait)
        _nominatim_last = time.monotonic()


# Nominatim `addresstype` values that mean "a place people live in", as opposed
# to a province/prefecture that happens to share the name (Toyama, Nagano, Fukui…).
SETTLEMENT_TYPES = {"city", "town", "village", "municipality", "hamlet", "borough",
                    "suburb", "quarter", "neighbourhood", "city_district", "district"}


def nominatim_geocode(query, prefer_settlement=False):
    """-> (lat, lon) or None. Raises requests.RequestException on network trouble.

    prefer_settlement: for city names, skip province-level hits when a city/town
    with that name is also in the results ("Nagano, Japón" -> the city, not the
    prefecture centroid 60 km away)."""
    _throttle_nominatim()
    resp = requests.get(
        NOMINATIM_URL,
        params={"q": query, "format": "json", "limit": 5 if prefer_settlement else 1},
        headers={"User-Agent": USER_AGENT, "Accept-Language": "es,en"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data:
        return None
    hit = data[0]
    if prefer_settlement:
        hit = next((d for d in data if d.get("addresstype") in SETTLEMENT_TYPES), data[0])
    return float(hit["lat"]), float(hit["lon"])


def osrm_drive(lat1, lon1, lat2, lon2):
    """-> (minutes, km) or None if OSRM can't route. Raises on network trouble."""
    url = f"{OSRM_URL}/{lon1},{lat1};{lon2},{lat2}"
    resp = requests.get(
        url,
        params={"overview": "false"},
        headers={"User-Agent": USER_AGENT},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "Ok" or not data.get("routes"):
        return None
    route = data["routes"][0]
    return round(route["duration"] / 60, 1), round(route["distance"] / 1000, 1)
