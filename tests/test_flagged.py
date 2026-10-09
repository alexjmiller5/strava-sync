import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def flagged():
    path = Path(__file__).parents[1] / "scripts/flagged_activities.py"
    spec = importlib.util.spec_from_file_location("flagged_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ROWS = [
    {"id": "a", "external_id": "1", "date": "2026-10-01", "name": "Run", "notes": "Morning Run"},
    {
        "id": "b",
        "external_id": "2",
        "date": "2026-10-02",
        "name": "Run",
        "notes": "Lunch Run\n\nForgot to start my watch until mile 1",
    },
    {
        "id": "c",
        "external_id": "3",
        "date": "2026-10-03",
        "name": "Bike",
        "notes": "Ride\n\nleft strava running on the train home",
    },
    {"id": "d", "external_id": "4", "date": "2026-10-04", "name": "Run", "notes": None},
]


def test_default_cues_flag_incomplete_recordings(flagged):
    hits = flagged.flagged(ROWS, flagged.DEFAULT_PATTERN)
    assert [h["id"] for h in hits] == ["b", "c"]
    assert hits[0]["cue"] == "Forgot to start"
    assert hits[0]["external_id"] == "2" and hits[0]["date"] == "2026-10-02"


def test_custom_pattern(flagged):
    assert [h["id"] for h in flagged.flagged(ROWS, r"morning")] == ["a"]


def test_main_reads_life_data_and_prints_json_lines(flagged, monkeypatch, capsys):
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)

        class Done:
            stdout = json.dumps(ROWS)

        return Done()

    monkeypatch.setattr(flagged.subprocess, "run", fake_run)
    flagged.main(["--pattern", "train"])
    assert seen[0][:2] == ["life", "sql"]
    assert "recording_source = 'Strava'" in seen[0][2] and "deleted_at IS NULL" in seen[0][2]
    (line,) = capsys.readouterr().out.splitlines()
    assert json.loads(line)["id"] == "c"
