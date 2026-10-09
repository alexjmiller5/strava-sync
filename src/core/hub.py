"""soma hub client over its HTTP API. Knows a URL and a bearer token, nothing else."""

import hashlib
from collections import defaultdict
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

PUSH_CHUNK = 500
USER_AGENT = "strava-sync/0.1 (+https://github.com/alexjmiller5/strava-sync)"


class HubError(RuntimeError):
    pass


def stamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Hub:
    def __init__(self, base_url: str, token: str, http: httpx.Client | None = None):
        self.base = base_url.rstrip("/")
        self.http = http or httpx.Client(timeout=120)
        self.headers = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}

    def _send(self, method, route, ok=(200, 201), **kw) -> httpx.Response:
        try:
            r = self.http.request(
                method, f"{self.base}{route}", headers=self.headers | kw.pop("headers", {}), **kw
            )
        except httpx.HTTPError as e:
            raise HubError(f"hub unreachable: {type(e).__name__}") from e
        if r.status_code not in ok:
            raise HubError(
                f"hub {method} {route.split('?')[0]}: HTTP {r.status_code} {r.text[:300]}"
            )
        return r

    def rows(self, table: str, columns: list[str], where: dict) -> list[dict]:
        """Every row matching an equality filter, tombstones included."""
        body = {"table": table, "columns": columns, "where": where, "limit": 200}
        rows, seen = [], set()
        while True:
            page = self._send("POST", "/v1/rows/pull", json=body).json()
            rows += page["rows"]
            cursor = page.get("next_cursor")
            if cursor is None:
                return rows
            if cursor in seen:
                raise HubError("hub repeated a pull cursor")
            seen.add(cursor)
            body["after"] = cursor

    def push(self, table: str, rows: list[dict]) -> None:
        """Sparse upsert by id; only the listed columns change. Raises on any rejection."""
        now = stamp()
        groups = defaultdict(list)
        for row in rows:
            row = {"updated_at": now, **row}
            groups[tuple(sorted(row))].append(row)
        for columns, group in groups.items():
            for i in range(0, len(group), PUSH_CHUNK):
                body = {"table": table, "columns": list(columns), "rows": group[i : i + PUSH_CHUNK]}
                out = self._send("POST", "/v1/rows/push", json=body).json()
                if out.get("rejected"):
                    raise HubError(f"{table}: rejected {out['rejected'][:3]}")

    def insert(self, table: str, rows: list[dict]) -> None:
        """Insert-only by id: an existing id is left untouched. Raises on any rejection."""
        now = stamp()
        rows = [{"updated_at": now, **row} for row in rows]
        body = {"table": table, "columns": sorted({c for row in rows for c in row}), "rows": rows}
        out = self._send("POST", "/v1/rows/insert", json=body).json()
        if out.get("rejected"):
            raise HubError(f"{table}: rejected {out['rejected'][:3]}")

    def put_file(self, key: str, data: bytes) -> None:
        """Write-once retained original; an existing key (412) already holds these bytes."""
        headers = {
            "If-None-Match": "*",
            "X-Content-SHA256": hashlib.sha256(data).hexdigest(),
            "Content-Type": "application/json",
        }
        self._send(
            "PUT", f"/v1/files/{quote(key, safe='/')}", ok=(201, 412), headers=headers, content=data
        )

    def append(self, stream: str, record: dict) -> None:
        self._send("POST", f"/v1/streams/{stream}/append", json=record)
