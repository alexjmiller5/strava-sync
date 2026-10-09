"""Strava activities -> life-data cardio_workouts rows, retained raw originals and samples.

The table contract (life-data catalog, cardio_workouts): one row per real
activity; a Strava recording of a race enriches the race row instead of
duplicating it; official race fields and known values are never overwritten
or erased; pace is a query, never stored; point samples go to a producer
stream keyed by the row id; raw originals go to the retained file service.
"""

import hashlib
import json
import math
from collections import Counter
from collections.abc import MutableMapping
from datetime import UTC, datetime, timedelta

import structlog

from core.hub import stamp
from core.strava import BudgetExhausted

TABLE = "cardio_workouts"
STREAM = "cardio_strava"
ASSERTED_BY = "script:strava-sync"
MAX_RECORD_BYTES = 900_000  # the hub refuses stream appends over 1 MB
RACE_TOLERANCE = 0.15  # GPS distance within 15% of the course confirms a race match
METERS_PER_MILE = 1609.344

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


def epoch(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def row_id(activity_id) -> str:
    return f"strava/activity/{activity_id}"


def raw_prefix(activity_id) -> str:
    return f"raw/strava/{activity_id}/"


def _span(minutes: float) -> str:
    m = round(minutes)
    return f"{m}min" if m < 60 else f"{m // 60}h {m % 60}min"


def to_row(a: dict) -> dict:
    """The row values a Strava activity supports; absent facts are left out, never null."""
    sport = a.get("sport_type") or a.get("type") or "Activity"
    activity = ACTIVITY.get(sport, "Other")
    start = datetime.fromisoformat(a["start_date"].replace("Z", "+00:00"))
    elapsed = a.get("elapsed_time") or 0
    meters = a.get("distance") or 0
    climb = a.get("total_elevation_gain") or 0
    heart = a.get("has_heartrate")
    label = LABEL.get(activity, sport)
    span = _span(elapsed / 60)
    text = ((a.get("name") or "").strip(), (a.get("description") or "").strip())
    row = {
        "name": f"{label} - {meters / METERS_PER_MILE:.1f}mi in {span}"
        if meters
        else f"{label} - {span}",
        "activity": activity,
        "date": a["start_date_local"][:10],
        "started_at": iso(start),
        "ended_at": iso(start + timedelta(seconds=elapsed)) if elapsed > 0 else None,
        "duration_minutes": round(elapsed / 60, 2) if elapsed > 0 else None,
        "distance_miles": round(meters / METERS_PER_MILE, 3) if meters > 0 else None,
        "elevation_gain_feet": round(climb * 3.28084, 1) if climb > 0 else None,
        "average_heart_rate": round(a["average_heartrate"])
        if heart and a.get("average_heartrate")
        else None,
        "maximum_heart_rate": round(a["max_heartrate"])
        if heart and a.get("max_heartrate")
        else None,
        "calories": round(a["calories"]) if (a.get("calories") or 0) > 0 else None,
        "recording_source": "Strava",
        "external_id": str(a["id"]),
        # Title, then the description (where incomplete-recording notes live).
        "notes": "\n\n".join(t for t in text if t) or None,
    }
    return {k: v for k, v in row.items() if v is not None}


def records(target_id: str, activity_id, start_tst: int, key: str, stream: dict) -> list[dict]:
    """One stream record per activity + sample key, split only when over the append limit."""
    data = stream.get("data") or []
    base = {
        "cardio_workout_id": target_id,
        "activity_id": str(activity_id),
        "start_tst": start_tst,
        "key": key,
        **{k: stream[k] for k in ("series_type", "original_size", "resolution") if k in stream},
    }
    size = len(json.dumps(base | {"data": data}))
    if size <= MAX_RECORD_BYTES:
        return [base | {"data": data}]
    step = math.ceil(len(data) / math.ceil(size / MAX_RECORD_BYTES))
    offsets = range(0, len(data), step)
    return [
        base | {"offset": i, "parts": len(offsets), "data": data[i : i + step]} for i in offsets
    ]


class Sync:
    def __init__(self, strava, hub, now=stamp):
        self.strava, self.hub, self.now = strava, hub, now

    def _by_external(self, activity_id) -> list[dict]:
        rows = self.hub.rows(TABLE, COLUMNS, {"external_id": str(activity_id)})
        return [r for r in rows if r["recording_source"] == "Strava"]

    def _retain(self, activity_id, kind: str, doc: tuple) -> None:
        data = doc[1]  # the provider's bytes, verbatim; content-addressed so versions never collide
        self.hub.put_file(
            f"{raw_prefix(activity_id)}{kind}-{hashlib.sha256(data).hexdigest()}.json", data
        )

    def activity(self, activity_id) -> str:
        """Upsert one activity; returns inserted / enriched / updated / unchanged / skipped / deleted."""
        rows = self._by_external(activity_id)
        live = [r for r in rows if not r["deleted_at"]]
        if rows and not live:
            return "skipped"  # deleted in life-data on purpose: never resurrect it
        doc = self.strava.activity(activity_id)
        if doc is None:
            return self._remove(activity_id)
        a = doc[0]
        self._retain(activity_id, "activity", doc)
        desired = to_row(a)
        if live:
            target, outcome = live[0], "updated"
        else:
            target, review = self._race_target(a, desired)
            if target is None:
                target, outcome = {"id": row_id(activity_id)}, "inserted"
                if review:
                    desired["needs_review"] = review
            else:
                outcome = "enriched"
            self._edge(activity_id, target["id"], created=outcome == "inserted")
        if target.get("race_type"):
            desired = {k: v for k, v in desired.items() if k in DEVICE}
        changes = {k: v for k, v in desired.items() if target.get(k) != v}
        if changes:
            self.hub.push(TABLE, [{"id": target["id"], **changes}])
        # Samples are re-read only for new rows or a crop (duration/distance changed).
        if outcome != "updated" or {"duration_minutes", "distance_miles"} & changes.keys():
            self._telemetry(activity_id, target["id"], a)
        return "unchanged" if outcome == "updated" and not changes else outcome

    def _race_target(self, a: dict, desired: dict):
        """(race row to enrich, None) or (None, review reason when a match is ambiguous)."""
        if desired["activity"] != "Running":
            return None, None
        races = [
            r
            for r in self.hub.rows(TABLE, COLUMNS, {"date": desired["date"]})
            if not r["deleted_at"] and r["race_type"] and not r["external_id"]
        ]
        meters = a.get("distance") or 0

        def confirms(race):
            course = race["race_distance_meters"]
            close = bool(course and meters and abs(meters - course) <= RACE_TOLERANCE * course)
            return a.get("workout_type") == 1 or close  # 1 = Strava's "Race" run type

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

    def _edge(self, activity_id, target_id: str, created: bool) -> None:
        ref = raw_prefix(activity_id)  # every retained version of this activity
        edge = {
            "id": f"takeout:{ref}:{TABLE}:{target_id}",
            "from_kind": "takeout",
            "from_ref": ref,
            "to_kind": TABLE,
            "to_ref": target_id,
            "rel": "imported_from" if created else "evidence_of",
            "field": None,
            "detail": {"created_row": 1} if created else None,
            "asserted_by": ASSERTED_BY,
        }
        self.hub.push("provenance", [edge])

    def _telemetry(self, activity_id, target_id: str, a: dict) -> None:
        streams, laps = self.strava.streams(activity_id), self.strava.laps(activity_id)
        for kind, doc in (("streams", streams), ("laps", laps)):
            if doc is not None:
                self._retain(activity_id, kind, doc)
        samples = streams[0] if streams and isinstance(streams[0], dict) else {}
        start = epoch(a["start_date"])
        for key, stream in samples.items():
            for record in records(target_id, activity_id, start, key, stream):
                self.hub.append(STREAM, record)

    def _remove(self, activity_id) -> str:
        now, outcome = self.now(), "absent"
        for r in self._by_external(activity_id):
            if r["deleted_at"]:
                continue
            if r["race_type"]:  # the race still happened; only its recording is gone
                reason = f"Strava activity {activity_id} was deleted; its device fields remain on this race row."
                self.hub.push(TABLE, [{"id": r["id"], "needs_review": reason}])
            else:
                self.hub.push(TABLE, [{"id": r["id"], "deleted_at": now, "updated_at": now}])
            outcome = "deleted"
        return outcome

    def delete(self, activity_id) -> str:
        # Events are unsigned: only Strava itself saying 404 proves the deletion.
        if self.strava.activity(activity_id) is not None:
            return "kept"
        return self._remove(activity_id)

    def handle(self, event: dict) -> str:
        if event.get("object_type") != "activity":
            if (event.get("updates") or {}).get("authorized") == "false":
                log.error("strava.deauthorized", fix="re-run scripts/authorize.py")
            return "ignored"
        if event["aspect_type"] == "delete":
            return self.delete(event["object_id"])
        return self.activity(event["object_id"])

    def backfill(self, state: MutableMapping) -> dict:
        """Every activity, newest first, resumable: the cursor is saved after each page."""
        state["backfill"] = "running"
        synced = 0
        try:
            while page := self.strava.activities(before=state.get("backfill_before")):
                for summary in page:
                    if not self._by_external(summary["id"]):
                        self.activity(summary["id"])
                        synced += 1
                # ponytail: a strict `before` cursor would skip a sibling starting in the same
                # second across a page edge; a second-level cursor plus id skip-set fixes it.
                state["backfill_before"] = min(epoch(s["start_date"]) for s in page)
        except BudgetExhausted as e:
            return {"synced": synced, "done": False, "paused": str(e)}
        state["backfill"] = "done"
        return {"synced": synced, "done": True}

    def reconcile(self, days: int) -> dict:
        """Re-read recent activities: catches edits that fire no webhook and missed events."""
        since = epoch(self.now()) - days * 86400
        listed, page = [], 1
        while True:
            batch = self.strava.activities(after=since, page=page)
            listed += batch
            if len(batch) < self.strava.per_page:
                break
            page += 1
        counts = Counter(self.activity(a["id"]) for a in listed)
        seen = {str(a["id"]) for a in listed}
        floor = iso(datetime.fromtimestamp(since, UTC))
        for r in self.hub.rows(TABLE, COLUMNS, {"recording_source": "Strava"}):
            if (
                not r["deleted_at"]
                and r["external_id"] not in seen
                and (r["started_at"] or "") >= floor
            ):
                counts[self.delete(r["external_id"])] += 1
        return {
            "listed": len(listed),
            "inserted": counts["inserted"],
            "updated": counts["updated"] + counts["enriched"],
            "deleted": counts["deleted"],
        }
