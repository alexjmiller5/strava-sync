"""strava-sync import <zip>: one Strava bulk export into a soma hub's cardio_workouts."""

import argparse
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from strava_sync.config import Settings
from strava_sync.export import Export
from strava_sync.hub import Hub
from strava_sync.sync import Sync


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="strava-sync", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    imp = commands.add_parser("import", help="import a Strava bulk export zip")
    imp.add_argument("zip", type=Path, help="the export zip Strava emailed a link to")
    imp.add_argument(
        "--timezone",
        help="IANA zone for the local date of activities whose file records no local time "
        "(default: this machine's)",
    )
    imp.add_argument(
        "--prune",
        action="store_true",
        help="soft-delete Strava rows absent from this export (an export is complete, so "
        "absence means the activity was deleted on Strava)",
    )
    args = parser.parse_args(argv)
    s = Settings()
    zone = ZoneInfo(args.timezone) if args.timezone else None
    sync = Sync(Hub(s.soma_hub_url, s.soma_hub_token), zone=zone)
    with Export(args.zip) as export:
        print(json.dumps(sync.run(export, prune=args.prune), indent=2))


if __name__ == "__main__":
    main()
