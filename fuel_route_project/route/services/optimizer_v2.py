"""
route/services/optimizer.py

Core fuel route optimization logic — with vectorized proximity filtering.

Performance notes:
  - filter_stations_near_route() uses NumPy to compute all point distances
    in one vectorized operation per station, instead of a Python loop.
  - cum_dist is a prefix-sum array (same concept as prefix sums in DSA):
    precomputed once O(n), then O(1) lookup per station.
  - polyline is also downsampled to every Kth point for the proximity check
    (with K chosen so spacing <= threshold), reducing 3000 points to ~150.
"""

import math
import logging
import numpy as np
from django.conf import settings

logger = logging.getLogger(__name__)

TANK_RANGE: float = getattr(settings, "VEHICLE_TANK_RANGE_MILES", 500)
MPG: float = getattr(settings, "VEHICLE_MPG", 10)
PROXIMITY_MILES: float = getattr(settings, "STATION_ROUTE_PROXIMITY_MILES", 5)


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Scalar haversine — used only for cum_dist precomputation."""
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.asin(math.sqrt(min(a, 1.0)))


# Using Vectorized haversine to calculate distance between the current point and polyline coorindates using numpy. This makes the greedy approach easier.


def haversine_vectorized(
    slat: float,
    slon: float,
    poly_lats: np.ndarray,  # shape (N,)
    poly_lons: np.ndarray,  # shape (N,)
) -> np.ndarray:
    """
    Vectorized haversine: one station vs ALL polyline points at once.

    Returns array of distances in miles, shape (N,).
    NumPy operates on the full array in C — no Python loop over points.
    This replaces the inner `for i, point in enumerate(polyline)` loop.
    """
    R = 3958.8  # earth radius in miles
    dlat = np.radians(poly_lats - slat)
    dlon = np.radians(poly_lons - slon)
    a = (
        np.sin(dlat / 2) ** 2
        + np.cos(np.radians(slat))
        * np.cos(np.radians(poly_lats))
        * np.sin(dlon / 2) ** 2
    )
    return R * 2 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


# --------------------------------------------------------------------------- #
# Proximity filter — vectorized + downsampled
# --------------------------------------------------------------------------- #


def filter_stations_near_route(
    stations,
    polyline: list[list[float]],  # [[lon, lat], ...]
    threshold_miles: float = PROXIMITY_MILES,
) -> list[dict]:
    """
    Return stations within threshold_miles of any point on the route polyline.

    Two-level speedup vs the naive nested loop:

    1. DOWNSAMPLE the polyline for the proximity check.
       ORS returns ~1 point per 30m (~0.019 miles). Our threshold is 5 miles.
       We only need points spaced at most threshold/2 = 2.5 miles apart to
       guarantee we never miss a nearby station. That's every ~130 points.
       Downsampling 3000 -> ~23 points cuts work by 99% for the distance check.
       We still use the FULL polyline's cum_dist for accurate route-mile positions.

    2. VECTORIZE the distance calculation with NumPy.
       Instead of `for point in polyline: haversine(station, point)` in Python,
       we call haversine_vectorized() which runs a single NumPy operation across
       all (downsampled) points at once — pure C, no Python loop over points.

    Combined effect on a NY→LA query:
      Naive:       500 stations × 3000 points = 1,500,000 haversine calls (Python)
      Optimized:   500 stations ×   23 points =    11,500 NumPy vector ops
      Speedup:     ~100-200x
    """
    n = len(polyline)

    # ---- Full polyline as arrays (used for cum_dist and exact best_idx) ----
    full_lons = np.array([p[0] for p in polyline])
    full_lats = np.array([p[1] for p in polyline])

    # ---- Prefix-sum (cum_dist): same idea as prefix sums in DSA ----
    # cum_dist[i] = total road miles from polyline[0] to polyline[i]
    # Precompute once O(n), then O(1) lookup: cum_dist[best_idx]
    # Without this we'd re-walk 0..best_idx for every station.
    cum_dist = np.zeros(n)
    for i in range(1, n):
        cum_dist[i] = cum_dist[i - 1] + haversine(
            full_lats[i - 1], full_lons[i - 1], full_lats[i], full_lons[i]
        )
    # cum_dist[-1] == total route miles (matches route["distance_miles"])

    # step 1 of filtering
    # ---- Downsampled polyline for fast proximity check ----
    # Keep every K-th point so spacing ≈ threshold_miles / 2
    avg_spacing_miles = cum_dist[-1] / n if n > 1 else 0.02
    K = max(1, int((threshold_miles / 2) / avg_spacing_miles))

    sample_idx = np.arange(0, n, K)
    # Always include the last point so we don't miss stations near the end
    if sample_idx[-1] != n - 1:
        sample_idx = np.append(sample_idx, n - 1)

    sample_lats = full_lats[sample_idx]
    sample_lons = full_lons[sample_idx]

    logger.info(
        f"Polyline: {n} points. Downsample step K={K} → {len(sample_idx)} points "
        f"(spacing ~{avg_spacing_miles * K:.2f} mi). "
        f"Threshold: {threshold_miles} mi."
    )

    # Step 2: The nearby-filtering
    nearby = []
    for station in stations:
        slat, slon = station.lat, station.lon

        # Step A: fast check against downsampled points (vectorized, no Python loop)
        dists_sampled = haversine_vectorized(slat, slon, sample_lats, sample_lons)
        # check if the closest sample is still far away from the padding
        # if the minimum distance is 7 and the padding is 9, then we don't need to consider the station
        if dists_sampled.min() > threshold_miles + avg_spacing_miles * K:
            # Station is definitely too far — skip without touching full polyline
            continue

        # Step B: exact check against full polyline (also vectorized)
        # Only reached by stations that passed the coarse filter (~10-20% of stations)
        dists_full = haversine_vectorized(slat, slon, full_lats, full_lons)
        best_idx = int(np.argmin(dists_full))
        best_dist = dists_full[best_idx]

        if best_dist <= threshold_miles:
            nearby.append(
                {
                    "opis_id": station.opis_id,
                    "name": station.name,
                    "city": station.city,
                    "state": station.state,
                    "lat": slat,
                    "lon": slon,
                    "avg_price": station.avg_price,
                    # O(1) lookup into prefix-sum array, this is the payoff of cum_dist
                    "dist_from_start": float(cum_dist[best_idx]),
                }
            )

    logger.info(
        f"Proximity filter: {len(nearby)} stations within {threshold_miles} mi "
        f"of route (from {stations.count()} candidates)"
    )
    return nearby


# --------------------------------------------------------------------------- #
# Greedy optimizer (unchanged)
# --------------------------------------------------------------------------- #


def fuel_optimizer(
    stations_on_route: list[dict],
    total_route_miles: float,
    tank_range: float = TANK_RANGE,
    mpg: float = MPG,
) -> dict:
    """
    Greedy cheapest-fuel algorithm.

    Args:
        stations_on_route: output of filter_stations_near_route()
        total_route_miles: total trip distance
        tank_range:        max miles per full tank (default 500)
        mpg:               fuel efficiency (default 10)

    Returns:
        {
            "fuel_stops": [
                {
                    "name", "city", "state", "lat", "lon",
                    "avg_price",
                    "dist_from_start",   # miles from trip start
                    "leg_miles",         # miles driven to reach this stop
                    "gallons_needed",    # fuel purchased at this stop
                    "leg_cost_usd",      # cost of this leg
                }
            ],
            "total_cost_usd": float,
        }

    Raises:
        ValueError — if no station is reachable and destination is too far
    """
    # Edge case: trip fits in one tank
    if total_route_miles <= tank_range:
        return {"fuel_stops": [], "total_cost_usd": 0.0}

    # first, sort the stations based on the distance from the start (we use cum_dist list in the above function, that is being used here) from the station dict
    stations = sorted(stations_on_route, key=lambda s: s["dist_from_start"])

    # establish the starting position. The tank range and mpg are hardcoded as 500, 10 respectively
    current_pos = 0.0
    fuel_stops = []
    total_cost = 0.0

    # starting the trip, we try to check the remaining mile and break the while loop if we can reach the destination.
    while True:
        # remaining from the current point.
        remaining = total_route_miles - current_pos

        if remaining <= 0:
            break

        # we check if can we reach the destination from here?
        if remaining <= tank_range:
            break

        # create a list of stations reachable from current position (strictly ahead) (sorted with dist_from_start)

        # REACHABLE STATION: A station is said to be reachable if we can drive to it from the current position without running out of fuel

        # here, we are going to derive a list of coordinates we can reach using the current fuel.
        # we are using the current truck mileage based on the current fuel and mpg
        reachable = [
            s
            for s in stations
            if current_pos < s["dist_from_start"] <= current_pos + tank_range
        ]

        if (
            not reachable
        ):  # if there are no coordinates in the DB which matches the required 500 mile range, then we raise a valueError.
            raise ValueError(
                f"No fuel station found within {tank_range:.0f} miles of "
                f"mile marker {current_pos:.1f}. "
                f"This route segment may pass through a very remote area."
            )

        # Filter to "viable" stations those from the reachable stations.
        # VIABLE STATION: viables are the stations from which we can either reach destination, or reach another station.

        viable = []
        for s in reachable:
            dist_after = s["dist_from_start"]

            # checking if can we reach the destination direclty from this station
            if total_route_miles - dist_after <= tank_range:
                viable.append(s)
                continue

            # checking if can we reach any further station from here?
            can_continue = any(
                dist_after < x["dist_from_start"] <= dist_after + tank_range
                for x in stations
            )
            if can_continue:
                viable.append(s)

        if not viable:
            # Should not happen on a well-connected road network. But we are going to provide a check for this for any rare edge cases.
            viable = reachable
            logger.warning(
                f"No viable station from pos {current_pos:.1f} — "
                f"falling back to cheapest reachable."
            )

        # attain the cheapest viable station with the minimum avg_price
        # as the distance is already handled by the reacheable list (which is sorted based on disctance), we can directly choose the minimum avg price from the viable stations
        best = min(viable, key=lambda s: s["avg_price"])

        leg_miles = best["dist_from_start"] - current_pos
        gallons = leg_miles / mpg
        cost = gallons * best["avg_price"]
        total_cost += cost

        fuel_stops.append(
            {
                **best,
                "leg_miles": round(leg_miles, 1),
                "gallons_needed": round(gallons, 2),
                "leg_cost_usd": round(cost, 2),
            }
        )
        # update the current_pos.
        current_pos = best["dist_from_start"]

    # the while loop ends on the last but one station, we have to handle the last stop.
    # Final leg: last stop (or origin if no stops) → destination
    if fuel_stops:
        last_stop_pos = fuel_stops[-1]["dist_from_start"]
        last_price = fuel_stops[-1]["avg_price"]
    else:
        # Trip < tank_range — already handled above, but just in case
        last_stop_pos = 0.0
        last_price = (
            min(stations_on_route, key=lambda s: s["avg_price"])["avg_price"]
            if stations_on_route
            else 0.0
        )

    # Final result calculation needs the differnce between the last station ["dist_from_start"] and the total_distance.
    final_leg = total_route_miles - last_stop_pos
    final_gallons = final_leg / mpg
    final_cost = final_gallons * last_price
    total_cost += final_cost

    if fuel_stops:
        fuel_stops[-1]["final_leg_miles"] = round(final_leg, 1)
        fuel_stops[-1]["final_leg_cost_usd"] = round(final_cost, 2)

    return {
        "fuel_stops": fuel_stops,
        "total_cost_usd": round(total_cost, 2),
    }
