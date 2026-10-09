"""In-memory stand-ins for Strava and the soma hub, shared by the sync tests."""

import json


def activity(id=101, **kw):
    base = {
        "id": id,
        "name": "Morning Run",
        "description": None,
        "sport_type": "Run",
        "type": "Run",
        "start_date": "2026-10-08T11:00:00Z",
        "start_date_local": "2026-10-08T07:00:00Z",
        "elapsed_time": 1800,
        "moving_time": 1700,
        "distance": 5000.0,
        "total_elevation_gain": 30.0,
        "has_heartrate": True,
        "average_heartrate": 150.4,
        "max_heartrate": 171.0,
        "calories": 400.0,
        "workout_type": 0,
    }
    return base | kw


STREAMS = {
    "time": {"data": [0, 1, 2], "series_type": "time", "original_size": 3, "resolution": "high"},
    "heartrate": {
        "data": [140, 150, 160],
        "series_type": "time",
        "original_size": 3,
        "resolution": "high",
    },
}


class FakeStrava:
    per_page = 2

    def __init__(self, activities=(), streams=None, laps=None):
        self.acts = {a["id"]: a for a in activities}
        self.stream_data = STREAMS if streams is None else streams
        self.lap_data = [{"lap_index": 1}] if laps is None else laps
        self.calls = []
        self.fail_after = None  # raise BudgetExhausted once this many calls were made

    def _call(self, *call):
        from core.strava import BudgetExhausted

        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise BudgetExhausted("daily read budget spent")
        self.calls.append(call)

    @staticmethod
    def _doc(obj):
        return obj, json.dumps(obj).encode()

    def activity(self, aid):
        self._call("activity", int(aid))
        a = self.acts.get(int(aid))
        return None if a is None else self._doc(a)

    def streams(self, aid):
        self._call("streams", int(aid))
        return self._doc(self.stream_data) if self.stream_data else None

    def laps(self, aid):
        self._call("laps", int(aid))
        return self._doc(self.lap_data)

    def activities(self, *, before=None, after=None, page=1):
        self._call("list", before, after, page)
        from core.sync import epoch

        acts = list(self.acts.values())
        if after is None:  # Strava lists newest first unless `after` is given
            acts = sorted(
                (a for a in acts if before is None or epoch(a["start_date"]) < before),
                key=lambda a: a["start_date"],
                reverse=True,
            )
        else:
            acts = sorted(
                (a for a in acts if epoch(a["start_date"]) > after),
                key=lambda a: a["start_date"],
            )
        start = (page - 1) * self.per_page
        return acts[start : start + self.per_page]


class FakeHub:
    def __init__(self, rows=()):
        self.tables = {"cardio_workouts": {r["id"]: dict(r) for r in rows}, "provenance": {}}
        self.pushes = []
        self.files = {}
        self.records = []

    def rows(self, table, columns, where):
        return [
            {c: r.get(c) for c in columns}
            for r in self.tables[table].values()
            if all(r.get(k) == v for k, v in where.items())
        ]

    def push(self, table, rows):
        for row in rows:
            self.pushes.append((table, dict(row)))
            self.tables[table].setdefault(row["id"], {"deleted_at": None}).update(row)

    def put_file(self, key, data):
        self.files.setdefault(key, data)

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
