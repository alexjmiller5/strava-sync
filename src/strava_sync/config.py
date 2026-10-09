"""Settings from env vars (SOMA_HUB_URL, SOMA_HUB_TOKEN). Instantiate Settings() inside
functions, not at import time, so tests run without secrets."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    soma_hub_url: str
    soma_hub_token: str
