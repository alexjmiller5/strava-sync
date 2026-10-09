import json
from urllib.parse import parse_qs

import httpx
import pytest

from core.strava import BudgetExhausted, Strava, StravaAuthError

NOW = 1_760_000_300  # 800 s into a 15-minute window: the next one starts in 100 s


def client(handler, state=None, seed="seed", slept=None):
    return Strava(
        "id",
        "secret",
        seed,
        {} if state is None else state,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=(slept.append if slept is not None else lambda s: None),
        now=lambda: NOW,
    )


def token_reply(request, refresh="rot-1"):
    form = parse_qs(request.content.decode())
    assert form["grant_type"] == ["refresh_token"] and form["client_secret"] == ["secret"]
    return httpx.Response(
        200, json={"access_token": "acc", "expires_at": NOW + 3600, "refresh_token": refresh}
    )


def test_refresh_persists_the_newest_rotated_token():
    seen = []

    def handler(request):
        if request.url.path == "/oauth/token":
            seen.append(parse_qs(request.content.decode())["refresh_token"][0])
            return token_reply(request, refresh=f"rot-{len(seen)}")
        assert request.headers["authorization"] == "Bearer acc"
        return httpx.Response(200, json={"id": 1})

    state = {}
    s = client(handler, state)
    assert s.activity(1)[0] == {"id": 1}
    assert state["token"]["refresh_token"] == "rot-1"
    s.refresh()
    assert seen == ["seed", "rot-1"]  # the rotated token is used, never the stale seed
    assert state["token"]["refresh_token"] == "rot-2"


def test_access_token_is_reused_until_it_expires():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/oauth/token":
            return token_reply(request)
        return httpx.Response(200, json={})

    s = client(handler)
    s.activity(1)
    s.activity(2)
    assert calls.count("/oauth/token") == 1


def test_rejected_stored_token_falls_back_to_a_reconsented_seed():
    def handler(request):
        if request.url.path == "/oauth/token":
            used = parse_qs(request.content.decode())["refresh_token"][0]
            if used == "dead":
                return httpx.Response(400, json={"message": "Bad Request"})
            return token_reply(request, refresh="fresh")
        return httpx.Response(200, json={})

    state = {"token": {"refresh_token": "dead", "access_token": "x", "expires_at": 0}}
    client(handler, state, seed="reconsented").activity(1)
    assert state["token"]["refresh_token"] == "fresh"


def test_all_tokens_rejected_raises_auth_error():
    def handler(request):
        return httpx.Response(401, json={})

    with pytest.raises(StravaAuthError):
        client(handler).activity(1)


def test_404_means_gone_and_raw_bytes_are_returned_verbatim():
    body = b'{"id": 5,   "name": "x"}'

    def handler(request):
        if request.url.path == "/oauth/token":
            return token_reply(request)
        if request.url.path.endswith("/5"):
            return httpx.Response(200, content=body)
        return httpx.Response(404, json={"message": "Record Not Found"})

    s = client(handler)
    assert s.activity(5) == ({"id": 5, "name": "x"}, body)
    assert s.activity(6) is None


def test_streams_request_every_key_by_type():
    def handler(request):
        if request.url.path == "/oauth/token":
            return token_reply(request)
        assert request.url.path == "/api/v3/activities/5/streams"
        assert request.url.params["key_by_type"] == "true"
        assert {"latlng", "heartrate", "time"} <= set(request.url.params["keys"].split(","))
        return httpx.Response(200, json={"time": {"data": [0]}})

    assert client(handler).streams(5)[0] == {"time": {"data": [0]}}


def limited(usage):
    def handler(request):
        if request.url.path == "/oauth/token":
            return token_reply(request)
        return httpx.Response(
            200,
            json=[],
            headers={"X-ReadRateLimit-Limit": "100,1000", "X-ReadRateLimit-Usage": usage},
        )

    return handler


def test_pacer_waits_for_the_next_quarter_hour_near_the_15_minute_limit():
    slept = []
    s = client(limited("96,300"), slept=slept)
    s.activities()
    assert slept == []
    s.activities()
    assert slept == [105]  # 100 s to the boundary plus a 5 s margin


def test_pacer_stops_before_the_daily_limit():
    s = client(limited("10,996"))
    s.activities()
    with pytest.raises(BudgetExhausted):
        s.activities()


def test_429_waits_for_the_window_then_retries():
    replies = iter([429, 200])
    slept = []

    def handler(request):
        if request.url.path == "/oauth/token":
            return token_reply(request)
        return httpx.Response(
            next(replies),
            json=[],
            headers={"X-ReadRateLimit-Limit": "100,1000", "X-ReadRateLimit-Usage": "101,400"},
        )

    assert client(handler, slept=slept).activities() == []
    assert slept == [105]


def test_401_on_a_read_forces_one_refresh():
    tokens = iter(["acc", "acc2"])

    def handler(request):
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200,
                json={"access_token": next(tokens), "expires_at": NOW + 3600, "refresh_token": "r"},
            )
        if request.headers["authorization"] == "Bearer acc":
            return httpx.Response(401, json={})
        return httpx.Response(200, json={"id": 1})

    assert client(handler).activity(1)[0] == {"id": 1}


def test_activity_list_pages_newest_first_with_a_before_cursor():
    def handler(request):
        if request.url.path == "/oauth/token":
            return token_reply(request)
        assert request.url.path == "/api/v3/athlete/activities"
        assert dict(request.url.params) == {"per_page": "200", "page": "1", "before": "99"}
        return httpx.Response(200, content=json.dumps([{"id": 1}]))

    assert client(handler).activities(before=99) == [{"id": 1}]


def test_subscribe_replaces_a_subscription_with_another_callback():
    log = []

    def handler(request):
        log.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": 7, "callback_url": "https://old/strava/k"}])
        if request.method == "DELETE":
            return httpx.Response(204)
        form = parse_qs(request.content.decode())
        assert form["callback_url"] == ["https://new/strava/k"] and form["verify_token"] == ["k"]
        return httpx.Response(201, json={"id": 8})

    assert client(handler).subscribe("https://new/strava/k", "k") == 8
    assert log == [
        ("GET", "/api/v3/push_subscriptions"),
        ("DELETE", "/api/v3/push_subscriptions/7"),
        ("POST", "/api/v3/push_subscriptions"),
    ]


def test_subscribe_keeps_a_matching_subscription():
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json=[{"id": 7, "callback_url": "https://new/strava/k"}])

    assert client(handler).subscribe("https://new/strava/k", "k") == 7
