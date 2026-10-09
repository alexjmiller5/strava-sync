import json
from zoneinfo import ZoneInfo

import pytest
from fakes import RACE, T0, FakeHub, export, fit, gz, points, row

from strava_sync.export import Export, ExportError
from strava_sync.sync import STREAM, Sync, row_id

NOW = "2026-10-09T12:00:00.000Z"
NY = ZoneInfo("America/New_York")
PREFIX = "raw/strava/export/2026-10-09/"


def fit_files(*ids, **kw):
    return {f"activities/{i}.fit.gz": gz(fit(points(**kw))) for i in ids}


class World:
    """One hub, any number of exports imported into it in turn."""

    def __init__(self, tmp_path, hub_rows=()):
        self.tmp, self.hub, self.n = tmp_path, FakeHub(hub_rows), 0

    def run(self, rows, files=None, **kw):
        self.n += 1
        path = export(self.tmp / f"e{self.n}.zip", rows, files)
        with Export(path) as e:
            return Sync(self.hub, zone=NY, now=lambda: NOW).run(e, **kw)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_new_run_becomes_one_row_with_origin_raw_files_and_samples(world):
    out = world.run([row(description="Easy loop")], fit_files(101))
    hub = world.hub

    r = hub.cardio(row_id(101))
    assert r["id"] == "strava/activity/101"
    assert {k: r[k] for k in r if k not in ("id", "updated_at", "deleted_at")} == {
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
    assert edge["id"] == "takeout:raw/strava/export/:cardio_workouts:strava/activity/101"
    assert edge["from_kind"] == "takeout" and edge["from_ref"] == "raw/strava/export/"
    assert edge["to_kind"] == "cardio_workouts" and edge["to_ref"] == r["id"]
    assert edge["rel"] == "imported_from" and edge["field"] is None
    assert edge["detail"] == {"created_row": 1, "locator": "activities.csv Activity ID 101"}
    assert edge["asserted_by"] == "script:strava-sync"
    assert sorted(hub.files) == [PREFIX + "activities.csv", PREFIX + "activities/101.fit.gz"]
    assert hub.files[PREFIX + "activities/101.fit.gz"] == fit_files(101)["activities/101.fit.gz"]
    assert hub.types[PREFIX + "activities.csv"] == "text/csv"
    recs = {r["key"]: r for s, r in hub.records if s == STREAM}
    assert sorted(recs) == ["altitude", "distance", "heartrate", "latlng", "time"]
    hr = recs["heartrate"]
    assert hr["cardio_workout_id"] == r["id"] and hr["activity_id"] == "101"
    assert hr["start_tst"] == T0 and hr["data"] == [140, 150, 160] and hr["original_size"] == 3
    assert hr["source"] == PREFIX + "activities/101.fit.gz"
    assert recs["time"]["data"] == [0, 1, 2]  # seconds after the activity start
    assert out == {
        "export": PREFIX,
        "activities": 1,
        "files": 2,
        "inserted": 1,
        "enriched": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped": 0,
        "samples": 5,
        "missing": [],
        "deleted": 0,
    }


def test_evening_run_keeps_its_local_date(world):
    world.run([row(date="Oct 9, 2026, 1:00:00 AM", filename="")])
    r = world.hub.cardio(row_id(101))
    assert (r["date"], r["started_at"]) == ("2026-10-08", "2026-10-09T01:00:00.000Z")


def test_the_files_own_utc_offset_beats_the_importer_time_zone(world):
    late = row(date="Oct 8, 2026, 11:30:00 PM")  # 01:30 the next day in Paris
    pts = [p | {"time": p["time"] + 45000} for p in points()]
    world.run([late], {"activities/101.fit.gz": gz(fit(pts, local_offset=7200))})
    assert world.hub.cardio(row_id(101))["date"] == "2026-10-09"


def test_raw_files_come_first_and_the_edge_follows_its_row(world):
    world.run([row()], fit_files(101))
    kinds = [k for k, _ in world.hub.order]
    assert kinds[:4] == ["file", "file", "push", "edge"]


def test_a_missing_origin_edge_is_restored_on_the_next_import(world):
    world.run([row()])
    world.hub.tables["provenance"].clear()  # e.g. a crash between the row and its edge
    assert world.run([row()])["unchanged"] == 1
    (edge,) = world.hub.tables["provenance"].values()
    assert edge["rel"] == "imported_from" and edge["detail"]["created_row"] == 1


def test_an_enriched_race_row_keeps_an_evidence_edge_on_later_imports(tmp_path):
    world = World(tmp_path, [RACE])
    assert world.run([row(meters=5100.0)])["enriched"] == 1
    world.hub.tables["provenance"].clear()
    assert world.run([row(meters=5100.0)])["unchanged"] == 1
    (edge,) = world.hub.tables["provenance"].values()
    assert edge["rel"] == "evidence_of" and edge["to_ref"] == RACE["id"]
    assert edge["detail"] == {"locator": "activities.csv Activity ID 101"}


@pytest.mark.parametrize(
    ("sport", "activity", "name"),
    [
        ("Ride", "Biking", "Bike - 3.1mi in 30min"),
        ("Gravel Ride", "Biking", "Bike - 3.1mi in 30min"),
        ("E-Bike Ride", "Biking", "Bike - 3.1mi in 30min"),
        ("Virtual Ride", "Biking", "Bike - 3.1mi in 30min"),
        ("Walk", "Walking", "Walk - 3.1mi in 30min"),
        ("Hike", "Hiking", "Hike - 3.1mi in 30min"),
        ("Swim", "Swimming", "Swim - 3.1mi in 30min"),
        ("Rowing", "Rowing", "Row - 3.1mi in 30min"),
        ("Trail Run", "Running", "Run - 3.1mi in 30min"),
        ("Stair-Stepper", "Stairmaster", "Stairmaster - 3.1mi in 30min"),
        ("Yoga", "Other", "Yoga - 3.1mi in 30min"),
    ],
)
def test_activity_types_map_to_the_activity_select(world, sport, activity, name):
    world.run([row(type=sport)])
    r = world.hub.cardio(row_id(101))
    assert (r["activity"], r["name"]) == (activity, name)


def test_indoor_session_without_distance_or_heart_rate_leaves_them_null(world):
    world.run(
        [
            row(
                type="Weight Training",
                meters=0.0,
                elevation=0.0,
                avg_hr=None,
                max_hr=None,
                calories=0.0,
                elapsed=4500,
                filename="",
            )
        ]
    )
    r = world.hub.cardio(row_id(101))
    assert r["name"] == "Weight Training - 1h 15min"
    for col in (
        "distance_miles",
        "elevation_gain_feet",
        "average_heart_rate",
        "maximum_heart_rate",
        "calories",
    ):
        assert col not in r


def test_heart_rate_missing_from_the_csv_comes_from_the_original_file(world):
    world.run([row(avg_hr=None, max_hr=None)], fit_files(101, hr=(140, 151, 166)))
    r = world.hub.cardio(row_id(101))
    assert (r["average_heart_rate"], r["maximum_heart_rate"]) == (152, 166)


def test_the_private_note_joins_title_and_description_in_notes(world):
    world.run([row(description="Tempo", private_note="Forgot to stop at the end")])
    notes = world.hub.cardio(row_id(101))["notes"]
    assert notes == "Morning Run\n\nTempo\n\nForgot to stop at the end"


def test_a_corrupt_original_still_imports_its_row_without_samples(world):
    out = world.run([row()], {"activities/101.fit.gz": b"not gzip"})
    assert out["inserted"] == 1 and out["samples"] == 0
    assert world.hub.files[PREFIX + "activities/101.fit.gz"] == b"not gzip"  # retained anyway


def test_race_row_is_enriched_never_duplicated_or_overwritten(tmp_path):
    world = World(tmp_path, [RACE])
    world.run([row(meters=5100.0, name="Turkey Trot 5K")], fit_files(101))
    hub = world.hub
    assert list(hub.tables["cardio_workouts"]) == [RACE["id"]]
    r = hub.cardio(RACE["id"])
    for col in ("name", "activity", "date", "notes", "race_type", "race_distance_meters"):
        assert r[col] == RACE[col]
    assert r["race_time_seconds"] == RACE["race_time_seconds"]
    assert r["recording_source"] == "Strava" and r["external_id"] == "101"
    assert r["started_at"] == "2026-10-08T11:00:00.000Z" and r["distance_miles"] == 3.169
    (edge,) = hub.tables["provenance"].values()
    assert edge["rel"] == "evidence_of" and edge["to_ref"] == RACE["id"]
    assert {r["cardio_workout_id"] for _, r in hub.records} == {RACE["id"]}


def test_warmup_on_race_day_stays_a_separate_row(tmp_path):
    world = World(tmp_path, [RACE])
    assert world.run([row(meters=3000.0)])["inserted"] == 1
    assert world.hub.cardio(RACE["id"]) == RACE
    assert "needs_review" not in world.hub.cardio(row_id(101))


def test_a_claimed_race_is_not_claimed_again_by_a_later_run_that_day(tmp_path):
    world = World(tmp_path, [RACE])
    later = row(id=102, date="Oct 8, 2026, 3:00:00 PM", filename="")
    out = world.run([row(filename=""), later])
    assert (out["enriched"], out["inserted"]) == (1, 1)
    assert world.hub.cardio(RACE["id"])["external_id"] == "101"
    assert world.hub.cardio(row_id(102))["external_id"] == "102"


def test_ambiguous_race_match_is_inserted_for_review(tmp_path):
    other = RACE | {"id": "athlinks/turkey/1/3", "race_result_id": "athlinks/turkey/1/3"}
    world = World(tmp_path, [RACE, other])
    assert world.run([row()])["inserted"] == 1
    review = world.hub.cardio(row_id(101))["needs_review"]
    assert RACE["id"] in review and other["id"] in review


def test_race_row_without_course_distance_needs_review(tmp_path):
    world = World(tmp_path, [RACE | {"race_distance_meters": None}])
    world.run([row()])
    assert RACE["id"] in world.hub.cardio(row_id(101))["needs_review"]


def test_a_race_row_already_linked_to_a_recording_is_not_claimed_twice(tmp_path):
    world = World(tmp_path, [RACE | {"recording_source": "Strava", "external_id": "55"}])
    assert world.run([row()])["inserted"] == 1
    assert world.hub.cardio(RACE["id"])["external_id"] == "55"


def test_rides_never_match_running_races(tmp_path):
    world = World(tmp_path, [RACE])
    assert world.run([row(type="Ride")])["inserted"] == 1
    assert "needs_review" not in world.hub.cardio(row_id(101))


def test_unchanged_reimport_pushes_nothing_and_sends_no_samples(world):
    world.run([row()], fit_files(101))
    pushes, records = len(world.hub.pushes), len(world.hub.records)
    out = world.run([row()], fit_files(101))
    assert out["unchanged"] == 1 and out["samples"] == 0
    assert len(world.hub.pushes) == pushes and len(world.hub.records) == records


def test_a_later_export_updates_only_what_changed(world):
    world.run([row()], fit_files(101))
    records = len(world.hub.records)
    out = world.run([row(description="Forgot to start the watch until mile 1")], fit_files(101))
    assert out["updated"] == 1
    assert world.hub.pushes[-1][1].keys() == {"id", "notes"}
    assert world.hub.cardio(row_id(101))["notes"].endswith("until mile 1")
    assert len(world.hub.records) == records


def test_a_crop_resends_samples(world):
    world.run([row()], fit_files(101))
    records = len(world.hub.records)
    assert world.run([row(elapsed=1500, meters=4200.0)], fit_files(101))["updated"] == 1
    assert len(world.hub.records) == records + 5


def test_missing_values_never_erase_known_ones(world):
    world.run([row()])
    world.run([row(avg_hr=None, max_hr=None)])
    r = world.hub.cardio(row_id(101))
    assert (r["average_heart_rate"], r["maximum_heart_rate"]) == (150, 171)


def test_review_flags_set_by_alex_are_kept(world):
    world.run([row()])
    world.hub.cardio(row_id(101))["needs_review"] = "checked by hand"
    world.run([row(name="Renamed")])
    assert world.hub.cardio(row_id(101))["needs_review"] == "checked by hand"


def test_soft_deleted_row_is_never_resurrected(world):
    world.run([row()])
    world.hub.cardio(row_id(101))["deleted_at"] = NOW
    pushes = len(world.hub.pushes)
    assert world.run([row(name="Renamed")])["skipped"] == 1
    assert len(world.hub.pushes) == pushes


def test_activities_gone_from_a_later_export_are_reported_not_deleted(world):
    world.run([row(), row(id=102, filename="")])
    out = world.run([row()])
    assert out["missing"] == ["102"] and out["deleted"] == 0
    assert world.hub.cardio(row_id(102))["deleted_at"] is None


def test_prune_soft_deletes_them_and_flags_a_race_recording_instead(tmp_path):
    world = World(tmp_path, [RACE])
    world.run([row(), row(id=102, date="Oct 7, 2026, 11:00:00 AM", filename="")])
    out = world.run([row(id=103, date="Oct 6, 2026, 11:00:00 AM", filename="")], prune=True)
    assert sorted(out["missing"]) == ["101", "102"] and out["deleted"] == 1
    gone = world.hub.cardio(row_id(102))
    assert gone["deleted_at"] == NOW and gone["updated_at"] == NOW
    race = world.hub.cardio(RACE["id"])
    assert race["deleted_at"] is None and "101" in race["needs_review"]


def test_an_empty_export_never_prunes(world):
    world.run([row(filename="")])
    with pytest.raises(ExportError, match="empty"):
        world.run([], prune=True)
    assert world.hub.cardio(row_id(101))["deleted_at"] is None


def test_oversized_sample_series_are_split_under_the_append_limit(world):
    pts = [p | {"time": T0 + i} for i, p in enumerate(points(1) * 60000)]
    world.run([row(elapsed=60000)], {"activities/101.fit.gz": gz(fit(pts))})
    parts = [r for _, r in world.hub.records if r["key"] == "latlng"]
    assert len(parts) > 1
    assert all(len(json.dumps(p)) < 1_000_000 for p in parts)
    assert [p["offset"] for p in parts] == sorted(p["offset"] for p in parts)
    assert sum(len(p["data"]) for p in parts) == 60000
    assert {p["parts"] for p in parts} == {len(parts)}
