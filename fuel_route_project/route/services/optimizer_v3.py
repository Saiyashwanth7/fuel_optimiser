import math
import logging
import numpy as np
from django.conf import settings

logger = logging.getLogger(__name__)

TANK_RANGE: float = getattr(settings, "VEHICLE_TANK_RANGE_MILES", 500)
MPG: float = getattr(settings, "VEHICLE_MPG", 10)
PROXIMITY_MILES: float = getattr(settings, "STATION_ROUTE_PROXIMITY_MILES", 5)
STOP_PENALTY: float = getattr(settings, "FUEL_STOP_PENALTY_USD", 2.0)

# --------------------------------------------------------------------------- #
# Geometry helpers (Vectorized)
# --------------------------------------------------------------------------- #


def haversine_vectorized(slat, slon, poly_lats, poly_lons):
    R = 3958.8
    dlat = np.radians(poly_lats - slat)
    dlon = np.radians(poly_lons - slon)
    a = (
        np.sin(dlat / 2) ** 2
        + np.cos(np.radians(slat))
        * np.cos(np.radians(poly_lats))
        * np.sin(dlon / 2) ** 2
    )
    return R * 2 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def filter_stations_near_route(stations, polyline, threshold_miles=PROXIMITY_MILES):
    n = len(polyline)
    full_lons = np.array([p[0] for p in polyline])
    full_lats = np.array([p[1] for p in polyline])

    cum_dist = np.zeros(n)
    for i in range(1, n):
        # Basic haversine for cum_dist
        dlat = np.radians(full_lats[i] - full_lats[i - 1])
        dlon = np.radians(full_lons[i] - full_lons[i - 1])
        a = (
            np.sin(dlat / 2) ** 2
            + np.cos(np.radians(full_lats[i - 1]))
            * np.cos(np.radians(full_lats[i]))
            * np.sin(dlon / 2) ** 2
        )
        cum_dist[i] = cum_dist[i - 1] + 3958.8 * 2 * np.arcsin(
            np.sqrt(np.clip(a, 0, 1))
        )

    nearby = []
    for station in stations:
        dists = haversine_vectorized(station.lat, station.lon, full_lats, full_lons)
        best_idx = int(np.argmin(dists))
        if dists[best_idx] <= threshold_miles:
            nearby.append(
                {
                    "opis_id": station.opis_id,
                    "name": station.name,
                    "city": station.city,
                    "state": station.state,
                    "avg_price": station.avg_price,
                    "dist_from_start": float(cum_dist[best_idx]),
                }
            )
    return nearby


# --------------------------------------------------------------------------- #
# Dynamic Programming Optimizer
# --------------------------------------------------------------------------- #


def greedy_fuel_optimizer(
    stations_on_route: list[dict],
    total_route_miles: float,
    tank_range: float = TANK_RANGE,
    mpg: float = MPG,
    stop_penalty: float = STOP_PENALTY,
) -> dict:
    """
    Globally optimal fuel routing using Dynamic Programming with stop penalties.
    """
    # 1. Setup Nodes: Start (0) + Sorted Stations + Destination (total_route_miles)
    sorted_stations = sorted(stations_on_route, key=lambda s: s["dist_from_start"])

    # Add dummy nodes for Start and Destination
    nodes = (
        [{"dist_from_start": 0.0, "avg_price": 0.0, "name": "Start"}]
        + sorted_stations
        + [
            {
                "dist_from_start": total_route_miles,
                "avg_price": 0.0,
                "name": "Destination",
            }
        ]
    )

    n = len(nodes)
    min_cost = [float("inf")] * n
    parent = [-1] * n
    min_cost[0] = 0.0

    # 2. DP Logic
    # min_cost[i] is the minimum cost to reach node i
    for i in range(n):
        if min_cost[i] == float("inf"):
            continue

        for j in range(i + 1, n):
            dist_ij = nodes[j]["dist_from_start"] - nodes[i]["dist_from_start"]

            if dist_ij > tank_range:
                break  # Cannot reach further

            # Cost calculation:
            # We assume we buy enough fuel at 'i' to reach 'j'
            fuel_needed = dist_ij / mpg

            # Apply stop penalty to all nodes except the Start
            penalty = stop_penalty if i > 0 else 0.0

            cost_to_j = min_cost[i] + (fuel_needed * nodes[i]["avg_price"]) + penalty

            if cost_to_j < min_cost[j]:
                min_cost[j] = cost_to_j
                parent[j] = i

    if min_cost[-1] == float("inf"):
        raise ValueError("Route unreachable with given tank range.")

    # 3. Reconstruct path
    fuel_stops = []
    curr = parent[-1]
    while curr > 0:
        fuel_stops.insert(0, nodes[curr])
        curr = parent[curr]

    return {
        "fuel_stops": fuel_stops,
        "total_cost_usd": round(min_cost[-1], 2),
    }
