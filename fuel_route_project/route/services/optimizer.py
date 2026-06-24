"""
route/services/optimizer.py

Core fuel route optimization logic.

Three responsibilities:
  1. haversine()                — GPS distance in miles
  2. filter_stations_near_route() — narrow 6700 stations to ~200-500 near the route
  3. project_to_route_distance()  — convert GPS position to miles-from-start
  4. greedy_fuel_optimizer()      — pick cheapest stops within tank range

Algorithm overview (explain this in the Loom):
  - Sort candidate stations by their distance from the route start.
  - From current position, find all stations reachable within 500 miles.
  - Among those, keep only "viable" ones: stations from which the next stop
    (or the destination) is also reachable within 500 miles.
  - Pick the cheapest viable station. Refuel there. Repeat.
  - Add final leg cost (last stop → destination).

Greedy is not globally optimal but is fast (O(n²) worst case, sub-second in
practice) and produces routes within a few percent of optimal for highway
driving where stations are plentiful.
"""

import math
import logging
from django.conf import settings

logger = logging.getLogger(__name__)

TANK_RANGE: float = getattr(settings, "VEHICLE_TANK_RANGE_MILES", 500)
MPG: float = getattr(settings, "VEHICLE_MPG", 10)
PROXIMITY_MILES: float = getattr(settings, "STATION_ROUTE_PROXIMITY_MILES", 20)


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #


# We use haversine distance to find the distance between the start lat,lng and finish lat, lng
# The harvesine distance formula is
# 2*R*(sine inverse(sqrt(sin(lat2-lat1)**2 + (cos(lat1)*cos(lat2)*(sin(lng2-lng1)**2)))
def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3958.8  # Earth radius in miles

    # converting degrees into radians
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)

    # the core part of haversine fomrula, the thing we use in the sine inverse part
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    # according to the haversine formula, a value will be 0≤a≤1, if a gets bigger than 1 due to computation mistake, we round it up to 1
    # here, R is the radius, the other part is the angle, distance (in miles) = raidus * angle
    return R * 2 * math.asin(math.sqrt(min(a, 1.0)))  # clamp for float rounding


# Using this function we are going to filter the stations near the polyine provided by the ORS API.
# this takes 3 parameters, stations: this are the DB rows. We are going to execute this filtering for all the rows
# Here, the stations are already filtered out in the views.py, instead of performing on all 3898 unique city and state coordinates, we only use the stations coordinates near in 35 miles radius on the polyline. So, the total stations, might come down to around 300-500 stations.
# polyline; this is the string we get upon calling the ORS API, after decoding this we get a nested list, l[i][0]->longitube,l[i][1]->latitude
# theshold_miles; We need a maximum distance between the polyline and the station. Like we consider all stations with in the radius of 15-20 miles of the polyline, because we are using the city-centre coordinates instead of the actual accurate coordinates. If we use the accurate coordinates, we can just use 5 miles are the threshold_miles. If we get the accurate coordinates, we can set 5 miles in the 33rd line
# This return a list of dict for the optimizer, not the ORM objects.
def filter_stations_near_route(
    stations,  # QuerySet or list of FuelStation ORM objects
    polyline: list[list[float]],  # [[lon, lat], ...]
    threshold_miles: float = PROXIMITY_MILES,
) -> list[dict]:
    """
    Return stations within threshold_miles of ANY point on the polyline.

    Returns a list of dicts (not ORM objects) ready for the optimizer.
    The dict includes 'dist_from_start' = miles along the route to the
    nearest polyline point.

    Why iterate polyline points rather than segments?
    ORS returns ~1 point per ~30m for highways — dense enough that point
    distance is within a few hundred metres of segment distance. Exact
    segment projection would add complexity for negligible accuracy gain
    given our 5-mile threshold.
    """
    # Pre-compute cumulative distances along the polyline once.
    # cum_dist[i] = total miles from polyline[0] to polyline[i]
    cum_dist = [0.0]
    for i in range(1, len(polyline)):
        p1, p2 = polyline[i - 1], polyline[i]
        seg = haversine(p1[1], p1[0], p2[1], p2[0])
        cum_dist.append(cum_dist[-1] + seg)

    nearby = []
    # Using a nested for loop to derive the distance between a station and each breadcrumb in the polyline.
    # This is the part which is very slow. Need to figure out a faster way to filter out the stations. Maybe, need to take lesser points from polyline. Each case has thousands of polyline points, need to work on this as well. Maybe, we can use numpy for faster calculations by vectorizing each poin the polyline and filtering on it
    for station in stations:
        slat, slon = station.lat, station.lon
        best_dist = float("inf")
        best_route_miles = 0.0

        for i, point in enumerate(polyline):
            d = haversine(slat, slon, point[1], point[0])
            if d < best_dist:
                best_dist = d
                best_route_miles = cum_dist[i]

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
                    "dist_from_start": best_route_miles,
                }
            )

    logger.info(
        f"Proximity filter: {len(nearby)} stations within {threshold_miles} miles "
        f"of route (from {len(list(stations))} candidates)"
    )
    return nearby


# --------------------------------------------------------------------------- #
# Greedy optimizer
# --------------------------------------------------------------------------- #


# This function in the actual core greedy algorithm. It takes the filtered stations (stations_on_route) as the parameter along with the total_route_miles, and tangk_range, mpg
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
