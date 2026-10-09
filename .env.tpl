# Canonical secrets manifest - 1Password secret references only, SAFE to commit.
# Import an export: just run <zip>   (op run with this file around `strava-sync import`)
#
# ALL of this app's env vars are fields of ONE item titled "Strava Sync ENV"
# in the project vault (field name = var name). SOMA_HUB_TOKEN is the hub
# credential enrolled for the strava-sync-v1 profile. Never write a literal
# secret reference in a comment: `op inject` parses them.
SOMA_HUB_URL=op://Strava Sync/Strava Sync ENV/SOMA_HUB_URL
SOMA_HUB_TOKEN=op://Strava Sync/Strava Sync ENV/SOMA_HUB_TOKEN
