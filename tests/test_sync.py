import json

import pytest
from fakes import RACE, FakeHub, FakeStrava, activity

from core.sync import STREAM, Sync, epoch, row_id

NOW = "2026-10-09T12:00:00.000Z"


def make(acts=(), rows=(), **kw):
    strava, hub = FakeStrava(acts, **kw), FakeHub(rows)
    return Sync(strava, hub, now=lambda: NOW), strava, hub


def test_new_run_becomes_one_row_with_origin_raw_files_and_samples():
    sync, strava, hub = make([activity(description="Easy loop")])

    assert sync.activity(101) == "inserted"

    row = hub.cardio(row_id(101))
    assert row["id"] == "strava/activity/101"
    assert {k: row[k] for k in row if k not in ("id", "updated_at", "deleted_at")} == {
        "name": "Run - 3.1mi in 30min",
        "activity": "Running",
        "date": "2026-10-08",
        "started_at": "2026-10-08T11:00:00.000Z",
        "ended_at": "2026-10-08T11:30:00.000Z",
        "duration_minutes": 30.0,
        "distance_miles": 3.107,
        "elevation_gain_feet": 98.4,
        "average_heart_rate": 150,
        "maximum_heart_rate": 171,
        "calories": 400,
        "recording_source": "Strava",
        "external_id": "101",
        "notes": "Morning Run\n\nEasy loop",
    }
    (edge,) = hub.tables["provenance"].values()
    assert edge["from_kind"] == "takeout"
    assert edge["from_ref"] == "raw/strava/101/"
    assert edge["to_kind"] == "cardio_workouts" and edge["to_ref"] == row["id"]
    assert edge["rel"] == "imported_from" and edge["field"] is None
    assert edge["detail"] == {"created_row": 1}
    assert edge["asserted_by"] == "script:strava-sync"
    kinds = sorted(k.split("/")[3].split("-")[0] for k in hub.files)
    assert kinds == ["activity", "laps", "streams"]
    assert all(k.startswith("raw/strava/101/") and k.endswith(".json") for k in hub.files)
    # raw bytes are the provider's, verbatim
    assert json.loads(next(v for k, v in hub.files.items() if "/activity-" in k))["id"] == 101
    keys = sorted(r["key"] for s, r in hub.records if s == STREAM)
    assert keys == ["heartrate", "time"]
    rec = next(r for _, r in hub.records if r["key"] == "heartrate")
    assert rec["cardio_workout_id"] == row["id"] and rec["activity_id"] == "101"
    assert rec["start_tst"] == epoch("2026-10-08T11:00:00Z") and rec["data"] == [140, 150, 160]


def test_evening_run_keeps_its_local_date():
    a = activity(start_date="2026-10-09T01:00:00Z", start_date_local="2026-10-08T21:00:00Z")
    sync, _, hub = make([a])
    sync.activity(101)
    row = hub.cardio(row_id(101))
    assert (row["date"], row["started_at"]) == ("2026-10-08", "2026-10-09T01:00:00.000Z")


def test_raw_files_are_written_before_the_row():
    sync, _, hub = make([activity()])
    order = []
    hub.put_file = lambda key, data: order.append("file")
    push = hub.push
    hub.push = lambda table, rows: (order.append(table), push(table, rows))
    sync.activity(101)
    assert order[:3] == ["file", "provenance", "cardio_workouts"]


@pytest.mark.parametrize(
    ("sport", "activity_value", "name"),
    [
        ("Ride", "Biking", "Bike - 3.1mi in 30min"),
        ("GravelRide", "Biking", "Bike - 3.1mi in 30min"),
        ("Walk", "Walking", "Walk - 3.1mi in 30min"),
        ("Hike", "Hiking", "Hike - 3.1mi in 30min"),
        ("Swim", "Swimming", "Swim - 3.1mi in 30min"),
        ("Rowing", "Rowing", "Row - 3.1mi in 30min"),
        ("TrailRun", "Running", "Run - 3.1mi in 30min"),
        ("Yoga", "Other", "Yoga - 3.1mi in 30min"),
    ],
)
def test_sport_types_map_to_the_activity_select(sport, activity_value, name):
    sync, _, hub = make([activity(sport_type=sport)])
    sync.activity(101)
    row = hub.cardio(row_id(101))
    assert (row["activity"], row["name"]) == (activity_value, name)


def test_indoor_session_without_distance_or_heart_rate_leaves_them_null():
    a = activity(
        sport_type="WeightTraining",
        distance=0.0,
        total_elevation_gain=0.0,
        has_heartrate=False,
        average_heartrate=None,
        max_heartrate=None,
        calories=0.0,
        elapsed_time=4500,
    )
    sync, _, hub = make([a])
    sync.activity(101)
    row = hub.cardio(row_id(101))
    assert row["name"] == "WeightTraining - 1h 15min"
    for col in (
        "distance_miles",
        "elevation_gain_feet",
        "average_heart_rate",
        "maximum_heart_rate",
        "calories",
    ):
        assert col not in row


def test_race_row_is_enriched_never_duplicated_or_overwritten():
    sync, _, hub = make([activity(distance=5100.0, name="Turkey Trot 5K")], rows=[RACE])

    assert sync.activity(101) == "enriched"

    assert list(hub.tables["cardio_workouts"]) == [RACE["id"]]
    row = hub.cardio(RACE["id"])
    for col in (
        "name",
        "activity",
        "date",
        "notes",
        "race_type",
        "race_distance_meters",
        "race_time_seconds",
    ):
        assert row[col] == RACE[col]
    assert row["recording_source"] == "Strava" and row["external_id"] == "101"
    assert row["started_at"] == "2026-10-08T11:00:00.000Z" and row["distance_miles"] == 3.169
    (edge,) = hub.tables["provenance"].values()
    assert edge["rel"] == "evidence_of" and edge["to_ref"] == RACE["id"] and edge["detail"] is None
    assert {r["cardio_workout_id"] for _, r in hub.records} == {RACE["id"]}


def test_strava_marked_race_matches_even_when_gps_distance_drifts():
    sync, _, hub = make([activity(distance=7000.0, workout_type=1)], rows=[RACE])
    assert sync.activity(101) == "enriched"


def test_warmup_on_race_day_stays_a_separate_row():
    sync, _, hub = make([activity(distance=3000.0)], rows=[RACE])
    assert sync.activity(101) == "inserted"
    assert hub.cardio(RACE["id"]) == RACE
    assert "needs_review" not in hub.cardio(row_id(101))


def test_ambiguous_race_match_is_inserted_for_review():
    other = RACE | {"id": "athlinks/turkey/1/3", "race_result_id": "athlinks/turkey/1/3"}
    sync, _, hub = make([activity()], rows=[RACE, other])
    assert sync.activity(101) == "inserted"
    review = hub.cardio(row_id(101))["needs_review"]
    assert RACE["id"] in review and other["id"] in review


def test_race_row_without_course_distance_needs_review():
    sync, _, hub = make([activity()], rows=[RACE | {"race_distance_meters": None}])
    sync.activity(101)
    assert RACE["id"] in hub.cardio(row_id(101))["needs_review"]


def test_a_race_row_already_linked_to_a_recording_is_not_claimed_twice():
    linked = RACE | {"recording_source": "Strava", "external_id": "55"}
    sync, _, hub = make([activity()], rows=[linked])
    assert sync.activity(101) == "inserted"
    assert hub.cardio(RACE["id"])["external_id"] == "55"


def test_rides_never_match_running_races():
    sync, _, hub = make([activity(sport_type="Ride")], rows=[RACE])
    assert sync.activity(101) == "inserted"
    assert "needs_review" not in hub.cardio(row_id(101))


def test_unchanged_reimport_pushes_nothing_and_fetches_no_telemetry():
    sync, strava, hub = make([activity()])
    sync.activity(101)
    pushes, calls = len(hub.pushes), len(strava.calls)

    assert sync.activity(101) == "unchanged"
    assert len(hub.pushes) == pushes
    assert strava.calls[calls:] == [("activity", 101)]


def test_description_edit_updates_notes_without_refetching_telemetry():
    sync, strava, hub = make([activity()])
    sync.activity(101)
    strava.acts[101] = activity(description="Forgot to start the watch until mile 1")
    calls = len(strava.calls)

    assert sync.activity(101) == "updated"
    assert hub.pushes[-1][1].keys() == {"id", "notes"}
    assert hub.cardio(row_id(101))["notes"].endswith("Forgot to start the watch until mile 1")
    assert strava.calls[calls:] == [("activity", 101)]


def test_crop_refetches_telemetry():
    sync, strava, hub = make([activity()])
    sync.activity(101)
    strava.acts[101] = activity(elapsed_time=1500, distance=4200.0)
    records = len(hub.records)
    sync.activity(101)
    assert ("streams", 101) in strava.calls[-2:]
    assert len(hub.records) == records + 2


def test_missing_provider_values_never_erase_known_ones():
    sync, strava, hub = make([activity()])
    sync.activity(101)
    strava.acts[101] = activity(has_heartrate=False, average_heartrate=None, max_heartrate=None)
    sync.activity(101)
    row = hub.cardio(row_id(101))
    assert (row["average_heart_rate"], row["maximum_heart_rate"]) == (150, 171)


def test_review_flags_set_by_alex_are_kept():
    sync, strava, hub = make([activity()])
    sync.activity(101)
    hub.cardio(row_id(101))["needs_review"] = "checked by hand"
    strava.acts[101] = activity(name="Renamed")
    sync.activity(101)
    assert hub.cardio(row_id(101))["needs_review"] == "checked by hand"


def test_soft_deleted_row_is_never_resurrected():
    sync, _, hub = make([activity()])
    sync.activity(101)
    hub.cardio(row_id(101))["deleted_at"] = NOW
    pushes = len(hub.pushes)
    assert sync.activity(101) == "skipped"
    assert len(hub.pushes) == pushes


def test_delete_event_soft_deletes_after_strava_confirms():
    sync, strava, hub = make([activity()])
    sync.activity(101)
    del strava.acts[101]
    assert sync.handle({"object_type": "activity", "object_id": 101, "aspect_type": "delete"}) == (
        "deleted"
    )
    row = hub.cardio(row_id(101))
    assert row["deleted_at"] == NOW and row["updated_at"] == NOW


def test_delete_event_for_an_activity_that_still_exists_is_ignored():
    sync, _, hub = make([activity()])
    sync.activity(101)
    sync.handle({"object_type": "activity", "object_id": 101, "aspect_type": "delete"})
    assert hub.cardio(row_id(101))["deleted_at"] is None


def test_deleting_the_recording_of_a_race_flags_it_instead_of_deleting_the_race():
    sync, strava, hub = make([activity()], rows=[RACE])
    sync.activity(101)
    del strava.acts[101]
    sync.delete(101)
    row = hub.cardio(RACE["id"])
    assert row["deleted_at"] is None
    assert "101" in row["needs_review"]


def test_create_and_update_events_sync_the_activity():
    sync, _, hub = make([activity()])
    assert sync.handle({"object_type": "activity", "object_id": 101, "aspect_type": "create"}) == (
        "inserted"
    )
    event = {"object_type": "activity", "object_id": 101, "aspect_type": "update"}
    assert sync.handle(event | {"updates": {"title": "x"}}) == "unchanged"


def test_athlete_events_are_ignored():
    sync, strava, hub = make()
    event = {"object_type": "athlete", "object_id": 7, "aspect_type": "update"}
    assert sync.handle(event | {"updates": {"authorized": "false"}}) == "ignored"
    assert strava.calls == [] and hub.pushes == []


def five():
    return [
        activity(
            id=i, start_date=f"2026-10-0{i}T11:00:00Z", start_date_local=f"2026-10-0{i}T07:00:00Z"
        )
        for i in range(1, 6)
    ]


def test_backfill_walks_every_page_and_resumes_after_the_daily_budget():
    sync, strava, hub = make(five())
    state = {}
    strava.fail_after = 8  # first page synced, second page listed, then the budget runs out

    first = sync.backfill(state)
    assert first["done"] is False and first["paused"] == "daily read budget spent"
    assert state["backfill"] == "running"

    strava.fail_after = None
    second = sync.backfill(state)
    assert second["done"] is True and state["backfill"] == "done"
    ids = sorted(r["external_id"] for r in hub.tables["cardio_workouts"].values())
    assert ids == ["1", "2", "3", "4", "5"]
    fetched = [c[1] for c in strava.calls if c[0] == "activity"]
    assert sorted(fetched) == [1, 2, 3, 4, 5]  # each detail fetched exactly once


def test_backfill_skips_activities_already_stored():
    sync, strava, hub = make(five())
    sync.activity(3)
    calls = len(strava.calls)
    sync.backfill({})
    assert ("activity", 3) not in strava.calls[calls:]


def test_reconcile_catches_missed_creates_and_deletes_in_the_window():
    acts = five()
    sync, strava, hub = make(acts)
    for i in (2, 4, 5):
        sync.activity(i)
    old = activity(id=9, start_date="2026-09-01T11:00:00Z", start_date_local="2026-09-01T07:00:00Z")
    strava.acts[9] = old
    sync.activity(9)
    del strava.acts[9]  # outside the window: left alone even though it vanished
    del strava.acts[4]  # deleted while the webhook was down
    sync.now = lambda: "2026-10-06T00:00:00.000Z"

    out = sync.reconcile(days=4)

    assert out == {"listed": 3, "inserted": 1, "updated": 0, "deleted": 1}
    assert hub.cardio(row_id(3))["external_id"] == "3"
    assert hub.cardio(row_id(4))["deleted_at"] is not None
    assert hub.cardio(row_id(9))["deleted_at"] is None


def test_oversized_sample_streams_are_split_under_the_append_limit():
    big = {"latlng": {"data": [[42.123456, -71.123456]] * 60000, "series_type": "time"}}
    sync, _, hub = make([activity()], streams=big)
    sync.activity(101)
    parts = [r for _, r in hub.records if r["key"] == "latlng"]
    assert len(parts) > 1
    assert all(len(json.dumps(p)) < 1_000_000 for p in parts)
    assert [p["offset"] for p in parts] == sorted(p["offset"] for p in parts)
    assert sum(len(p["data"]) for p in parts) == 60000
    assert {p["parts"] for p in parts} == {len(parts)}
