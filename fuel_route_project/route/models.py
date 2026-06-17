from django.db import models


class FuelStation(models.Model):
    """
    One row per unique OPIS Truckstop ID.

    The raw CSV has multiple rows per station (price collected at
    different times) -- those are collapsed into a single row here,
    with avg_price = mean(Retail Price) across all snapshots for
    that station ID, per the assessment's explicit instruction
    (no timestamps available, so average rather than "most recent").

    lat/lon are city-level (geocoded from City+State, not the street
    address), so multiple stations in the same city will share
    identical coordinates. This is the precision tradeoff documented
    in the README -- acceptable given the 500-mile tank range and the
    5-mile route-proximity filter used downstream.
    """
    opis_id = models.IntegerField(unique=True, db_index=True)
    name = models.CharField(max_length=200)
    address = models.CharField(max_length=300, blank=True, default="")
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2, db_index=True)
    lat = models.FloatField()
    lon = models.FloatField()
    avg_price = models.FloatField()
    price_sample_count = models.IntegerField(default=1)  # how many raw rows were averaged

    class Meta:
        indexes = [
            models.Index(fields=["lat", "lon"]),
            models.Index(fields=["avg_price"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.city}, {self.state}) - ${self.avg_price:.3f}"
