"""
route/services/geocoding.py

Geocodio is the primary geocoder for start/finish locations at request time.
Nominatim is kept only as a fallback if GEOCODIO_API_KEY isn't set.

Why the switch from Nominatim-only:
Nominatim's free usage policy caps at 1 req/sec *per IP*, and cloud hosting
IPs (Render, Railway, etc.) are shared across many unrelated apps hitting
Nominatim from the same address range — so 429s happen even when this
process respects its own rate limit. Geocodio is already used for the
offline station pre-geocoding (load_stations.py) via GEOCODIO_API_KEY,
so reusing it here for live start/finish lookups removes the flakiest
part of the request path with no new dependency.

Cache: simple in-process dict. For production, swap with Django's cache
framework (Redis). For the assessment, in-process is fine — the server
process is long-lived.
"""

import logging
import time

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

_cache: dict[str, tuple[float, float] | None] = {}

GEOCODIO_URL = "https://api.geocod.io/v1.7/geocode"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "SpotterFuelRouteAssessment/1.0"


def _geocode_via_geocodio(location: str) -> tuple[float, float] | None:
    api_key = getattr(settings, "GEOCODIO_API_KEY", "")
    if not api_key:
        return None

    params = {
        "q": location.strip() + ", USA",
        "api_key": api_key,
        "limit": 1,
    }

    try:
        resp = requests.get(GEOCODIO_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        results = data.get("results", [])
        if results:
            loc = results[0]["location"]
            lat, lon = float(loc["lat"]), float(loc["lng"])
            logger.info(f"Geocoded '{location}' via Geocodio -> ({lat}, {lon})")
            return lat, lon

        logger.warning(f"Geocodio returned no results for '{location}'")
        return None

    except requests.RequestException as exc:
        logger.error(f"Geocodio request failed for '{location}': {exc}")
        return None
    except (KeyError, ValueError, IndexError) as exc:
        logger.error(f"Unexpected Geocodio response for '{location}': {exc}")
        return None


def _geocode_via_nominatim(location: str) -> tuple[float, float] | None:
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
            logger.info(f"Geocoded '{location}' via Nominatim -> ({lat}, {lon})")
            return lat, lon

        logger.warning(f"Nominatim returned no results for '{location}'")
        return None

    except requests.RequestException as exc:
        logger.error(f"Nominatim request failed for '{location}': {exc}")
        return None


def geocode_address(location: str) -> tuple[float, float] | None:
    """
    Geocode a free-form US location string (e.g. "Chicago, IL" or "New York, NY").

    Returns (lat, lon) or None if not found.
    Results are cached in-process.

    Priority: Geocodio (if GEOCODIO_API_KEY set) -> Nominatim fallback.
    """
    key = location.strip().lower()
    if key in _cache:
        return _cache[key]

    coords = _geocode_via_geocodio(location)

    if coords is None:
        coords = _geocode_via_nominatim(location)

    _cache[key] = coords
    return coords
