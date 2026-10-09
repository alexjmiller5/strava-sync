"""Strava webhook receiver: answer the subscription challenge, queue events, ack fast.

Strava cannot send Modal proxy-auth headers, so the endpoint is public at the
edge and authenticated by the secret path key (the subscription verify token).
Events carry ids only; the worker re-reads everything from the Strava API.
"""

import hmac
from collections.abc import Callable
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel


class Event(BaseModel):
    object_type: Literal["activity", "athlete"]
    object_id: int
    aspect_type: Literal["create", "update", "delete"]
    updates: dict = {}
    owner_id: int
    subscription_id: int
    event_time: int


def create_app(verify_token: str, enqueue: Callable[[dict], object]) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def check(key: str):
        if not hmac.compare_digest(key.encode(), verify_token.encode()):
            raise HTTPException(404)

    @app.get("/strava/{key}")
    def challenge(
        key: str,
        mode: str = Query(alias="hub.mode"),
        challenge: str = Query(alias="hub.challenge"),
        token: str = Query(alias="hub.verify_token"),
    ):
        check(key)
        if mode != "subscribe" or not hmac.compare_digest(token.encode(), verify_token.encode()):
            raise HTTPException(403)
        return {"hub.challenge": challenge}

    @app.post("/strava/{key}")
    def event(key: str, event: Event):
        check(key)
        enqueue(event.model_dump())
        return {"ok": True}

    return app
