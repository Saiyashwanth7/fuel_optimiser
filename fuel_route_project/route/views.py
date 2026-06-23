"""
route/views.py

Single endpoint: GET /api/route/?start=<location>&finish=<location>

Flow (one request):
  1. Resolve start + finish via in-memory cache → DB lookup → geocoding
  2. Fetch route polyline from ORS (1 API call)
  3. Bounding-box DB query to narrow ~6700 stations to ~200-500
  4. Proximity filter to stations within 5 miles of polyline (in-memory)
  5. Greedy optimizer picks cheapest fuel stops (in-memory, milliseconds)
  6. Return JSON with stops, costs, and GeoJSON geometry
"""

# new-views
from django.http import JsonResponse

"""
Add this to route/views.py (or wherever your other views live).

Serves the static frontend HTML at the root URL. Since this is served
from the same Django app, there's no CORS issue -- the fetch() calls
in the HTML to /api/ are same-origin.
"""

from django.shortcuts import render

import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import FuelStation
from .services.geocoding import geocode_address
from .services.optimizer_v2 import filter_stations_near_route, greedy_fuel_optimizer
from .services.routing import get_route

logger = logging.getLogger(__name__)

# In-memory cache for geocoded locations (process-wide)
_geocode_cache: dict[str, tuple[float, float]] = {}


# Check if the location is in the US or not
def is_within_usa(lat: float, lon: float) -> bool:
    return 24.0 <= lat <= 49.5 and -125.0 <= lon <= -66.0


def parse_city_state(location: str) -> tuple[str, str] | None:
    """
    Parse "City, State" format.
    Returns (city, state) or None if invalid.
    """
    parts = [part.strip() for part in location.split(",")]
    if len(parts) != 2:
        return None

    city, state = parts[0], parts[1].upper()
    if len(city) == 0 or len(state) == 0:
        return None

    return city, state


def resolve_location_coords(location: str) -> tuple[float, float] | None:
    """
    Resolve location coordinates with priority:
      1. In-memory cache (by exact location string)
      2. DB fuel stations (by city, state)
      3. Geocoding API (Nominatim)

    Caches results in memory for future requests in this process.
    """
    location_key = location.strip().lower()

    # 1. Check in-memory cache
    if location_key in _geocode_cache:
        logger.info(f"Cache hit for '{location}'")
        return _geocode_cache[location_key]

    coords = None

    # 2. Try DB lookup by city, state
    parsed = parse_city_state(location)
    if parsed:
        city, state = parsed
        station = (
            FuelStation.objects.filter(
                city__iexact=city,
                state__iexact=state,
            )
            .order_by("avg_price")
            .first()
        )

        if station:
            coords = (station.lat, station.lon)
            logger.info(
                f"Resolved '{location}' from DB station: " f"({coords[0]}, {coords[1]})"
            )

    # 3. Fall back to geocoding API
    if coords is None:
        coords = geocode_address(location)
        if coords:
            logger.info(f"Geocoded '{location}' via API: ({coords[0]}, {coords[1]})")

    # 4. Cache in memory if found
    if coords:
        _geocode_cache[location_key] = coords
        return coords

    return None


def frontend_view(request):
    return render(request, "index.html")


def health_check(request):
    return JsonResponse(
        {
            "status": "ok",
            "message": "Try https://fueloptimiser-production.up.railway.app/api/?start=Chicago,+IL&finish=Dallas,+TX",
        }
    )


class RouteView(APIView):
    """
    GET /api/route/

    Query params:
        start  — US location string, e.g. "New York, NY"
        finish — US location string, e.g. "Los Angeles, CA"
    """

    def get(self, request):
        start = request.query_params.get("start", "").strip()
        finish = request.query_params.get("finish", "").strip()

        # ------------------------------------------------------------------ #
        # Validate inputs
        # ------------------------------------------------------------------ #
        if not start or not finish:
            return Response(
                {"error": "Both 'start' and 'finish' query parameters are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if start.lower() == finish.lower():
            return Response(
                {
                    "start": start,
                    "finish": finish,
                    "total_distance_miles": 0,
                    "total_fuel_cost_usd": 0.0,
                    "fuel_stops": [],
                    "route_geometry": None,
                }
            )

        # ------------------------------------------------------------------ #
        # 1. Resolve start and finish coordinates
        # ------------------------------------------------------------------ #
        start_coords = resolve_location_coords(start)
        if not start_coords:
            return Response(
                {
                    "error": (
                        f"Could not resolve start location: '{start}'. "
                        "Please use a format like 'Chicago, IL' or 'Dallas, TX'."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        finish_coords = resolve_location_coords(finish)
        if not finish_coords:
            return Response(
                {
                    "error": (
                        f"Could not resolve finish location: '{finish}'. "
                        "Please use a format like 'Chicago, IL' or 'Dallas, TX'."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        start_lat, start_lon = start_coords
        finish_lat, finish_lon = finish_coords

        if not is_within_usa(start_lat, start_lon):
            return Response(
                {
                    "error": (
                        f"Start location '{start}' does not appear to be within the USA. "
                        "This service only supports routes within the continental United States."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not is_within_usa(finish_lat, finish_lon):
            return Response(
                {
                    "error": (
                        f"Finish location '{finish}' does not appear to be within the USA. "
                        "This service only supports routes within the continental United States."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ------------------------------------------------------------------ #
        # 2. Fetch route from ORS (single API call)
        # ------------------------------------------------------------------ #
        try:
            route = get_route(
                [start_lon, start_lat],  # ORS expects [lon, lat]
                [finish_lon, finish_lat],
            )
        except Exception as exc:
            logger.error(f"ORS routing failed: {exc}")
            return Response(
                {"error": f"Routing service error: {str(exc)}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        polyline = route["geometry"]  # [[lon, lat], ...]
        total_miles = route["distance_miles"]

        # ------------------------------------------------------------------ #
        # 3. Bounding-box DB query — fast thanks to indexed lat/lon columns
        # ------------------------------------------------------------------ #
        lats = [p[1] for p in polyline]
        lons = [p[0] for p in polyline]
        padding = 0.5  # ~35 miles — generous enough to catch all 5-mile candidates

        candidates = FuelStation.objects.filter(
            lat__gte=min(lats) - padding,
            lat__lte=max(lats) + padding,
            lon__gte=min(lons) - padding,
            lon__lte=max(lons) + padding,
        )

        # ------------------------------------------------------------------ #
        # 4. Proximity filter + project to route distance (in-memory)
        # ------------------------------------------------------------------ #
        nearby = filter_stations_near_route(candidates, polyline)

        if not nearby:
            return Response(
                {
                    "error": (
                        "No fuel stations found within 5 miles of this route. "
                        "The route may pass through an area not covered by the dataset."
                    )
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        # ------------------------------------------------------------------ #
        # 5. Greedy optimizer
        # ------------------------------------------------------------------ #
        try:
            result = greedy_fuel_optimizer(nearby, total_miles)
        except ValueError as exc:
            return Response(
                {"error": str(exc)},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        # ------------------------------------------------------------------ #
        # 6. Build response
        # ------------------------------------------------------------------ #
        return Response(
            {
                "start": start,
                "finish": finish,
                "total_distance_miles": round(total_miles, 1),
                "total_fuel_cost_usd": result["total_cost_usd"],
                "fuel_stop_count": len(result["fuel_stops"]),
                "fuel_stops": result["fuel_stops"],
                "route_geometry": {
                    "type": "LineString",
                    "coordinates": polyline,
                },
            }
        )
