# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.28"]
# ///
"""One-time Strava OAuth consent: prints the refresh token for the ENV item.

    op run --env-file=.env.tpl -- uv run scripts/authorize.py

Open the printed URL in the browser signed in to the athlete's Strava account,
approve, then paste the address the browser lands on (http://localhost/...?code=...;
the page itself fails to load, which is expected). Only the refresh token goes
to stdout - store it as STRAVA_REFRESH_TOKEN in the project's ENV item.
"""

import os
import sys
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

SCOPE = "activity:read_all"


def authorize_url(client_id: str) -> str:
    query = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": "http://localhost/exchange_token",
        "approval_prompt": "force",
        "scope": SCOPE,
    }
    return f"https://www.strava.com/oauth/authorize?{urlencode(query)}"


def main() -> None:
    client_id, secret = os.environ["STRAVA_CLIENT_ID"], os.environ["STRAVA_CLIENT_SECRET"]
    print(
        f"Approve in the browser:\n{authorize_url(client_id)}\nThen paste the redirect URL:",
        file=sys.stderr,
    )
    landed = parse_qs(urlparse(sys.stdin.readline().strip()).query)
    if "activity:read_all" not in landed.get("scope", [""])[0].split(","):
        sys.exit("activity:read_all was not granted; approve again with that box checked")
    r = httpx.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": client_id,
            "client_secret": secret,
            "code": landed["code"][0],
            "grant_type": "authorization_code",
        },
    )
    if r.status_code != 200:
        sys.exit(f"token exchange failed: HTTP {r.status_code}")  # body may echo credentials
    print(r.json()["refresh_token"])


if __name__ == "__main__":
    main()
