import hashlib
import json
import re

import httpx
import pytest

from strava_sync.hub import Hub, HubError


def hub(handler):
    return Hub(
        "https://hub.example/", "tok", http=httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_rows_follow_every_cursor_page():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        assert request.headers["authorization"] == "Bearer tok"
        if "after" not in body:
            return httpx.Response(200, json={"rows": [{"id": "a"}], "next_cursor": "c1"})
        return httpx.Response(200, json={"rows": [{"id": "b"}], "next_cursor": None})

    rows = hub(handler).rows("cardio_workouts", ["id"], {"date": "2026-10-08"})
    assert rows == [{"id": "a"}, {"id": "b"}]
    assert bodies[0] == {
        "table": "cardio_workouts",
        "columns": ["id"],
        "where": {"date": "2026-10-08"},
        "limit": 200,
    }
    assert bodies[1]["after"] == "c1"


def test_push_stamps_updated_at_and_groups_rows_by_column_set():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"upserted": 1, "rejected": []})

    hub(handler).push("t", [{"id": "a", "x": 1}, {"id": "b", "y": 2}])
    assert [b["columns"] for b in bodies] == [["id", "updated_at", "x"], ["id", "updated_at", "y"]]
    stamp = bodies[0]["rows"][0]["updated_at"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", stamp)


def test_push_keeps_an_explicit_updated_at():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"upserted": 1, "rejected": []})

    hub(handler).push("t", [{"id": "a", "deleted_at": "S", "updated_at": "S"}])
    assert bodies[0]["rows"][0]["updated_at"] == "S"


def test_push_rejection_raises():
    def handler(request):
        return httpx.Response(200, json={"upserted": 0, "rejected": [{"row_id": "a", "rule": "x"}]})

    with pytest.raises(HubError, match="rejected"):
        hub(handler).push("t", [{"id": "a"}])


def test_put_file_is_conditional_and_content_checked():
    data = b'{"id":1}'

    def handler(request):
        assert request.method == "PUT"
        assert request.url.path == "/v1/files/raw/strava/1/activity-abc.json"
        assert request.headers["if-none-match"] == "*"
        assert request.headers["x-content-sha256"] == hashlib.sha256(data).hexdigest()
        assert request.headers["content-type"] == "text/csv"
        assert request.content == data
        return httpx.Response(412)  # already retained: same key means same bytes

    hub(handler).put_file("raw/strava/1/activity-abc.json", data, "text/csv")


def test_put_file_failure_raises():
    with pytest.raises(HubError):
        hub(lambda r: httpx.Response(403)).put_file("raw/strava/1/a.json", b"{}")


def test_append_posts_the_record_to_the_stream():
    def handler(request):
        assert request.url.path == "/v1/streams/cardio_strava/append"
        assert json.loads(request.content) == {"key": "time"}
        return httpx.Response(200, json={"ok": True})

    hub(handler).append("cardio_strava", {"key": "time"})


def test_insert_posts_to_the_insert_only_route_and_accepts_existing():
    bodies = []

    def handler(request):
        assert request.url.path == "/v1/rows/insert"
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"inserted": [], "existing": ["e"], "rejected": []})

    hub(handler).insert("provenance", [{"id": "e", "rel": "imported_from"}])
    assert bodies[0]["table"] == "provenance"
    assert bodies[0]["columns"] == ["id", "rel", "updated_at"]
    assert re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", bodies[0]["rows"][0]["updated_at"]
    )


def test_insert_rejection_raises():
    def handler(request):
        return httpx.Response(200, json={"inserted": [], "existing": [], "rejected": [{"id": "e"}]})

    with pytest.raises(HubError, match="rejected"):
        hub(handler).insert("provenance", [{"id": "e"}])
