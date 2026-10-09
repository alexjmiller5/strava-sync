"""Modal deployment shim - ALL infrastructure lives here, as code.

Business logic stays in src/core/ (plain Python, no Modal imports). This file
only maps that logic onto Modal: image, secrets, state, endpoints, schedules.
"""

import modal

APP_NAME = "strava-sync"  # also the Modal secret name (see justfile sync-secrets)

app = modal.App(APP_NAME)

image = (
    modal.Image.debian_slim(python_version="3.13")
    .uv_sync(extra_options="--no-dev")  # reads pyproject.toml + uv.lock; skip dev group
    # add_local_dir, NOT add_local_python_source: the latter can't resolve
    # packages under src/ layout, and this also carries non-.py data files.
    .add_local_dir("src/core", remote_path="/root/core", ignore=["**/__pycache__"])
)

secrets = [modal.Secret.from_name(APP_NAME)]

# Operational state owned by this app: the newest rotated Strava refresh token
# and the backfill cursor. Entries expire after 7 idle days; the daily cron keeps
# them warm, and a lost token falls back to the STRAVA_REFRESH_TOKEN seed.
state = modal.Dict.from_name(f"{APP_NAME}-state", create_if_missing=True)


def _sync():
    from core.config import Settings
    from core.hub import Hub
    from core.strava import Strava
    from core.sync import Sync

    s = Settings()
    strava = Strava(s.strava_client_id, s.strava_client_secret, s.strava_refresh_token, state)
    return Sync(strava, Hub(s.life_hub_url, s.life_hub_token)), s


# One worker at a time: events for the same activity never race each other.
@app.function(image=image, secrets=secrets, timeout=1800, max_containers=1)
def process(event: dict) -> str:
    """Background worker - .spawn()ed from the webhook. spawn() IS the queue."""
    from core.strava import BudgetExhausted

    sync, _ = _sync()
    try:
        return sync.handle(event)
    except BudgetExhausted:
        return "deferred to the daily reconcile"  # read budget spent for today


@app.function(image=image, secrets=secrets)
@modal.asgi_app()
def web():
    """Strava's webhook callback (GET challenge + POST events) at /strava/<verify token>.

    Public at the Modal edge because Strava cannot send proxy-auth headers;
    the secret path key authenticates it (core/web.py)."""
    from core.config import Settings
    from core.web import create_app

    return create_app(Settings().strava_verify_token, process.spawn)


# Modal cron is the preferred home for schedules (Starter plan: 5 deployed
# crons TOTAL across all apps - overflow to GHA cron / CF Cron Triggers).
@app.function(image=image, secrets=secrets, schedule=modal.Cron("15 10 * * *"), timeout=86400)
def daily() -> dict:
    """Reconcile recent activities, then continue an unfinished backfill."""
    sync, s = _sync()
    out = {"reconcile": sync.reconcile(s.reconcile_days)}
    if state.get("backfill") == "running":
        out["backfill"] = sync.backfill(state)
    return out


@app.function(image=image, secrets=secrets, timeout=86400)
def backfill() -> dict:
    """One-time import of every activity, paced under Strava's read limits.
    Stops when the daily budget is spent; the daily cron resumes it."""
    sync, _ = _sync()
    return sync.backfill(state)


@app.function(image=image, secrets=secrets, timeout=86400)
def reconcile(days: int = 0) -> dict:
    """Re-read activities started in the last `days` (default RECONCILE_DAYS)."""
    sync, s = _sync()
    return sync.reconcile(days or s.reconcile_days)


@app.function(image=image, secrets=secrets)
def subscribe() -> int:
    """Point Strava's push subscription at the DEPLOYED web endpoint."""
    import httpx

    from core.config import Settings
    from core.strava import Strava

    s = Settings()
    url = modal.Function.from_name(APP_NAME, "web").get_web_url()
    callback = f"{url}/strava/{s.strava_verify_token}"
    # Strava validates the callback within 2 s during creation: warm it first.
    params = {
        "hub.mode": "subscribe",
        "hub.challenge": "warm",
        "hub.verify_token": s.strava_verify_token,
    }
    httpx.get(callback, params=params, timeout=60).raise_for_status()
    strava = Strava(s.strava_client_id, s.strava_client_secret, s.strava_refresh_token, state)
    return strava.subscribe(callback, s.strava_verify_token)
