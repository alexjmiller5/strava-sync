# strava-sync

Imports a Strava bulk export ("Download your account" zip) into a
[soma](https://github.com/alexjmiller5/soma) hub's `cardio_workouts` table:
one row per activity (race results recorded earlier are enriched, never
duplicated), every original FIT/GPX/TCX file and the `activities.csv`
retained verbatim in the hub's file service, and GPS / heart-rate samples
appended to a hub stream. Re-importing a later export only updates rows that
changed.

Strava's API needs a paid subscription; the bulk export is free.

## Usage

1. Request your archive: strava.com > Settings > My Account > Download or
   Delete Your Account > Get Started > Request download. Strava emails a link
   within a few hours; download the zip while signed in to Strava.
2. Import it:

   ```bash
   export SOMA_HUB_URL=https://<your hub>  SOMA_HUB_TOKEN=<credential>
   nix run github:alexjmiller5/strava-sync -- import ~/Downloads/export_12345.zip
   ```

   or from a checkout, `uv run strava-sync import <zip>`. The command prints a
   JSON summary (rows inserted / enriched / updated / unchanged, files,
   samples, and the ids of `missing` activities).

| Option | What |
|---|---|
| `--timezone <IANA zone>` | Zone for the local date of activities whose file records no local time (GPX, TCX, manual entries). Default: this machine's. FIT files carry their own. |
| `--prune` | Soft-delete Strava rows whose activity is absent from this export (deleted on Strava). Race rows are flagged for review instead. |

## Hub credential

Enroll a profile holding exactly `tables:read:cardio_workouts`,
`tables:write:cardio_workouts`, `provenance:create:cardio_workouts`,
`streams:append:cardio_strava`, `files:read:raw/strava/` and
`files:write:raw/strava/`:
`soma login --profile <id> --name "Strava Sync" --start pending.json`,
approve the printed URL, then `soma login --claim pending.json --wait` prints
the token for `SOMA_HUB_TOKEN`.

## Development

`just test`, `just check`, `just fmt`. Tests run offline against an in-memory
hub and generated export zips. `scripts/flagged_activities.py` lists imported
rows whose notes flag an incomplete recording.
