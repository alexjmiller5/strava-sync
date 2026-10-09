# AGENTS.md

Strava Sync - mirrors one athlete's Strava activities into the soma
`cardio_workouts` table: a webhook for near-instant sync, a paced one-time
backfill, and a daily reconcile. Python on Modal.

## Architecture rule (the one that matters)

**Business logic lives in `src/core/` as plain Python with NO Modal imports.**
Only `app.py` imports `modal` - it is the deployment shim (image, secrets,
state, endpoints, schedules).

- `core/web.py` - webhook receiver. Answers Strava's subscription challenge,
  validates the event, `process.spawn(event)`s it and returns 200 (Strava wants
  an ack within 2 s; events carry ids only). Modal's spawn IS the queue.
- `core/strava.py` - API client: refresh-token rotation, read-limit pacing
  from Strava's `X-ReadRateLimit-*` headers (sleep to the next 15-minute
  window near 100 reads; stop with `BudgetExhausted` near 1,000/day), raw
  response bytes kept for retention.
- `core/sync.py` - activity -> row mapping, race-row enrichment, telemetry,
  delete handling, backfill, reconcile.
- `core/hub.py` - soma hub API client (rows pull/push, files, streams).

## Data contract (soma is the record; read its catalog first)

The `cardio_workouts` contract lives in the soma catalog (soma-map
`references/schema.md`). What this app does with it:

- One row per activity, id `strava/activity/<id>`, `recording_source`
  `Strava`, `external_id` = the Strava activity id. Lookups go by
  `external_id` first; a row soft-deleted in soma is never resurrected.
- A Running activity on the date of an unlinked race row enriches that row
  when Strava marks it a race (`workout_type` 1) or its distance is within 15%
  of `race_distance_meters`. Only device fields (`DEVICE` in sync.py) are
  written to race rows; name, date, notes and every `race_*` field stay
  official. Several candidates, or a race row with no course distance, insert
  a separate row with `needs_review` naming the candidates.
- Values are written only when present and different: a missing provider
  value never erases a known one, `needs_review` is set only on insert, pace
  and speed are never stored. `notes` = Strava title + description on rows
  this app created (the strip/clip review reads them).
- Sport types: Run/TrailRun/VirtualRun -> Running, *Ride -> Biking, Walk,
  Hike, Swim, Rowing/VirtualRow, Elliptical, StairStepper -> Stairmaster;
  everything else -> Other (named after its Strava sport type).
- Provenance: one edge per row, `from_kind` `takeout`, `from_ref`
  `raw/strava/<id>/` (all retained versions), `asserted_by`
  `script:strava-sync`; `imported_from` + `detail.created_row = 1` for rows
  it created, `evidence_of` for race rows it enriched. Edge before row.
- Raw originals are written FIRST, verbatim and content-addressed:
  `raw/strava/<id>/{activity,streams,laps}-<sha256>.json` through
  `PUT /v1/files/` with `If-None-Match: *` (412 = already retained).
- Point samples: stream `cardio_strava`, one record per activity + sample key
  `{cardio_workout_id, activity_id, start_tst, key, series_type,
  original_size, resolution, data}`; records over 900 kB split into parts
  with `offset`/`parts`. Re-sent only for new rows or crops (duration or
  distance changed): dedupe on `(activity_id, key[, offset])`, latest
  `ingested_at` wins.
- Deletes: a webhook delete is unsigned, so the row is soft-deleted only after
  `GET /activities/<id>` returns 404. A deleted recording of a race row sets
  `needs_review` instead of deleting the race.

## Owned resources

- Modal app `strava-sync`, secret `strava-sync` (synced from `.env.tpl`),
  Dict `strava-sync-state` (the newest rotated refresh token + backfill
  cursor), and ONE Modal cron slot (`daily`, 10:15 UTC).
- 1Password vault `Strava Sync`: `Strava Sync ENV`, `Strava Sync CI Modal
  Token`, `Strava Sync CI op Service Account Token` (SA `strava-sync-ci`).
- A Strava API application on the athlete's own Strava account (client id
  and secret in the ENV item), with exactly one push subscription.
- Approved shared-service use: the soma hub through its API with this
  app's own enrolled profile credential (`SOMA_HUB_TOKEN`, profile
  `strava-sync-v1`), file prefix `raw/strava/`, stream `cardio_strava`.
  Never soma's D1/R2 bindings or another consumer's token.

## Strava API policy caveat

Strava's API Agreement (the June 2026 policy, including its 7-day cache
clause) restricts storing and reusing API data. Alex chose to mirror his own
activities through the API anyway; Strava's official bulk export ("Download
your data") zip is the sanctioned permanent archive alongside it. A Strava
enforcement change can revoke the app: the soma rows and retained raw
files stay, and a fresh bulk export covers anything missed.

## Endpoint auth (exception to the template rule)

Strava cannot send Modal proxy-auth headers, so `web` is public at the Modal
edge. It is authenticated by the secret path key `/strava/<STRAVA_VERIFY_TOKEN>`
(constant-time compare); a wrong key is a 404. Rotating the verify token =
update the ENV field, deploy, `just run subscribe`.

## Commands

| Command | Purpose |
|---|---|
| `just test` / `just check` / `just fmt` | pytest / ruff read-only / ruff fix |
| `just dev` | `modal serve` against real infra |
| `just logs` | Stream deployed-app logs |
| `just run subscribe` | Point Strava's push subscription at the deployed `web` URL (replaces an old one) |
| `just run backfill` | One-time import of every activity, newest first; pauses at the daily budget and the daily cron resumes it |
| `just sync-secrets` / `just deploy` | CI's job (below) |

**Deploying = commit + push to `main`.** The GHA deploy workflow runs tests,
syncs secrets and deploys; watch it with `gh run watch <id> --exit-status`.

One-offs in `scripts/`: `authorize.py` (OAuth consent -> refresh token,
scope `activity:read_all`), `flagged_activities.py` (lists Strava rows whose
description flags an incomplete recording; `--pattern` overrides the cues).

## Gotchas

- Strava fires no webhook for description edits or crops, and lists have no
  "changed since" filter: the daily reconcile re-reads every activity started
  in the last `RECONCILE_DAYS` (default 14), inserts missed ones and deletes
  vanished ones (404-verified). Edits to older activities need a wider manual
  pass: `modal run --detach app.py::reconcile --days 365`.
- Modal Dict entries expire after 7 idle days. The daily cron keeps them warm;
  if the token entry is lost the client falls back to the `STRAVA_REFRESH_TOKEN`
  seed, and if Strava refuses both, re-run `scripts/authorize.py`.
- A cold `web` container can miss Strava's 2 s ack: Strava retries, writes are
  idempotent, and the reconcile catches the rest. `subscribe` warms the
  endpoint before creating the subscription for the same reason.
- `process` runs one container at a time so events for one activity never
  race. Backfill and reconcile may overlap it; upserts are idempotent.
- Hub pushes must carry every write: a rejected row raises, nothing is
  silently dropped. A rejection is the catalog contract working - fix the
  value or the rule, never route around it.

## TDD

Write the test in `tests/` first (`tests/fakes.py` holds in-memory Strava and
hub fakes), then the `src/core/` code. `app.py` shim functions stay thin.

## Hardcoded owner assumptions

The code is generic; the workflow is wired to Alex's setup for convenience:
secrets flow through his 1Password (`.env.tpl`; `op-project-bootstrap` is his
private bootstrap script) and deploys target his Modal workspace.
