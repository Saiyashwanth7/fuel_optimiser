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
            full_lats[i - 1], full_lons[i - 1],
            full_lats[i],     full_lons[i]
        )
    # cum_dist[-1] == total route miles (matches route["distance_miles"])

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

    nearby = []
    for station in stations:
        slat, slon = station.lat, station.lon

        # Step A: fast check against downsampled points (vectorized, no Python loop)
        dists_sampled = haversine_vectorized(slat, slon, sample_lats, sample_lons)
        if dists_sampled.min() > threshold_miles + avg_spacing_miles * K:
            # Station is definitely too far — skip without touching full polyline
            continue

        # Step B: exact check against full polyline (also vectorized)
        # Only reached by stations that passed the coarse filter (~10-20% of candidates)
        dists_full = haversine_vectorized(slat, slon, full_lats, full_lons)
        best_idx = int(np.argmin(dists_full))
        best_dist = dists_full[best_idx]

        if best_dist <= threshold_miles:
            nearby.append({
                "opis_id": station.opis_id,
                "name": station.name,
                "city": station.city,
                "state": station.state,
                "lat": slat,
                "lon": slon,
                "avg_price": station.avg_price,
                # O(1) lookup into prefix-sum array — this is the payoff of cum_dist
                "dist_from_start": float(cum_dist[best_idx]),
            })

    logger.info(
        f"Proximity filter: {len(nearby)} stations within {threshold_miles} mi "
        f"of route (from {stations.count()} candidates)"
    )
    return nearby


# --------------------------------------------------------------------------- #
# Greedy optimizer (unchanged)
# --------------------------------------------------------------------------- #

def greedy_fuel_optimizer(
    stations_on_route: list[dict],
    total_route_miles: float,
    tank_range: float = TANK_RANGE,
    mpg: float = MPG,
) -> dict:
    """
    Greedy cheapest-fuel stop selector.

    Sorts stations by dist_from_start, then at each position picks the
    cheapest station reachable within tank_range that still allows
    continuing to the next stop or destination.
    """
    if total_route_miles <= tank_range:
        return {"fuel_stops": [], "total_cost_usd": 0.0}

    stations = sorted(stations_on_route, key=lambda s: s["dist_from_start"])

    current_pos = 0.0
    fuel_stops = []
    total_cost = 0.0

    while True:
        remaining = total_route_miles - current_pos
        if remaining <= tank_range:
            break

        reachable = [
            s for s in stations
            if current_pos < s["dist_from_start"] <= current_pos + tank_range
        ]

        if not reachable:
            raise ValueError(
                f"No fuel station found within {tank_range:.0f} miles of "
                f"mile marker {current_pos:.1f}. "
                "This route segment may pass through a very remote area."
            )

        viable = []
        for s in reachable:
            d = s["dist_from_start"]
            if total_route_miles - d <= tank_range:
                viable.append(s)
                continue
            if any(d < x["dist_from_start"] <= d + tank_range for x in stations):
                viable.append(s)

        if not viable:
            viable = reachable
            logger.warning(f"No viable station at pos {current_pos:.1f} — using fallback.")

        best = min(viable, key=lambda s: s["avg_price"])
        leg_miles = best["dist_from_start"] - current_pos
        gallons = leg_miles / mpg
        cost = gallons * best["avg_price"]
        total_cost += cost

        fuel_stops.append({
            **best,
            "leg_miles": round(leg_miles, 1),
            "gallons_needed": round(gallons, 2),
            "leg_cost_usd": round(cost, 2),
        })
        current_pos = best["dist_from_start"]

    # Final leg cost (last stop → destination, using last stop's price)
    if fuel_stops:
        final_leg = total_route_miles - fuel_stops[-1]["dist_from_start"]
        final_cost = (final_leg / mpg) * fuel_stops[-1]["avg_price"]
        total_cost += final_cost
        fuel_stops[-1]["final_leg_miles"] = round(final_leg, 1)
        fuel_stops[-1]["final_leg_cost_usd"] = round(final_cost, 2)

    return {
        "fuel_stops": fuel_stops,
        "total_cost_usd": round(total_cost, 2),
    }