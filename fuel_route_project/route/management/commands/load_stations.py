"""
route/management/commands/load_stations.py

Loads fuel stations from the original fuel_prices.csv plus the pre-geocoded
city coordinates produced by Geocodio (split into part1 and part2 CSVs).

Design decisions documented here so they are easy to explain in the Loom:

1. DUPLICATE OPIS IDs (same station, multiple price rows)
   → Average the Retail Price across all rows for that OPIS ID.
   Rationale: No timestamps in the dataset, so we cannot take "most recent".
   The mean gives a stable, representative price. This matches the assessment brief.

2. MULTIPLE STATIONS IN THE SAME CITY
   → Keep every station as its OWN row in FuelStation, all sharing the
   city-centre lat/lon provided by Geocodio.
   Rationale: Collapsing to one price per city would throw away real price
   variation between stations (e.g. Flying J vs Pilot in the same town can
   differ by $0.10/gal). The greedy optimizer picks cheapest per stop, so
   having fine-grained prices strictly improves results.
   The precision trade-off (city-centre coords, not exact exit) is acceptable
   given the 5-mile proximity filter and 500-mile tank range.

3. GEOCODIO GAVE ONLY CITY-CENTRE COORDS
   → Stored as-is. Multiple stations in the same city share the same lat/lon.
   This is documented in FuelStation.Meta and in the README.

Usage:
    python manage.py load_stations \
        --csv fuel_prices.csv \
        --geo1 geocoded_part1.csv \
        --geo2 geocoded_part2.csv
"""

import csv
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError

from route.models import FuelStation


class Command(BaseCommand):
    help = "Load fuel stations from fuel_prices CSV + Geocodio geocoded CSVs"

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv",
            required=True,
            help="Path to fuel-prices-for-be-assessment.csv",
        )
        parser.add_argument(
            "--geo1",
            required=True,
            help="Path to geocoded_part1.csv (City,State,Latitude,Longitude)",
        )
        parser.add_argument(
            "--geo2",
            required=True,
            help="Path to geocoded_part2.csv (City,State,Latitude,Longitude)",
        )

    def handle(self, *args, **options):
        # ------------------------------------------------------------------ #
        # Step 1 — Build geocode lookup: (city, state) -> (lat, lon)
        # ------------------------------------------------------------------ #
        geo_lookup: dict[tuple[str, str], tuple[float, float]] = {}

        for geo_path in (options["geo1"], options["geo2"]):
            with open(geo_path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    city = row["City"].strip()
                    state = row["State"].strip()
                    try:
                        lat = float(row["Latitude"])
                        lon = float(row["Longitude"])
                    except (ValueError, KeyError):
                        self.stdout.write(
                            self.style.WARNING(
                                f"  Bad geocode row skipped: {row}"
                            )
                        )
                        continue
                    geo_lookup[(city, state)] = (lat, lon)

        self.stdout.write(f"Geocode lookup built: {len(geo_lookup)} city+state pairs")

        # ------------------------------------------------------------------ #
        # Step 2 — Parse fuel_prices CSV
        #          Group by OPIS ID, average prices, keep first occurrence of
        #          name/address/city/state for each station.
        # ------------------------------------------------------------------ #
        id_prices: dict[int, list[float]] = defaultdict(list)
        id_meta: dict[int, dict] = {}  # first-seen metadata per OPIS ID

        with open(options["csv"], newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    opis_id = int(row["OPIS Truckstop ID"])
                    price = float(row["Retail Price"])
                except (ValueError, KeyError) as exc:
                    self.stdout.write(
                        self.style.WARNING(f"  Skipping bad row: {exc} — {row}")
                    )
                    continue

                id_prices[opis_id].append(price)

                if opis_id not in id_meta:
                    id_meta[opis_id] = {
                        "name": row.get("Truckstop Name", "").strip(),
                        "address": row.get("Address", "").strip(),
                        "city": row.get("City", "").strip(),
                        "state": row.get("State", "").strip(),
                    }

        self.stdout.write(
            f"Unique OPIS IDs in CSV: {len(id_meta)}  "
            f"(from {sum(len(v) for v in id_prices.values())} rows)"
        )

        # ------------------------------------------------------------------ #
        # Step 3 — Join with geocode lookup and save to DB
        # ------------------------------------------------------------------ #
        FuelStation.objects.all().delete()
        self.stdout.write("Cleared existing FuelStation rows.")

        created = 0
        skipped_no_geo = 0
        to_create = []

        for opis_id, prices in id_prices.items():
            meta = id_meta[opis_id]
            city = meta["city"]
            state = meta["state"]

            coords = geo_lookup.get((city, state))
            if coords is None:
                # Try case-insensitive fallback
                coords = geo_lookup.get((city.lower(), state.lower()))

            if coords is None:
                skipped_no_geo += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"  No geocode for '{city}, {state}' — station {opis_id} skipped"
                    )
                )
                continue

            lat, lon = coords
            avg_price = sum(prices) / len(prices)

            to_create.append(
                FuelStation(
                    opis_id=opis_id,
                    name=meta["name"],
                    address=meta["address"],
                    city=city,
                    state=state,
                    lat=lat,
                    lon=lon,
                    avg_price=round(avg_price, 6),
                    price_sample_count=len(prices),
                )
            )

        # Bulk insert for speed
        FuelStation.objects.bulk_create(to_create, batch_size=500)
        created = len(to_create)

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone. {created} stations loaded, {skipped_no_geo} skipped (no geocode)."
            )
        )
        self.stdout.write(
            f"Stations with multiple price samples: "
            f"{sum(1 for p in id_prices.values() if len(p) > 1)}"
        )