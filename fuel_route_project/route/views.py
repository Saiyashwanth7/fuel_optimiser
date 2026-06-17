"""
route/views.py

Single endpoint: GET /api/route/?start=<location>&finish=<location>

Flow (one request):
  1. Geocode start + finish via Nominatim (2 calls, cached after first use)
  2. Fetch route polyline from ORS (1 API call)
  3. Bounding-box DB query to narrow ~6700 stations to ~200-500
  4. Proximity filter to stations within 5 miles of polyline (in-memory)
  5. Greedy optimizer picks cheapest fuel stops (in-memory, milliseconds)
  6. Return JSON with stops, costs, and GeoJSON geometry
"""

import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import FuelStation
from .services.geocoding import geocode_address
from .services.optimizer import filter_stations_near_route, greedy_fuel_optimizer
from .services.routing import get_route

logger = logging.getLogger(__name__)


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
        # 1. Geocode start and finish
        # ------------------------------------------------------------------ #
        start_coords = geocode_address(start)
        if not start_coords:
            return Response(
                {
                    "error": (
                        f"Could not geocode start location: '{start}'. "
                        "Please use a format like 'Chicago, IL' or 'Dallas, TX'."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        finish_coords = geocode_address(finish)
        if not finish_coords:
            return Response(
                {
                    "error": (
                        f"Could not geocode finish location: '{finish}'. "
                        "Please use a format like 'Chicago, IL' or 'Dallas, TX'."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        start_lat, start_lon = start_coords
        finish_lat, finish_lon = finish_coords

        # ------------------------------------------------------------------ #
        # 2. Fetch route from ORS (single API call)
        # ------------------------------------------------------------------ #
        try:
            route = get_route(
                [start_lon, start_lat],   # ORS expects [lon, lat]
                [finish_lon, finish_lat],
            )
        except Exception as exc:
            logger.error(f"ORS routing failed: {exc}")
            return Response(
                {"error": f"Routing service error: {str(exc)}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        polyline = route["geometry"]       # [[lon, lat], ...]
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