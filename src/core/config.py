"""Settings from env vars - Modal Secret in the cloud, `op run` locally.

One field per line in .env.tpl. Instantiate Settings() inside functions,
not at import time, so tests can run without secrets.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    strava_client_id: str
    strava_client_secret: str
    # Seed only: the newest rotated refresh token lives in the app's state Dict.
    strava_refresh_token: str
    # Random secret: Strava's subscription verify token AND the callback path key.
    strava_verify_token: str
    soma_hub_url: str
    soma_hub_token: str
    # Strava has no "changed since" filter and fires no webhook for description
    # edits or crops, so the daily reconcile re-reads activities started this recently.
    reconcile_days: int = 14
