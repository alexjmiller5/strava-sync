from fastapi.testclient import TestClient

from core.web import create_app

KEY = "s3cret"
EVENT = {
    "object_type": "activity",
    "object_id": 101,
    "aspect_type": "create",
    "updates": {},
    "owner_id": 7,
    "subscription_id": 9,
    "event_time": 1760000000,
}


def setup():
    queued = []
    return TestClient(create_app(KEY, queued.append)), queued


def validation(token=KEY, mode="subscribe"):
    return {"hub.mode": mode, "hub.challenge": "abc", "hub.verify_token": token}


def test_subscription_challenge_is_echoed():
    web, _ = setup()
    r = web.get(f"/strava/{KEY}", params=validation())
    assert r.status_code == 200 and r.json() == {"hub.challenge": "abc"}


def test_wrong_verify_token_is_refused():
    web, _ = setup()
    assert web.get(f"/strava/{KEY}", params=validation(token="nope")).status_code == 403
    assert web.get(f"/strava/{KEY}", params=validation(mode="unsubscribe")).status_code == 403


def test_wrong_path_key_is_not_found_and_queues_nothing():
    web, queued = setup()
    assert web.get("/strava/guess", params=validation()).status_code == 404
    assert web.post("/strava/guess", json=EVENT).status_code == 404
    assert queued == []


def test_event_is_acknowledged_and_queued():
    web, queued = setup()
    r = web.post(f"/strava/{KEY}", json=EVENT)
    assert r.status_code == 200
    assert queued == [EVENT]


def test_malformed_event_is_rejected():
    web, queued = setup()
    assert web.post(f"/strava/{KEY}", json={"object_id": "x"}).status_code == 422
    bad = EVENT | {"aspect_type": "explode"}
    assert web.post(f"/strava/{KEY}", json=bad).status_code == 422
    assert queued == []
