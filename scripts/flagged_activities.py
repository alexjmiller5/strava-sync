"""List Strava-recorded cardio rows whose Strava description flags an incomplete recording.

For the strip/clip follow-up: activities where recording started late, was
stopped late, or otherwise does not match what happened. Reads the local
soma replica (`soma sql`); the Strava title and description are the
row's `notes`. Race rows enriched by a recording keep their official notes,
so their Strava description is only in the retained raw activity JSON.

Usage: uv run scripts/flagged_activities.py [--pattern REGEX]
       (or STRAVA_FLAG_PATTERN=...). Prints one JSON object per flagged row.
"""

import argparse
import json
import os
import re
import subprocess

DEFAULT_PATTERN = (
    r"forg[eo]t(?:ten)? to (?:start|stop|end|pause|turn \w+)"
    r"|start(?:ed)? (?:it |recording |the watch )?late|late start"
    r"|(?:didn'?t|never) (?:start|stop|end)"
    r"|(?:left|kept) (?:it|strava|the watch|my watch|recording)[\w ]{0,12}(?:on|running|going)"
    r"|still recording|recorded (?:too|extra)|extra (?:distance|miles|time)"
    r"|too (?:long|much|far)|incomplete|cut off|gps (?:glitch|error|drift)|(?:watch|phone) died"
    r"|real(?:ly)? (?:distance|time|was)|actual(?:ly)? (?:distance|time|was|ran)"
)

QUERY = """SELECT id, external_id, date, name, notes FROM cardio_workouts
WHERE deleted_at IS NULL AND recording_source = 'Strava' AND notes IS NOT NULL
ORDER BY date"""


def flagged(rows: list[dict], pattern: str) -> list[dict]:
    cue = re.compile(pattern, re.IGNORECASE)
    hits = []
    for row in rows:
        match = cue.search(row.get("notes") or "")
        if match:
            hits.append(
                {k: row[k] for k in ("id", "external_id", "date", "name")} | {"cue": match[0]}
            )
    return hits


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pattern", default=os.environ.get("STRAVA_FLAG_PATTERN", DEFAULT_PATTERN))
    args = parser.parse_args(argv)
    out = subprocess.run(["soma", "sql", QUERY], capture_output=True, text=True, check=True)
    for hit in flagged(json.loads(out.stdout), args.pattern):
        print(json.dumps(hit))


if __name__ == "__main__":
    main()
