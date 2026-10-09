# Canonical secrets manifest - 1Password secret references only, SAFE to commit.
# Local dev:       op run --env-file=.env.tpl -- <cmd>   (see justfile)
# Push to Modal:   just sync-secrets
#
# ALL of this app's env vars are fields of ONE item titled "Strava Sync ENV"
# in the project vault (field name = var name). Never write a literal secret
# reference in a comment: `op inject` parses them.
STRAVA_CLIENT_ID=op://Strava Sync/Strava Sync ENV/STRAVA_CLIENT_ID
STRAVA_CLIENT_SECRET=op://Strava Sync/Strava Sync ENV/STRAVA_CLIENT_SECRET
STRAVA_REFRESH_TOKEN=op://Strava Sync/Strava Sync ENV/STRAVA_REFRESH_TOKEN
STRAVA_VERIFY_TOKEN=op://Strava Sync/Strava Sync ENV/STRAVA_VERIFY_TOKEN
SOMA_HUB_URL=op://Strava Sync/Strava Sync ENV/LIFE_HUB_URL
SOMA_HUB_TOKEN=op://Strava Sync/Strava Sync ENV/LIFE_HUB_TOKEN
