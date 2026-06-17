"""
route/services/geocoding.py

Nominatim geocoding for the start/finish locations provided at request time.
Station geocoding is NOT done here — stations are pre-loaded via load_stations.

Cache: simple in-process dict. For production, swap with Django's cache framework
(Redis). For the assessment, in-process is fine — the server process is long-lived.
"""

import logging
import time

import requests

logger = logging.getLogger(__name__)

_cache: dict[str, tuple[float, float] | None] = {}

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "SpotterFuelRouteAssessment/1.0"


def geocode_address(location: str) -> tuple[float, float] | None:
    """
    Geocode a free-form US location string (e.g. "Chicago, IL" or "New York, NY").

    Returns (lat, lon) or None if not found.
    Results are cached in-process.
    """
    key = location.strip().lower()
    if key in _cache:
        return _cache[key]

    params = {
        "q": location.strip() + ", USA",
        "format": "json",
        "limit": 1,
        "countrycodes": "us",
    }
    headers = {"User-Agent": USER_AGENT}

    try:
        resp = requests.get(NOMINATIM_URL, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        time.sleep(1)  # Nominatim rate-limit: 1 req/sec

        if data:
            lat = float(data[0]["lat"])
            lon = float(data[0]["lon"])
            _cache[key] = (lat, lon)
            logger.info(f"Geocoded '{location}' -> ({lat}, {lon})")
            return lat, lon

        logger.warning(f"Nominatim returned no results for '{location}'")
        _cache[key] = None
        return None

    except requests.RequestException as exc:
        logger.error(f"Geocoding request failed for '{location}': {exc}")
        return None