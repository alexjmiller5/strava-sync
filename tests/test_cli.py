import json

import pytest
from fakes import FakeHub, export, row

from strava_sync import cli
from strava_sync.export import ExportError


def test_import_reads_the_hub_settings_and_prints_the_summary(tmp_path, monkeypatch, capsys):
    hubs = []

    def fake_hub(url, token):
        hubs.append((url, token, FakeHub()))
        return hubs[-1][2]

    monkeypatch.setenv("SOMA_HUB_URL", "https://hub.example")
    monkeypatch.setenv("SOMA_HUB_TOKEN", "tok")
    monkeypatch.setattr(cli, "Hub", fake_hub)
    rows = [row(filename="")] + [row(id=200 + i, filename="") for i in range(60)]  # logs progress
    path = export(tmp_path / "e.zip", rows)  # 11:00 UTC on Oct 8

    cli.main(["import", str(path), "--timezone", "Pacific/Kiritimati"])  # UTC+14

    ((url, token, hub),) = hubs
    assert (url, token) == ("https://hub.example", "tok")
    assert hub.cardio("strava/activity/101")["date"] == "2026-10-09"
    out = json.loads(capsys.readouterr().out)  # stdout is the summary alone
    assert out["inserted"] == 61 and out["export"] == "raw/strava/export/2026-10-09/"


def test_prune_reaches_the_import(tmp_path, monkeypatch):
    monkeypatch.setenv("SOMA_HUB_URL", "https://hub.example")
    monkeypatch.setenv("SOMA_HUB_TOKEN", "tok")
    monkeypatch.setattr(cli, "Hub", lambda url, token: FakeHub())
    with pytest.raises(ExportError, match="empty"):  # only a prune refuses an empty export
        cli.main(["import", str(export(tmp_path / "e.zip", [])), "--prune"])
