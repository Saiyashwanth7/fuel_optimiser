# Fuel Route Optimizer

A backend service that takes a start and finish location in the US and figures out the cheapest way to fuel up along the route, given a truck with a 500 mile tank range and 10 miles per gallon fuel efficiency.

Built for the Spotter AI Backend Django Engineer assessment.

## Live Demo

The API is deployed and live on Railway:

**Base URL:** `https://fueloptimiser-production.up.railway.app`

This above link has a simple frontend to let users enter the locations for testing, also has a view raw backend button where the actual backend JSON response can be viewed.

### Try it now

**Chicago to Dallas:**
`https://fueloptimiser-production.up.railway.app/api/?start=Chicago,+IL&finish=Dallas,+TX`
_start:_ Chicago,IL _Finish:_ Dallas,TX

**New York to Los Angeles:**
`https://fueloptimiser-production.up.railway.app/api/?start=New+York,+NY&finish=Los+Angeles,+CA`
_start:_ New York,NY _Finish:_ Los Angeles,CA

**Short trip under 500 miles (no fuel stop needed):**
`https://fueloptimiser-production.up.railway.app/api/?start=Chicago,+IL&finish=Indianapolis,+IN`
_start:_ Chicago,IL _Finish:_ Indianapolis,+IN

## What it does

You give it two locations, say Chicago, IL and Dallas, TX, and it returns:

- The total driving distance
- Where exactly to stop for fuel along the way
- How much fuel to buy at each stop
- The total cost of fuel for the whole trip

The goal isn't to minimize the number of stops, it's to minimize total cost. Sometimes that means stopping at more places to catch cheaper prices instead of just topping up at the nearest station every 500 miles.

## How it works, in plain terms

There are three real stages to every request.

**Stage 1, figure out where the start and finish actually are.** Since the dataset only has city and state names for the fuel stations, I check three places in order: an in-memory cache (in case this exact location was asked before in this session), my own database of already-geocoded cities (since I loaded 8000+ stations with coordinates already), and only as a last resort, a live geocoding API call. Most requests never need that last step.

**Stage 2, get the actual road.** I call OpenRouteService exactly once per request. This is intentional, the assessment specifically asks to keep routing API calls to a minimum, ideally just one. That single call gives me the full route shape (as a list of coordinates) and the total distance. Everything after this point happens locally, no more external calls.

**Stage 3, decide where to stop and how much to spend.** This is the actual algorithm, explained below.

## The algorithm

### Finding stations near the route

I have around 8000 real fuel stations in the database, but most of them are nowhere near any given route. Before doing anything expensive, I run a quick bounding box query in Postgres to narrow things down to a few hundred stations that are roughly in the right geographic area.

From there, I check each station's actual distance to the route. The route itself comes back from ORS as a polyline, basically a long list of coordinate points tracing the literal road. For a long trip like Chicago to Dallas, this can be 5000+ points.

Checking a few hundred stations against 5000+ points one at a time in a Python loop is slow, my first version of this took around 44 seconds for that exact trip. So the current version does two things differently:

1. It downsamples the route points for an initial rough check. Since consecutive points on the route are extremely close together, you don't need to check every single one to know if a station is in the right neighborhood. I sample points spaced no more than half my proximity threshold apart, which still guarantees no real candidate gets missed.
2. It uses NumPy to check a station against all sampled points in one vectorized operation, instead of looping through them one at a time in plain Python.

So for every station, there's a fast rough pass first. If even the closest sampled point is too far away, the station gets thrown out immediately, no further work done on it. Only the handful of stations that pass this rough check get the full, precise treatment, checked against every single point on the actual route to get an exact distance and exact position along the trip.

This brought the same query down to around 11 seconds, roughly a 4x improvement. The earlier, simpler version is still in the repo as `optimizer.py` for comparison, the live code uses `optimizer_v2.py`.

### Picking where to actually stop

Once I know which stations are near the route and exactly where along the trip each one sits, a separate function decides the actual stops.

The truck starts with a full tank, good for 500 miles. At each point, I look at every station reachable within the next 500 miles. Among those, I don't just grab the closest one, I grab the cheapest one. But there's a catch, a cheap station only counts if the truck can actually continue its journey from there too, either by reaching the destination directly, or by reaching another station further ahead. I call these "viable" stations, and only among viable ones do I pick the cheapest.

This repeats until the remaining distance to the destination fits inside one tank, and the trip just finishes from there.

If a stretch of the route genuinely has no reachable station at all, the API returns a clear error rather than failing silently or giving a wrong answer.

## An experiment that didn't make it to production: `optimizer_v3.py`

While building this, I noticed the greedy algorithm sometimes recommends more stops than feel necessary, since it always grabs the cheapest reachable station even when that station is only marginally cheaper than one further along. There's a real tradeoff here: more stops means more time off the road, which has its own cost that the assessment doesn't ask me to model but that a real trucking company would care about.

So I tried a different approach in `optimizer_v3.py`: instead of greedy selection, use dynamic programming to find the truly globally optimal path, where each potential stop is a node, each reachable pair of nodes is an edge weighted by fuel cost, and a fixed `stop_penalty` (e.g. $2) is added to every edge to discourage taking more stops than needed. This turns stop selection into a shortest-path problem, solved with an O(n²) DP over stations sorted by distance.

This version is **not used in production** (the live API runs `optimizer_v2.py`), for one concrete reason I found while testing it: the penalty leaks into the reported total in a way that breaks the response. The DP's `total_cost_usd` is `real fuel cost + (stop_penalty × number of stops)`, but each individual stop's `leg_cost_usd` in the response only reflects fuel, not the penalty. So if you sum the itemized `leg_cost_usd` values across all stops, you get a smaller number than `total_cost_usd` reports — the two don't reconcile, and nothing in the response explains why. For an assessment that explicitly asks for "total money spent on fuel," returning a number padded with an invented per-stop fee, with no field disclosing that, felt like the wrong tradeoff to ship under time pressure.

The right fix is to track real fuel cost separately from the DP's penalized score (e.g. a parallel `min_fuel_cost[]` array updated alongside the DP's `min_cost[]`, returning that instead) — but that's a non-trivial change, not a one-line patch, so I'm leaving it as a documented experiment rather than rushing it into the live path. If I were to continue working on this, this is the first thing I'd fix, followed by tuning the actual penalty value against real route data to see how much it changes stop counts in practice.

## A real, honest limitation

The fuel station addresses in the source data are highway exit descriptions, things like "I-44, exit 283", not actual mailing addresses. Geocoding services can't pin those down precisely. So I geocoded every station at the city level, using city and state only.

This means multiple stations in the same town share identical coordinates, and there's a small margin of error between where a station is geocoded to sit and where it physically is. I accounted for this with a wider proximity threshold than I'd otherwise use, instead of something tight like 5 miles, the threshold is more forgiving to absorb that uncertainty.

If precise, address-level coordinates were available for each station, this margin of error would mostly disappear, and the proximity threshold could be tightened significantly for even more accurate stop selection.

## Project structure

```
route/
  models.py                    FuelStation model, one row per unique station
  views.py                     The single API endpoint
  serializers.py                DRF serializer for the model
  services/
    routing.py                  ORS API wrapper, one call per request
    geocoding.py                 Nominatim fallback for start/finish lookup
    optimizer.py                  Original nested-loop version (v1)
    optimizer_v2.py                Current/live version, vectorized + downsampled
    optimizer_v3.py                 Experimental DP + stop-penalty version, not in production (see above)
  management/commands/
    load_stations.py              One-time command to load and geocode station data
```

## Running it locally

1. Clone the repo and install dependencies:

   ```
   pip install -r requirements.txt
   ```

2. Set up a `.env` file with your database credentials and API keys:

   ```
   DATABASE_NAME=your_db_name
   DATABASE_USER=your_db_user
   DATABASE_PASSWORD=your_db_password
   DATABASE_HOST=localhost
   DATABASE_PORT=5432
   ORS_API_KEY=your_openrouteservice_key
   DJANGO_SECRET_KEY=any_random_string
   ```

3. Run migrations:

   ```
   python manage.py migrate
   ```

4. Load the station data (you'll need the original CSV and the two geocoded CSVs):

   ```
   python manage.py load_stations --csv fuel-prices-for-be-assessment.csv --geo1 geocoded_part1.csv --geo2 geocoded_part2.csv
   ```

5. Start the server:

   ```
   python manage.py runserver
   ```

6. Hit the endpoint:
   ```
   GET /api/route/?start=Chicago, IL&finish=Dallas, TX
   ```

## A note on the data

Some city/state pairs in the dataset, mostly a handful of Canadian towns mixed into the US data, couldn't be geocoded successfully. These are excluded from the station database. It's a tiny fraction of the total dataset and doesn't meaningfully affect coverage on US routes.

Multiple price entries exist for the same station ID in the raw CSV with no timestamps to tell which is most recent, so I averaged them. This felt like the more honest choice than arbitrarily picking one.

## Video Explanation:

loom video: https://www.loom.com/share/78f49079767d46e8a38017e9266b04dd
