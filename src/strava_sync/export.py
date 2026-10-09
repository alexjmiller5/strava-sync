"""Strava's bulk export ("Download your account" zip): activities.csv plus original uploads.

activities.csv repeats some column names: the first Distance / Elapsed Time / Max Heart
Rate are display values (km, rounded), the last ones raw units (meters, seconds). A row
read into a dict keeps the LAST of a repeated name, so every number here is in raw units.
Activity Date is UTC. Original files (FIT/GPX/TCX, often gzipped) carry the samples.
"""

import csv
import gzip
import io
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime

import fitdecode
import gpxpy

DATE_FORMAT = "%b %d, %Y, %I:%M:%S %p"  # "Oct 8, 2026, 11:00:00 AM"
REPEATED = ("Distance", "Elapsed Time")  # display first, raw units last
SEMICIRCLE = 180 / 2**31
SERIES = ("altitude", "heartrate", "distance", "cadence", "watts", "temp")


class ExportError(ValueError):
    pass


def _number(text):
    if text is None or not text.strip():
        return None
    value = float(text)
    return int(value) if value.is_integer() else value


def _float(text):
    return None if text is None or not text.strip() else float(text)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class Export:
    """An open export zip. `date` is the day Strava generated it (the csv's zip timestamp)."""

    def __init__(self, path):
        self.zip = zipfile.ZipFile(path)
        found = [n for n in self.zip.namelist() if n.rsplit("/", 1)[-1] == "activities.csv"]
        if len(found) != 1:
            self.zip.close()
            raise ExportError(f"{path}: no single activities.csv - not a Strava bulk export")
        self.root = found[0].removesuffix("activities.csv")
        self.csv = self.zip.read(found[0])
        self.date = "%04d-%02d-%02d" % self.zip.getinfo(found[0]).date_time[:3]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.zip.close()

    def originals(self) -> list[str]:
        """Every original upload, as a path relative to the export root."""
        folder = f"{self.root}activities/"
        return [
            n.removeprefix(self.root)
            for n in self.zip.namelist()
            if n.startswith(folder) and not n.endswith("/")
        ]

    def read(self, name: str) -> bytes:
        return self.zip.read(self.root + name)

    def activities(self) -> list[dict]:
        reader = csv.reader(io.StringIO(self.csv.decode("utf-8-sig")))
        header = next(reader)
        if any(header.count(name) != 2 for name in REPEATED):
            raise ExportError(
                "activities.csv layout changed: expected display and raw-unit columns for "
                + " and ".join(REPEATED)
            )
        out = []
        for values in reader:
            r = dict(zip(header, values))  # the last of a repeated name wins: raw units
            out.append(
                {
                    "id": r["Activity ID"],
                    "name": r["Activity Name"],
                    "description": r["Activity Description"],
                    "private_note": r.get("Activity Private Note", ""),
                    "type": r["Activity Type"],
                    "start": datetime.strptime(r["Activity Date"], DATE_FORMAT).replace(tzinfo=UTC),
                    "elapsed_time": _float(r["Elapsed Time"]),
                    "distance": _float(r["Distance"]),
                    "total_elevation_gain": _float(r.get("Elevation Gain")),
                    "average_heartrate": _float(r.get("Average Heart Rate")),
                    "max_heartrate": _float(r.get("Max Heart Rate")),
                    "calories": _float(r.get("Calories")),
                    "filename": r.get("Filename") or None,
                }
            )
        return out


@dataclass
class Track:
    """Samples from one original file. `time` is unix seconds; `utc_offset` (seconds east
    of UTC) is known only when the file records the device's local time (FIT)."""

    points: list[dict]
    utc_offset: int | None = None

    def series(self) -> dict[str, list]:
        """Arrays aligned with `time`, keyed like Strava's streams; empty series are dropped."""
        pts = self.points  # every parser keeps only timed points
        out = {"time": [p["time"] for p in pts]}
        if any(p.get("lat") is not None for p in pts):
            out["latlng"] = [
                [p["lat"], p["lon"]] if p.get("lat") is not None else None for p in pts
            ]
        for key in SERIES:
            values = [p.get(key) for p in pts]
            if any(v is not None for v in values):
                out[key] = values
        return out if pts else {}


def _fit(data: bytes) -> Track:
    points, offset = [], None
    with fitdecode.FitReader(io.BytesIO(data)) as fit:
        for frame in fit:
            if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                continue

            def get(field):
                return frame.get_value(field, fallback=None)

            if frame.name == "record" and get("timestamp"):
                lat, lon = get("position_lat"), get("position_long")
                altitude = get("enhanced_altitude")
                points.append(
                    {
                        "time": int(get("timestamp").timestamp()),
                        "lat": None if lat is None else round(lat * SEMICIRCLE, 7),
                        "lon": None if lon is None else round(lon * SEMICIRCLE, 7),
                        "altitude": get("altitude") if altitude is None else altitude,
                        "heartrate": get("heart_rate"),
                        "distance": get("distance"),
                        "cadence": get("cadence"),
                        "watts": get("power"),
                        "temp": get("temperature"),
                    }
                )
            elif frame.name == "activity" and get("local_timestamp") and get("timestamp"):
                seconds = (get("local_timestamp") - get("timestamp")).total_seconds()
                offset = round(seconds / 900) * 900  # time zones move in quarter hours
    return Track(points, offset)


def _gpx(data: bytes) -> Track:
    doc = gpxpy.parse(data.decode("utf-8-sig").lstrip())
    points = []
    for p in (p for t in doc.tracks for s in t.segments for p in s.points):
        if p.time is None:
            continue
        ext = {_local(e.tag): e.text for x in p.extensions for e in x.iter()}
        points.append(
            {
                "time": int(p.time.timestamp()),
                "lat": p.latitude,
                "lon": p.longitude,
                "altitude": p.elevation,
                "heartrate": _number(ext.get("hr")),
                "cadence": _number(ext.get("cad")),
                "watts": _number(ext.get("power")),
                "temp": _number(ext.get("atemp")),
            }
        )
    return Track(points)


def _tcx(data: bytes) -> Track:
    root = ET.fromstring(data.lstrip())  # Strava's TCX originals often lead with whitespace
    points = []
    for tp in (e for e in root.iter() if _local(e.tag) == "Trackpoint"):
        f = {_local(e.tag): e.text for e in tp.iter()}  # HeartRateBpm's child is "Value"
        if not f.get("Time"):
            continue
        points.append(
            {
                "time": int(datetime.fromisoformat(f["Time"].replace("Z", "+00:00")).timestamp()),
                "lat": _number(f.get("LatitudeDegrees")),
                "lon": _number(f.get("LongitudeDegrees")),
                "altitude": _number(f.get("AltitudeMeters")),
                "distance": _number(f.get("DistanceMeters")),
                "heartrate": _number(f.get("Value")),
                "cadence": _number(f.get("Cadence") or f.get("RunCadence")),
                "watts": _number(f.get("Watts")),
            }
        )
    return Track(points)


PARSERS = {"fit": _fit, "gpx": _gpx, "tcx": _tcx}


def track(name: str, data: bytes) -> Track | None:
    """Samples of an original upload; None for a file type that carries none."""
    if name.endswith(".gz"):
        data, name = gzip.decompress(data), name.removesuffix(".gz")
    parse = PARSERS.get(name.rsplit(".", 1)[-1].lower())
    return parse(data) if parse else None
