"""Strava API v3 client: refresh-token rotation, read-limit pacing, raw bytes kept."""

import time
from collections.abc import MutableMapping

import httpx
import structlog

API = "https://www.strava.com/api/v3"
TOKEN_URL = "https://www.strava.com/oauth/token"
STREAM_KEYS = (
    "time,distance,latlng,altitude,velocity_smooth,heartrate,cadence,watts,temp,moving,grade_smooth"
)
WINDOW = 900  # Strava's short read window: 15 minutes, resetting on the quarter hour

log = structlog.get_logger()


class BudgetExhausted(RuntimeError):
    """The daily read budget is spent; Strava resets it at midnight UTC."""


class StravaAuthError(RuntimeError):
    """Every refresh token was refused: re-run scripts/authorize.py."""


class Strava:
    per_page = 200

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        seed_refresh_token: str,
        state: MutableMapping,
        http: httpx.Client | None = None,
        sleep=time.sleep,
        now=time.time,
        margin: int = 5,
    ):
        self.client_id, self.client_secret, self.seed = client_id, client_secret, seed_refresh_token
        self.state, self.sleep, self.now, self.margin = state, sleep, now, margin
        self.http = http or httpx.Client(timeout=60)
        self.usage = None  # (short, daily, short limit, daily limit) from the last read

    # --- auth -------------------------------------------------------------

    def refresh(self) -> str:
        stored = (self.state.get("token") or {}).get("refresh_token")
        for refresh_token in dict.fromkeys(t for t in (stored, self.seed) if t):
            r = self.http.post(
                TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
            )
            if r.status_code in (400, 401):
                continue
            r.raise_for_status()
            body = r.json()
            # Strava may rotate the refresh token: always keep the newest one.
            self.state["token"] = {
                k: body[k] for k in ("access_token", "expires_at", "refresh_token")
            }
            return body["access_token"]
        raise StravaAuthError("Strava refused every refresh token; re-run scripts/authorize.py")

    def _access_token(self) -> str:
        token = self.state.get("token") or {}
        if token.get("access_token") and token.get("expires_at", 0) > self.now() + 60:
            return token["access_token"]
        return self.refresh()

    # --- pacing -----------------------------------------------------------

    def _to_next_window(self):
        self.sleep(WINDOW - int(self.now()) % WINDOW + 5)
        if self.usage:
            self.usage = (0, *self.usage[1:])

    def _wait(self):
        if not self.usage:
            return
        short, daily, short_limit, daily_limit = self.usage
        if daily >= daily_limit - self.margin:
            raise BudgetExhausted("daily read budget spent")
        if short >= short_limit - self.margin:
            log.info("strava.pacing", short=short, short_limit=short_limit)
            self._to_next_window()

    def _record(self, r: httpx.Response):
        usage, limit = (
            r.headers.get("x-readratelimit-usage"),
            r.headers.get("x-readratelimit-limit"),
        )
        if usage and limit:
            self.usage = (*map(int, usage.split(",")), *map(int, limit.split(",")))

    # --- requests ---------------------------------------------------------

    def _request(self, method, path, **kw) -> httpx.Response:
        refreshed = throttled = False
        while True:
            self._wait()
            headers = {"Authorization": f"Bearer {self._access_token()}"}
            r = self.http.request(method, f"{API}{path}", headers=headers, **kw)
            self._record(r)
            if r.status_code == 401 and not refreshed:
                refreshed = True
                self.refresh()
                continue
            if r.status_code == 429 and not throttled:
                throttled = True
                if self.usage and self.usage[1] >= self.usage[3] - self.margin:
                    raise BudgetExhausted("daily read budget spent")
                self._to_next_window()
                continue
            return r

    def _get(self, path, params=None) -> tuple[object, bytes] | None:
        r = self._request("GET", path, params=params)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json(), r.content

    def activity(self, activity_id):
        return self._get(f"/activities/{activity_id}")

    def streams(self, activity_id):
        params = {"keys": STREAM_KEYS, "key_by_type": "true"}
        return self._get(f"/activities/{activity_id}/streams", params)

    def laps(self, activity_id):
        return self._get(f"/activities/{activity_id}/laps")

    def activities(self, *, before=None, after=None, page=1) -> list[dict]:
        params = {"per_page": self.per_page, "page": page}
        params |= {k: v for k, v in (("before", before), ("after", after)) if v is not None}
        return self._get("/athlete/activities", params)[0]

    # --- webhook subscription (app credentials, not the athlete token) ----

    def subscribe(self, callback_url: str, verify_token: str) -> int:
        """Point the app's single push subscription at callback_url; returns its id."""
        app = {"client_id": self.client_id, "client_secret": self.client_secret}
        url = f"{API}/push_subscriptions"
        existing = self.http.get(url, params=app)
        existing.raise_for_status()
        for sub in existing.json():
            if sub["callback_url"] == callback_url:
                return sub["id"]
            self.http.delete(f"{url}/{sub['id']}", params=app).raise_for_status()
        r = self.http.post(
            url, data=app | {"callback_url": callback_url, "verify_token": verify_token}
        )
        r.raise_for_status()
        return r.json()["id"]
