"""A Strava bulk export -> soma cardio_workouts rows, retained originals and samples.

The table contract (soma catalog, cardio_workouts): one row per real
activity; a Strava recording of a race enriches the race row instead of
duplicating it; official race fields and known values are never overwritten
or erased; pace is a query, never stored; point samples go to a producer
stream keyed by the row id; raw originals go to the retained file service.
"""

import json
import math
import re
from datetime import datetime, timedelta

import structlog

from strava_sync.export import Export, ExportError, track
from strava_sync.hub import stamp

TABLE = "cardio_workouts"
STREAM = "cardio_strava"
EXPORTS = "raw/strava/export/"  # every retained export: <date>/activities.csv + originals
ASSERTED_BY = "script:strava-sync"
MAX_RECORD_BYTES = 900_000  # the hub refuses stream appends over 1 MB
EDGE_CHUNK = 200
OUTCOMES = ("inserted", "enriched", "updated", "unchanged", "skipped")
RACE_TOLERANCE = 0.15  # GPS distance within 15% of the course confirms a race match
METERS_PER_MILE = 1609.344

# Strava activity types with spaces and hyphens removed ("E-Bike Ride" -> "EBikeRide").
ACTIVITY = {
    "Run": "Running",
    "TrailRun": "Running",
    "VirtualRun": "Running",
    "Ride": "Biking",
    "MountainBikeRide": "Biking",
    "GravelRide": "Biking",
    "EBikeRide": "Biking",
    "EMountainBikeRide": "Biking",
    "VirtualRide": "Biking",
    "Walk": "Walking",
    "Hike": "Hiking",
    "Swim": "Swimming",
    "Rowing": "Rowing",
    "VirtualRow": "Rowing",
    "Elliptical": "Elliptical",
    "StairStepper": "Stairmaster",
}
LABEL = {
    "Running": "Run",
    "Biking": "Bike",
    "Walking": "Walk",
    "Hiking": "Hike",
    "Swimming": "Swim",
    "Rowing": "Row",
    "Elliptical": "Elliptical",
    "Stairmaster": "Stairmaster",
}
# What a recording may add to a race row; name, date, notes and race_* stay official.
DEVICE = (
    "recording_source",
    "external_id",
    "started_at",
    "ended_at",
    "duration_minutes",
    "distance_miles",
    "elevation_gain_feet",
    "average_heart_rate",
    "maximum_heart_rate",
    "calories",
)
COLUMNS = [
    "id",
    "deleted_at",
    "name",
    "activity",
    "date",
    "notes",
    "needs_review",
    "race_type",
    "race_distance_meters",
    *DEVICE,
]

log = structlog.get_logger()


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def row_id(activity_id) -> str:
    return f"strava/activity/{activity_id}"


def local_date(start: datetime, utc_offset: int | None, zone) -> str:
    """The activity's local day: the file's own UTC offset (FIT) when it records one, else
    the importer's time zone (None = this machine's)."""
    # ponytail: GPX/TCX/manual activities abroad get the importer's zone; a lat/lon time zone
    # lookup would fix their dates if that ever matters.
    if utc_offset is not None:
        return (start + timedelta(seconds=utc_offset)).date().isoformat()
    return start.astimezone(zone).date().isoformat()


def _span(minutes: float) -> str:
    m = round(minutes)
    return f"{m}min" if m < 60 else f"{m // 60}h {m % 60}min"


def to_row(a: dict, date: str, series: dict) -> dict:
    """The row values an export activity supports; absent facts are left out, never null."""
    sport = a["type"] or "Activity"
    activity = ACTIVITY.get(re.sub(r"[^A-Za-z]", "", sport), "Other")
    elapsed = a["elapsed_time"] or 0
    meters = a["distance"] or 0
    climb = a["total_elevation_gain"] or 0
    beats = [h for h in series.get("heartrate", []) if h]  # fills a CSV without heart rate
    average = a["average_heartrate"] or (sum(beats) / len(beats) if beats else None)
    peak = a["max_heartrate"] or (max(beats) if beats else None)
    span = _span(elapsed / 60)
    label = LABEL.get(activity, sport)
    # Title, description, private note: where incomplete-recording notes live.
    text = (a["name"], a["description"], a["private_note"])
    row = {
        "name": f"{label} - {meters / METERS_PER_MILE:.1f}mi in {span}"
        if meters
        else f"{label} - {span}",
        "activity": activity,
        "date": date,
        "started_at": iso(a["start"]),
        "ended_at": iso(a["start"] + timedelta(seconds=elapsed)) if elapsed > 0 else None,
        "duration_minutes": round(elapsed / 60, 2) if elapsed > 0 else None,
        "distance_miles": round(meters / METERS_PER_MILE, 3) if meters > 0 else None,
        "elevation_gain_feet": round(climb * 3.28084, 1) if climb > 0 else None,
        "average_heart_rate": round(average) if average else None,
        "maximum_heart_rate": round(peak) if peak else None,
        "calories": round(a["calories"]) if (a["calories"] or 0) > 0 else None,
        "recording_source": "Strava",
        "external_id": str(a["id"]),
        "notes": "\n\n".join(t.strip() for t in text if t and t.strip()) or None,
    }
    return {k: v for k, v in row.items() if v is not None}


def records(base: dict, data: list) -> list[dict]:
    """One stream record per activity + sample key, split only when over the append limit."""
    size = len(json.dumps(base | {"data": data}))
    if size <= MAX_RECORD_BYTES:
        return [base | {"data": data}]
    step = math.ceil(len(data) / math.ceil(size / MAX_RECORD_BYTES))
    offsets = range(0, len(data), step)
    return [
        base | {"offset": i, "parts": len(offsets), "data": data[i : i + step]} for i in offsets
    ]


class Sync:
    def __init__(self, hub, zone=None, now=stamp):
        self.hub, self.zone, self.now = hub, zone, now
        self.known = None

    def _rows(self) -> dict:
        """Every cardio row, pulled once and kept current as this import writes."""
        if self.known is None:
            self.known = {r["id"]: r for r in self.hub.rows(TABLE, COLUMNS, {})}
        return self.known

    def _push(self, target_id: str, changes: dict) -> None:
        self.hub.push(TABLE, [{"id": target_id, **changes}])
        blank = dict.fromkeys(COLUMNS) | {"id": target_id}
        self._rows().setdefault(target_id, blank).update(changes)

    def _by_external(self, activity_id) -> list[dict]:
        return [
            r
            for r in self._rows().values()
            if r["recording_source"] == "Strava" and r["external_id"] == str(activity_id)
        ]

    def run(self, export: Export, prune: bool = False) -> dict:
        """Import one export: originals first, then rows, then their origin edges."""
        listed = export.activities()
        if prune and not listed:
            raise ExportError("refusing to prune with an empty export")
        prefix = f"{EXPORTS}{export.date}/"
        originals = export.originals()
        self.hub.put_file(f"{prefix}activities.csv", export.csv, "text/csv")
        for name in originals:
            self.hub.put_file(prefix + name, export.read(name))
        counts, samples, deleted, edges = dict.fromkeys(OUTCOMES, 0), 0, 0, []
        for n, a in enumerate(listed, 1):
            outcome, target_id, appended = self.activity(a, export, prefix)
            counts[outcome] += 1
            samples += appended
            if target_id:
                edges.append(self._edge(a["id"], target_id))
            if n % 50 == 0:
                log.info("strava.progress", done=n, of=len(listed))
        # After their rows (an edge needs a live target) and on every import: the insert
        # never touches an existing edge, so a crash in between heals on the next run.
        for i in range(0, len(edges), EDGE_CHUNK):
            self.hub.insert("provenance", edges[i : i + EDGE_CHUNK])
        seen = {a["id"] for a in listed}
        missing = [
            r
            for r in self._rows().values()
            if r["recording_source"] == "Strava"
            and not r["deleted_at"]
            and r["external_id"] not in seen
        ]
        if prune:
            deleted = sum(self._remove(r) for r in missing)
        return {
            "export": prefix,
            "activities": len(listed),
            "files": 1 + len(originals),
            **counts,
            "samples": samples,
            "missing": sorted(r["external_id"] for r in missing),
            "deleted": deleted,
        }

    def activity(self, a: dict, export: Export, prefix: str) -> tuple[str, str | None, int]:
        """Upsert one activity: (outcome, row id written or confirmed, records appended)."""
        rows = self._by_external(a["id"])
        live = [r for r in rows if not r["deleted_at"]]
        if rows and not live:
            return "skipped", None, 0  # deleted in soma on purpose: never resurrect it
        series, offset = {}, None
        if a["filename"]:
            try:
                parsed = track(a["filename"], export.read(a["filename"]))
            except Exception as e:  # a damaged original still leaves the CSV summary
                log.warning("strava.unreadable", file=a["filename"], error=repr(e)[:200])
                parsed = None
            if parsed:
                series, offset = parsed.series(), parsed.utc_offset
        desired = to_row(a, local_date(a["start"], offset, self.zone), series)
        if live:
            target, outcome = live[0], "updated"
        else:
            target, review = self._race_target(desired)
            if target is None:
                target, outcome = {"id": row_id(a["id"])}, "inserted"
                if review:
                    desired["needs_review"] = review
            else:
                outcome = "enriched"
        if target.get("race_type"):
            desired = {k: v for k, v in desired.items() if k in DEVICE}
        changes = {k: v for k, v in desired.items() if target.get(k) != v}
        if changes:
            self._push(target["id"], changes)
        appended = 0
        # Samples go with new rows and crops (duration or distance changed) only.
        if series and (
            outcome != "updated" or {"duration_minutes", "distance_miles"} & changes.keys()
        ):
            appended = self._samples(a, target["id"], series, prefix + a["filename"])
        outcome = "unchanged" if outcome == "updated" and not changes else outcome
        return outcome, target["id"], appended

    def _race_target(self, desired: dict):
        """(race row to enrich, None) or (None, review reason when a match is ambiguous)."""
        if desired["activity"] != "Running":
            return None, None
        races = [
            r
            for r in self._rows().values()
            if r["date"] == desired["date"]
            and not r["deleted_at"]
            and r["race_type"]
            and not r["external_id"]
        ]
        meters = (desired.get("distance_miles") or 0) * METERS_PER_MILE

        def confirms(race):
            course = race["race_distance_meters"]
            return bool(course and meters and abs(meters - course) <= RACE_TOLERANCE * course)

        hits = [r for r in races if confirms(r)]
        if len(hits) == 1:
            return hits[0], None
        if hits:
            ids = ", ".join(r["id"] for r in hits)
            return (
                None,
                f"Possibly the same activity as race rows {ids}; confirm and merge into one.",
            )
        if unproven := [r["id"] for r in races if not r["race_distance_meters"]]:
            ids = ", ".join(unproven)
            return (
                None,
                f"Same date as race row(s) {ids}, which have no course distance to confirm a match.",
            )
        return None, None

    @staticmethod
    def _edge(activity_id, target_id: str) -> dict:
        created = target_id == row_id(activity_id)  # else an enriched race row
        detail = {"locator": f"activities.csv Activity ID {activity_id}"}
        return {
            "id": f"takeout:{EXPORTS}:{TABLE}:{target_id}",
            "from_kind": "takeout",
            "from_ref": EXPORTS,  # every retained export; the locator finds the activity
            "to_kind": TABLE,
            "to_ref": target_id,
            "rel": "imported_from" if created else "evidence_of",
            "field": None,
            "detail": {"created_row": 1} | detail if created else detail,
            "asserted_by": ASSERTED_BY,
        }

    def _samples(self, a: dict, target_id: str, series: dict, source: str) -> int:
        start = int(a["start"].timestamp())
        base = {"cardio_workout_id": target_id, "activity_id": str(a["id"]), "start_tst": start}
        appended = 0
        for key, data in series.items():
            if key == "time":
                data = [t - start for t in data]  # seconds after the activity start
            meta = base | {"key": key, "original_size": len(data), "source": source}
            for record in records(meta, data):
                self.hub.append(STREAM, record)
                appended += 1
        return appended

    def _remove(self, r: dict) -> int:
        if r["race_type"]:  # the race still happened; only its recording is gone
            reason = (
                f"Strava activity {r['external_id']} is gone from the latest export; "
                "its device fields remain on this race row."
            )
            self._push(r["id"], {"needs_review": reason})
            return 0
        now = self.now()
        self._push(r["id"], {"deleted_at": now, "updated_at": now})
        return 1
