from datetime import UTC, datetime

import pytest
from fakes import HEADER, T0, export, fit, gpx, gz, points, row, tcx

from strava_sync.export import Export, ExportError, track


def test_activities_read_raw_units_from_the_repeated_columns(tmp_path):
    path = export(tmp_path / "e.zip", [row(description="Easy loop\nwith strides")])
    with Export(path) as e:
        (a,) = e.activities()
    assert a == {
        "id": "101",
        "name": "Morning Run",
        "description": "Easy loop\nwith strides",
        "private_note": "",
        "type": "Run",
        "start": datetime(2026, 10, 8, 11, 0, tzinfo=UTC),
        "elapsed_time": 1800.0,
        "distance": 5000.0,  # meters (the last Distance), never the km display column
        "total_elevation_gain": 30.0,
        "average_heartrate": 150.4,
        "max_heartrate": 171.0,
        "calories": 400.0,
        "filename": "activities/101.fit.gz",
    }


def test_blank_cells_are_none(tmp_path):
    r = row(avg_hr=None, max_hr=None, calories=None, elevation=None, filename="")
    with Export(export(tmp_path / "e.zip", [r])) as e:
        (a,) = e.activities()
    assert a["average_heartrate"] is None and a["max_heartrate"] is None
    assert a["calories"] is None and a["total_elevation_gain"] is None
    assert a["filename"] is None


def test_afternoon_dates_parse_on_the_24_hour_clock(tmp_path):
    with Export(export(tmp_path / "e.zip", [row(date="Jan 18, 2019, 3:04:05 PM")])) as e:
        (a,) = e.activities()
    assert a["start"] == datetime(2019, 1, 18, 15, 4, 5, tzinfo=UTC)


def test_a_changed_csv_layout_is_refused_rather_than_misread(tmp_path):
    header = [h for i, h in enumerate(HEADER) if h != "Distance" or i > 10]  # one Distance left
    path = export(tmp_path / "e.zip", [], header=header)
    with Export(path) as e, pytest.raises(ExportError, match="layout"):
        e.activities()


def test_export_files_dates_and_a_nested_root(tmp_path):
    files = {"activities/101.fit.gz": gz(fit(points())), "activities/7.gpx": gpx(points())}
    path = export(tmp_path / "e.zip", [row()], files, root="export_42/")
    with Export(path) as e:
        assert e.date == "2026-10-09"
        assert e.csv.startswith(b"Activity ID,")
        assert sorted(e.originals()) == ["activities/101.fit.gz", "activities/7.gpx"]
        assert e.read("activities/7.gpx") == files["activities/7.gpx"]


def test_a_zip_without_activities_csv_is_not_an_export(tmp_path):
    import zipfile

    with zipfile.ZipFile(tmp_path / "x.zip", "w") as z:
        z.writestr("other.txt", "x")
    with pytest.raises(ExportError, match="activities.csv"):
        Export(tmp_path / "x.zip")


def test_fit_samples_and_the_devices_local_offset():
    t = track("activities/101.fit.gz", gz(fit(points(hr=(140, None, 160)), local_offset=-14400)))
    assert t.utc_offset == -14400
    s = t.series()
    assert s["time"] == [T0, T0 + 1, T0 + 2]
    assert s["heartrate"] == [140, None, 160]
    assert s["latlng"][0] == pytest.approx([42.36, -71.06], abs=1e-6)
    assert s["altitude"] == [10.0, 11.0, 12.0]
    assert s["distance"] == [0.0, 3.0, 6.0]
    assert set(s) == {"time", "latlng", "altitude", "heartrate", "distance"}  # no empty series


def test_fit_without_an_activity_message_has_no_offset():
    assert track("a.fit", fit(points())).utc_offset is None


def test_gpx_samples_include_extension_heart_rate_and_cadence():
    s = track("activities/7.gpx", gpx(points())).series()
    assert s["time"] == [T0, T0 + 1, T0 + 2]
    assert s["heartrate"] == [140, 150, 160]
    assert s["cadence"] == [80, 80, 80]
    assert s["latlng"][1] == [42.3601, -71.06]
    assert s["altitude"] == [10, 11, 12]


def test_tcx_with_leading_whitespace_parses():
    t = track("activities/8.tcx.gz", gz(tcx(points())))
    assert t.utc_offset is None
    s = t.series()
    assert s["time"] == [T0, T0 + 1, T0 + 2]
    assert s["heartrate"] == [140, 150, 160]
    assert s["distance"] == [0, 3, 6]
    assert s["latlng"][2] == [42.3602, -71.06]


@pytest.mark.parametrize(("name", "build"), [("a.gpx", gpx), ("a.tcx", tcx)])
def test_untimed_points_are_skipped(name, build):
    pts = points()
    pts[1]["time"] = None
    s = track(name, build(pts)).series()
    assert s["time"] == [T0, T0 + 2] and s["heartrate"] == [140, 160]


def test_a_file_without_points_has_no_series():
    assert track("a.gpx", gpx([])).series() == {}


def test_unknown_file_types_have_no_track():
    assert track("activities/9.jpg", b"\xff\xd8") is None
