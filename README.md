# strava-sync

Mirrors your Strava activities into a [life-data](https://github.com/alexjmiller5/life-data)
hub's `cardio_workouts` table, on [Modal](https://modal.com): a Strava webhook
for near-instant sync, a paced one-time backfill, and a daily reconcile for
edits Strava sends no webhook for. Raw API responses are retained verbatim in
the hub's file service and GPS/heart-rate samples go to a hub stream.

## Layout

```
app.py            Modal shim: image, secret, state Dict, endpoints, cron
src/core/         business logic (plain Python, portable)
scripts/          authorize.py (OAuth consent), flagged_activities.py, secrets sync
tests/            pytest
.env.tpl          secrets manifest (1Password op:// refs, committed)
```

## Configuration

| Variable | What |
|---|---|
| `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET` | Your Strava API application ([strava.com/settings/api](https://www.strava.com/settings/api)) |
| `STRAVA_REFRESH_TOKEN` | From `scripts/authorize.py`; the app keeps the newest rotated one in its Modal Dict |
| `STRAVA_VERIFY_TOKEN` | Any long random string: the webhook verify token and the callback path secret |
| `LIFE_HUB_URL`, `LIFE_HUB_TOKEN` | Your hub and a credential enrolled for this app |
| `RECONCILE_DAYS` | Optional, default 14: how far back the daily reconcile re-reads |

The hub credential needs table read/write for `cardio_workouts` and
`provenance` (broad `tables:read` + `tables:write` while the hub's narrow
write grants cannot carry that table's invariants), `streams:append:cardio_strava`,
and `files:read:raw/strava/` + `files:write:raw/strava/`.

## Setup (one time)

1. Create a Strava API application at strava.com/settings/api (any website;
   Authorization Callback Domain `localhost`). Store the client id and secret.
2. Generate `STRAVA_VERIFY_TOKEN` (`openssl rand -hex 24`) and enroll a hub
   credential for the app.
3. Consent: `op run --env-file=.env.tpl -- uv run scripts/authorize.py`, open the
   URL while signed in to Strava, approve "View data about your private
   activities", paste the redirect address back. Store the printed refresh token.
4. Deploy (push to `main`; CI syncs the Modal secret and deploys).
5. `just run subscribe` - registers the webhook at the deployed URL.
6. `just run backfill` - imports history under Strava's read limits
   (100 per 15 minutes, 1,000 per day); the daily cron finishes it.

A new Strava account or a revoked app repeats steps 3-5. The Strava API terms
restrict caching API data; keep Strava's own bulk export as the permanent archive.

## Development

`just test`, `just check`, `just fmt`. Tests run offline against in-memory
fakes of Strava and the hub.
