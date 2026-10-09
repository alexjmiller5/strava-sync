"""An in-memory soma hub and builders for Strava bulk-export zips and original files."""

import csv
import gzip
import io
import struct
import zipfile

from fitdecode.utils import compute_crc

# activities.csv as Strava writes it: Distance, Elapsed Time, Max Heart Rate, Relative
# Effort and Commute appear twice (display values first, raw units last).
HEADER = (
    "Activity ID,Activity Date,Activity Name,Activity Type,Activity Description,Elapsed Time,"
    "Distance,Max Heart Rate,Relative Effort,Commute,Activity Private Note,Activity Gear,"
    "Filename,Athlete Weight,Bike Weight,Elapsed Time,Moving Time,Distance,Max Speed,"
    "Average Speed,Elevation Gain,Elevation Loss,Elevation Low,Elevation High,Max Grade,"
    "Average Grade,Average Positive Grade,Average Negative Grade,Max Cadence,Average Cadence,"
    "Max Heart Rate,Average Heart Rate,Max Watts,Average Watts,Calories,Max Temperature,"
    "Average Temperature,Relative Effort,Total Work,Number of Runs,Uphill Time,Downhill Time,"
    "Other Time,Perceived Exertion,Type,Start Time,Weighted Average Power,Power Count,"
    "Prefer Perceived Exertion,Perceived Relative Effort,Commute,Total Weight Lifted,"
    "From Upload,Grade Adjusted Distance,Media"
).split(",")


def row(
    id=101,
    date="Oct 8, 2026, 11:00:00 AM",
    name="Morning Run",
    type="Run",
    description="",
    elapsed=1800,
    meters=5000.0,
    elevation=30.0,
    avg_hr=150.4,
    max_hr=171.0,
    calories=400.0,
    filename="activities/101.fit.gz",
    private_note="",
    gear="Shoes",
):
    """One activities.csv line; None leaves a cell blank like Strava does."""
    cells = [""] * len(HEADER)

    def put(name, value, occurrence=-1):
        at = [i for i, n in enumerate(HEADER) if n == name][occurrence]
        cells[at] = "" if value is None else str(value)

    put("Activity ID", id)
    put("Activity Date", date)
    put("Activity Name", name)
    put("Activity Type", type)
    put("Activity Description", description)
    put("Activity Private Note", private_note)
    put("Activity Gear", gear)
    put("Filename", filename)
    put("Elapsed Time", elapsed if elapsed is None else int(elapsed), 0)
    put("Elapsed Time", elapsed if elapsed is None else float(elapsed))
    put("Moving Time", elapsed)
    put("Distance", meters if meters is None else f"{meters / 1000:.2f}", 0)  # display km
    put("Distance", meters)
    put("Elevation Gain", elevation)
    put("Max Heart Rate", max_hr, 0)
    put("Max Heart Rate", max_hr)
    put("Average Heart Rate", avg_hr)
    put("Calories", calories)
    return cells


def export(path, rows, files=None, root="", when=(2026, 10, 9, 21, 30, 0), header=HEADER):
    """A Strava export zip: activities.csv plus original files under activities/."""
    text = io.StringIO()
    writer = csv.writer(text)
    writer.writerow(header)
    writer.writerows(rows)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo(f"{root}activities.csv", when), text.getvalue())
        z.writestr(zipfile.ZipInfo(f"{root}profile.csv", when), "Athlete ID\n1\n")
        z.writestr(zipfile.ZipInfo(f"{root}activities/", when), "")  # Strava's zips list it
        for name, data in (files or {}).items():
            z.writestr(zipfile.ZipInfo(f"{root}{name}", when), data)
    return path


FIT_EPOCH = 631065600  # 1989-12-31T00:00:00Z
T0 = 1791457200  # 2026-10-08T11:00:00Z


def points(n=3, hr=(140, 150, 160)):
    return [
        {
            "time": T0 + i,
            "lat": 42.36 + i / 10000,
            "lon": -71.06,
            "alt": 10.0 + i,
            "hr": hr[i] if i < len(hr) else None,
            "dist": i * 3.0,
        }
        for i in range(n)
    ]


def fit(pts, local_offset=None):
    """A minimal FIT activity: one record message per point, plus an activity message with
    the device's local time when `local_offset` (seconds east of UTC) is given."""

    def semi(deg):
        return 0x7FFFFFFF if deg is None else round(deg * 2**31 / 180)

    fields = [(253, 4, 0x86), (0, 4, 0x85), (1, 4, 0x85), (2, 2, 0x84), (3, 1, 0x02), (5, 4, 0x86)]
    body = bytearray(struct.pack("<BBBHB", 0x40, 0, 0, 20, len(fields)))
    for f in fields:
        body += struct.pack("BBB", *f)
    for p in pts:
        body += struct.pack(
            "<BIiiHBI",
            0,
            p["time"] - FIT_EPOCH,
            semi(p["lat"]),
            semi(p["lon"]),
            round((p["alt"] + 500) * 5),
            0xFF if p["hr"] is None else p["hr"],
            round(p["dist"] * 100),
        )
    if local_offset is not None:
        body += struct.pack("<BBBHB", 0x41, 0, 0, 34, 2)
        body += struct.pack("BBB", 253, 4, 0x86) + struct.pack("BBB", 5, 4, 0x86)
        t = pts[-1]["time"] - FIT_EPOCH
        body += struct.pack("<BII", 1, t, t + local_offset)
    header = struct.pack("<BBHI4s", 14, 0x20, 2132, len(body), b".FIT")
    header += struct.pack("<H", compute_crc(header))
    data = header + bytes(body)
    return data + struct.pack("<H", compute_crc(data))


def gz(data):
    return gzip.compress(data)


def gpx(pts):
    def iso(t):  # a point without a time has no <time> element
        from datetime import UTC, datetime

        return (
            "" if t is None else f"<time>{datetime.fromtimestamp(t, UTC):%Y-%m-%dT%H:%M:%SZ}</time>"
        )

    trkpts = "".join(
        f'<trkpt lat="{p["lat"]}" lon="{p["lon"]}"><ele>{p["alt"]}</ele>'
        f"{iso(p['time'])}<extensions><gpxtpx:TrackPointExtension>"
        f"<gpxtpx:hr>{p['hr']}</gpxtpx:hr><gpxtpx:cad>80</gpxtpx:cad>"
        "</gpxtpx:TrackPointExtension></extensions></trkpt>"
        for p in pts
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<gpx creator="StravaGPX" version="1.1" xmlns="http://www.topografix.com/GPX/1/1" '
        'xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">'
        f"<trk><name>Ride</name><trkseg>{trkpts}</trkseg></trk></gpx>"
    ).encode()


def tcx(pts):
    def iso(t):  # a trackpoint without a time has no <Time> element
        from datetime import UTC, datetime

        return (
            "" if t is None else f"<Time>{datetime.fromtimestamp(t, UTC):%Y-%m-%dT%H:%M:%SZ}</Time>"
        )

    tps = "".join(
        f"<Trackpoint>{iso(p['time'])}<Position>"
        f"<LatitudeDegrees>{p['lat']}</LatitudeDegrees>"
        f"<LongitudeDegrees>{p['lon']}</LongitudeDegrees></Position>"
        f"<AltitudeMeters>{p['alt']}</AltitudeMeters><DistanceMeters>{p['dist']}</DistanceMeters>"
        f"<HeartRateBpm><Value>{p['hr']}</Value></HeartRateBpm></Trackpoint>"
        for p in pts
    )
    # Strava's TCX originals often start with whitespace before the XML declaration.
    return (
        '          <?xml version="1.0" encoding="UTF-8"?>\n'
        '<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">'
        f'<Activities><Activity Sport="Running"><Lap><Track>{tps}</Track></Lap></Activity>'
        "</Activities></TrainingCenterDatabase>"
    ).encode()


class FakeHub:
    def __init__(self, rows=()):
        self.tables = {"cardio_workouts": {r["id"]: dict(r) for r in rows}, "provenance": {}}
        self.pushes = []
        self.files = {}
        self.types = {}
        self.records = []
        self.order = []

    def rows(self, table, columns, where):
        return [
            {c: r.get(c) for c in columns}
            for r in self.tables[table].values()
            if all(r.get(k) == v for k, v in where.items())
        ]

    def push(self, table, rows):
        assert table != "provenance", "the profile grants provenance inserts only"
        for row in rows:
            self.order.append(("push", row["id"]))
            self.pushes.append((table, dict(row)))
            self.tables[table].setdefault(row["id"], {"deleted_at": None}).update(row)

    def insert(self, table, rows):
        """Insert-only, like the hub's rows/insert; an edge needs a live target row."""
        assert table == "provenance"
        for row in rows:
            target = self.tables[row["to_kind"]].get(row["to_ref"])
            assert target and not target.get("deleted_at"), "edge target must be a live row"
            self.order.append(("edge", row["to_ref"]))
            if row["id"] not in self.tables[table]:  # an existing edge is no write at all
                self.pushes.append((table, dict(row)))
                self.tables[table][row["id"]] = dict(row)

    def put_file(self, key, data, content_type="application/octet-stream"):
        self.order.append(("file", key))
        self.files.setdefault(key, data)
        self.types.setdefault(key, content_type)

    def append(self, stream, record):
        self.records.append((stream, record))

    def cardio(self, row_id):
        return self.tables["cardio_workouts"][row_id]


RACE = {
    "id": "athlinks/turkey/1/2",
    "name": "Turkey Trot",
    "activity": "Running",
    "date": "2026-10-08",
    "recording_source": "Race Results",
    "external_id": None,
    "race_type": "Road",
    "race_distance_meters": 5000.0,
    "race_time_seconds": 1500.0,
    "race_timing_basis": "Chip",
    "race_result_id": "athlinks/turkey/1/2",
    "notes": "Bib 1.",
    "started_at": None,
    "deleted_at": None,
}
