"""
route/services/routing.py

Thin wrapper around OpenRouteService (ORS) directions API.
We make exactly ONE call per user request — fetch the full polyline once,
then do all fuel-stop logic locally against that polyline.
"""

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

ORS_BASE = "https://api.openrouteservice.org/v2/directions/driving-car/geojson"


def get_route(
    start_coords: list[float, float],  # [lon, lat]
    end_coords: list[float, float],    # [lon, lat]
) -> dict:
    """
    Fetch a driving route from ORS.

    Args:
        start_coords: [longitude, latitude] of the start point
        end_coords:   [longitude, latitude] of the end point

    Returns:
        {
            "geometry": [[lon, lat], ...],   # full route polyline
            "distance_miles": float,
        }

    Raises:
        requests.HTTPError  — on 4xx/5xx from ORS
        ValueError          — if ORS response is malformed
    """
    api_key = settings.ORS_API_KEY
    if not api_key:
        raise ValueError(
            "ORS_API_KEY is not set. Add it to your .env file."
        )

    headers = {
        "Authorization": api_key,
        "Content-Type": "application/json",
    }
    body = {"coordinates": [start_coords, end_coords]}

    logger.info(f"ORS route request: {start_coords} -> {end_coords}")

    resp = requests.post(ORS_BASE, json=body, headers=headers, timeout=20)
    resp.raise_for_status()

    data = resp.json()

    try:
        feature = data["features"][0]
        coords = feature["geometry"]["coordinates"]   # list of [lon, lat]
        dist_m = feature["properties"]["summary"]["distance"]
    except (KeyError, IndexError) as exc:
        raise ValueError(f"Unexpected ORS response structure: {exc}") from exc

    dist_miles = dist_m / 1609.344

    logger.info(f"ORS route received: {len(coords)} points, {dist_miles:.1f} miles")

    return {
        "geometry": coords,
        "distance_miles": dist_miles,
    }