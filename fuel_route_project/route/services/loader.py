import csv
from pathlib import Path


def load_stations_from_csv(path):
    path = Path(path)
    stations = []
    with path.open(newline='', encoding='utf-8') as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            stations.append(row)
    return stations
