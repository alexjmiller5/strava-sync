# AGENTS.md

Strava Sync - imports Strava's bulk export ("Download your account" zip) into
the soma `cardio_workouts` table, ad hoc: one row per activity, every original
file retained, point samples in a stream. A Python CLI, `strava-sync import
<zip>`; nothing is deployed.

## Why an export, not the API

Strava's API is subscriber-only: on a free account strava.com/settings/api
says "The Strava API is available to subscribers" and its Create Application
form is disabled. The bulk export is free, complete (every activity with its
original upload) and needs only the signed-in web session. A later export
re-imports idempotently, so "sync" = request an export and import it.

## Layout

- `src/strava_sync/export.py` - the zip: `activities.csv` (one row per
  activity) and the original uploads under `activities/` (FIT via fitdecode,
  GPX via gpxpy, TCX via ElementTree; `.gz` or plain) parsed into sample
  series aligned on time.
- `src/strava_sync/sync.py` - activity -> row mapping, race-row enrichment,
  provenance edges, samples, `--prune`.
- `src/strava_sync/hub.py` - soma hub API client (rows pull/push/insert,
  files, stream appends).
- `src/strava_sync/cli.py` - `strava-sync import <zip> [--timezone Z] [--prune]`.
- `scripts/flagged_activities.py` - lists Strava rows whose notes flag an
  incomplete recording (`--pattern` overrides the cues).

## Requesting and running an export

1. Request: strava.com > Settings > My Account > "Download or Delete Your
   Account" > Get Started > **Request download**
   (`https://www.strava.com/athlete/download_my_account`; "Request received."
   confirms). Strava emails a link within a few hours.
2. Download: the link needs the signed-in Strava session, so open it in the
   browser that is signed in (the agent Chrome).
3. Import: `just run <zip>` in a checkout (`op run --env-file=.env.tpl`
   supplies `SOMA_HUB_URL` / `SOMA_HUB_TOKEN`), or without a checkout
   `nix run github:alexjmiller5/strava-sync -- import <zip>` with those two
   variables in the environment. Prints a JSON summary: inserted / enriched /
   updated / unchanged / skipped rows, files, samples, `missing` activity ids.
4. After the first import of a Strava account: `uv run
   scripts/flagged_activities.py` lists rows whose notes read like an
   incomplete recording.

## Data contract (soma is the record; read its catalog first)

The `cardio_workouts` contract lives in the soma catalog (soma-map
`references/schema.md`). What this app does with it:

- One row per activity, id `strava/activity/<id>`, `recording_source`
  `Strava`, `external_id` = the Strava activity id. Lookups go by
  `external_id` (all cardio rows are pulled once per import); a row
  soft-deleted in soma is never resurrected.
- `activities.csv` gives the summary: Activity Date (UTC) -> `started_at`,
  Elapsed Time -> `duration_minutes` / `ended_at`, Distance (meters) ->
  `distance_miles`, Elevation Gain, Average / Max Heart Rate, Calories.
  Heart rate missing from the csv comes from the original file. The local
  `date` uses the FIT file's own UTC offset (activity `local_timestamp`),
  else `--timezone` (default: the machine's zone).
- A Running activity on the date of an unlinked race row enriches that row
  when its distance is within 15% of `race_distance_meters` (the csv has no
  race flag). Only device fields (`DEVICE` in sync.py) are written to race
  rows; name, date, notes and every `race_*` field stay official. Several
  candidates, or a race row with no course distance, insert a separate row
  with `needs_review` naming the candidates.
- Values are written only when present and different: a missing value never
  erases a known one, `needs_review` is set only on insert, pace and speed
  are never stored. `notes` = title + description + private note on rows
  this app created (the strip/clip review reads them).
- Activity types match with spaces and hyphens removed: Run / Trail Run /
  Virtual Run -> Running, any *Ride -> Biking, Walk, Hike, Swim, Rowing /
  Virtual Row, Elliptical, Stair-Stepper -> Stairmaster; everything else ->
  Other (named after its Strava type, e.g. "Weight Training - 1h 15min").
- Raw originals are written FIRST, verbatim and write-once (`PUT /v1/files/`
  with `If-None-Match: *`, 412 = already retained):
  `raw/strava/export/<date>/activities.csv` and
  `raw/strava/export/<date>/activities/<original file>`, `<date>` = the day
  Strava generated the export (the csv's zip timestamp).
- Provenance: one edge per row, id `takeout:raw/strava/export/:cardio_workouts:<row id>`,
  `from_kind` `takeout`, `from_ref` `raw/strava/export/` (every retained
  export), `detail.locator` `activities.csv Activity ID <id>`, `asserted_by`
  `script:strava-sync`; `imported_from` + `detail.created_row = 1` for rows
  it created, `evidence_of` for race rows it enriched. Inserted through
  `/v1/rows/insert` AFTER the rows (the `provenance:create` grant needs a
  live target) and re-sent on every import; an existing edge is never
  changed, so a crash in between heals on the next import.
- Point samples: stream `cardio_strava`, one record per activity + sample key
  `{cardio_workout_id, activity_id, start_tst, key, original_size, source,
  data}`; `start_tst` = the activity start (unix s), `time` = seconds after
  it, `latlng` = `[lat, lon]`, `altitude` and `distance` in meters, plus
  `heartrate`, `cadence`, `watts`, `temp` when recorded (null where a point
  lacks one); `source` = the retained original's key. Records over 900 kB
  split into parts with `offset`/`parts`. Sent only for new rows or crops
  (duration or distance changed): dedupe on `(activity_id, key[, offset])`,
  latest `ingested_at` wins.
- Deletes: a Strava row whose activity is absent from the export is listed in
  `missing`; `--prune` soft-deletes it (a race row gets `needs_review`
  instead, the race still happened). An export without activities never
  prunes.

## Owned resources

- 1Password vault `Strava Sync`: `Strava Sync ENV` (`SOMA_HUB_URL`,
  `SOMA_HUB_TOKEN`).
- Approved shared-service use: the soma hub through its API with this app's
  own enrolled profile credential (`SOMA_HUB_TOKEN`, profile
  `strava-sync-v1`: `tables:read:cardio_workouts`,
  `tables:write:cardio_workouts`, `provenance:create:cardio_workouts`,
  `streams:append:cardio_strava`, `files:read:raw/strava/`,
  `files:write:raw/strava/`), file prefix `raw/strava/` (registered in soma
  AGENTS.md), stream `cardio_strava`. Never soma's D1/R2 bindings or another
  consumer's token.

## Commands

| Command | Purpose |
|---|---|
| `just test` / `just check` / `just fmt` | pytest / ruff read-only / ruff fix |
| `just run <zip> [--prune] [--timezone Z]` | Import one export with the `.env.tpl` credentials |
| `nix build` / `nix flake check` | The installable package (uv2nix) and its `--help` smoke check |

CI (`.github/workflows/test.yml`) runs `check` and the tests on every push
to `main`; it deploys nothing and holds no secrets.

## Gotchas

- `activities.csv` repeats column names: the first Distance / Elapsed Time /
  Max Heart Rate are display values (km, rounded), the last ones raw units.
  The reader keeps the last and refuses a csv without both Distance and
  Elapsed Time pairs rather than misreading kilometers as meters.
- Original files are the upload as recorded. A crop in Strava changes the csv
  summary (so the row) but not the original, so samples can run past the
  row's `started_at` / `ended_at`.
- Hub round trips set the pace: a first import takes ~5 s per new activity
  (one row push plus a few stream appends); re-importing an unchanged export
  takes about a minute (write-once file PUTs answer 412, no row writes).
- A crash between a new row and its sample appends leaves that row without
  samples; the next import sees the row unchanged and does not resend them.
- Hub pushes must carry every write: a rejected row raises, nothing is
  silently dropped. A rejection is the catalog contract working - fix the
  value or the rule, never route around it.
- `~/Desktop` is iCloud on Alex's Macs: run tests with
  `UV_PROJECT_ENVIRONMENT=$HOME/.cache/uv-venvs/strava-sync
  PYTHONPYCACHEPREFIX=$HOME/.cache/pycache/strava-sync uv run python -m pytest
  -p no:cacheprovider`.

## TDD

Write the test in `tests/` first (`tests/fakes.py` holds the in-memory hub,
an export-zip builder with the real csv header, and a minimal FIT encoder),
then the `src/strava_sync/` code.

## Hardcoded owner assumptions

The code is generic; the workflow is wired to Alex's setup for convenience:
the hub credential flows through his 1Password (`.env.tpl`;
`op-project-bootstrap` is his private bootstrap script).
