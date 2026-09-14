"""Issue snapshot sources: where the supervisor gets the one issue a run triages.

The model never talks to Linear. The service fetches a read-only snapshot of
the named issue, stores it with the run, and pipes it to the CLI on stdin.
Two sources exist:

- :class:`FixtureSource` reads ``<dir>/<IDENTIFIER>.json``; tests and the
  real-CLI smoke test use it with a synthetic issue.
- :class:`LinearSource` queries the Linear GraphQL API with a personal API key
  read from a user-only file. The key is used for the request and nothing
  else: it is never written into the snapshot, the run, or a log.

Both raise :class:`SnapshotError` with the identifier, the reason, and the
inspection time, which becomes the failed run's explanation.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from omakron.runner import write_durably
from omakron.triage import normalize_identifier

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
LINEAR_TIMEOUT_S = 30
MAX_DESCRIPTION_CHARS = 20_000
MAX_COMMENTS = 20
MAX_COMMENT_CHARS = 4_000

ISSUE_QUERY = """
query OmakronIssue($id: String!) {
  issue(id: $id) {
    identifier
    url
    title
    description
    priorityLabel
    state { name }
    labels { nodes { name } }
    createdAt
    updatedAt
    team { key labels { nodes { name } } }
    comments(first: 20) {
      nodes { body createdAt user { name } }
    }
  }
}
"""


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class SnapshotError(Exception):
    """The named issue could not be turned into a snapshot."""

    def __init__(self, identifier: str, kind: str, reason: str):
        self.identifier = identifier
        self.kind = kind  # unknown | inaccessible | misconfigured | malformed
        self.reason = reason
        self.inspected_at = _now()
        super().__init__(f"issue {identifier}: {reason} (inspected {self.inspected_at})")


def snapshot_digest(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode("utf-8")).hexdigest()


def validate_snapshot(obj: object) -> list[str]:
    """Structural rules a snapshot must meet before it is piped to the model."""
    if not isinstance(obj, dict):
        return ["snapshot is not a JSON object"]
    problems = []
    if obj.get("snapshot_kind") != "linear_issue":
        problems.append("snapshot_kind is not linear_issue")
    if not isinstance(obj.get("team_labels"), list):
        problems.append("team_labels is not a list")
    issue = obj.get("issue")
    if not isinstance(issue, dict):
        return [*problems, "issue is not an object"]
    for key in ("identifier", "url", "title", "state", "priority", "labels", "updatedAt"):
        if key not in issue:
            problems.append(f"issue.{key} missing")
    return problems


class FixtureSource:
    """``<dir>/<IDENTIFIER>.json`` holds a ready-made snapshot; anything else is unknown."""

    kind = "fixture"

    def __init__(self, directory: Path | str):
        self.directory = Path(directory)

    def fetch(self, identifier: str) -> dict[str, Any]:
        path = self.directory / f"{identifier}.json"
        if not path.is_file():
            raise SnapshotError(identifier, "unknown", f"no issue {identifier} in {self.directory}")
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SnapshotError(identifier, "malformed", f"fixture unreadable: {exc}") from exc
        problems = validate_snapshot(obj)
        if problems:
            raise SnapshotError(identifier, "malformed", "; ".join(problems))
        obj = dict(obj)
        obj["fetched_at"] = _now()
        obj["requested_identifier"] = identifier
        obj["source"] = self.kind
        return obj


INBOX_MAX_AGE_S = 3600


class InboxSource:
    """Explicit imports from the connected Linear session; never pretend to be live lookup."""

    kind = "inbox"

    def __init__(self, directory: Path):
        self.directory = directory

    def accept(self, snapshot: object) -> dict:
        problems = validate_snapshot(snapshot)
        if problems:
            raise ValueError("; ".join(problems))
        identifier = normalize_identifier(snapshot["issue"]["identifier"])
        if snapshot.get("synthetic") is not False:
            raise ValueError("the pilot inbox requires an explicitly non-synthetic snapshot")
        if snapshot.get("source") != "linear_connector":
            raise ValueError("source must identify the explicit linear_connector import")
        self._fresh(identifier, snapshot)
        value = dict(snapshot, requested_identifier=identifier, imported_at=_now())
        value["issue"] = dict(snapshot["issue"], identifier=identifier)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_durably(
            self.directory / f"{identifier}.json",
            (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
        return {
            "identifier": identifier,
            "fetched_at": value["fetched_at"],
            "imported_at": value["imported_at"],
            "expires_after_seconds": INBOX_MAX_AGE_S,
        }

    @staticmethod
    def _fresh(identifier: str, snapshot: dict) -> None:
        try:
            fetched = dt.datetime.fromisoformat(snapshot["fetched_at"].replace("Z", "+00:00"))
            age = (dt.datetime.now(dt.UTC) - fetched).total_seconds()
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise SnapshotError(identifier, "malformed", "fetched_at must have a timezone") from exc
        if not 0 <= age <= INBOX_MAX_AGE_S:
            raise SnapshotError(
                identifier,
                "stale",
                "import is stale or future-dated; fetch and import this issue again",
            )

    def fetch(self, identifier: str) -> dict:
        path = self.directory / f"{normalize_identifier(identifier)}.json"
        if not path.is_file():
            raise SnapshotError(
                identifier,
                "unknown",
                "no imported snapshot; fetch this issue through connected Linear, then import it",
            )
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise SnapshotError(
                identifier, "malformed", "imported snapshot cannot be read"
            ) from exc
        problems = validate_snapshot(value)
        if problems or value["issue"]["identifier"] != identifier:
            raise SnapshotError(
                identifier, "malformed", "imported snapshot does not match the requested issue"
            )
        self._fresh(identifier, value)
        return value


Transport = Callable[[bytes, dict[str, str]], tuple[int, bytes]]


def _urllib_transport(body: bytes, headers: dict[str, str]) -> tuple[int, bytes]:
    request = urllib.request.Request(LINEAR_GRAPHQL_URL, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=LINEAR_TIMEOUT_S) as response:  # noqa: S310
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class LinearSource:
    """Read-only single-issue lookup against the Linear GraphQL API."""

    kind = "linear"

    def __init__(self, api_key_file: Path | str, transport: Transport | None = None):
        self.api_key_file = Path(api_key_file)
        self._transport = transport or _urllib_transport

    def _api_key(self, identifier: str) -> str:
        try:
            key = self.api_key_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            raise SnapshotError(
                identifier, "misconfigured", f"Linear API key file not found: {self.api_key_file}"
            ) from exc
        except OSError as exc:
            raise SnapshotError(
                identifier, "misconfigured", f"Linear API key file unreadable: {exc}"
            ) from exc
        if not key or any(ch.isspace() for ch in key):
            raise SnapshotError(
                identifier, "misconfigured", f"Linear API key file is empty: {self.api_key_file}"
            )
        return key

    def fetch(self, identifier: str) -> dict[str, Any]:
        key = self._api_key(identifier)
        body = json.dumps({"query": ISSUE_QUERY, "variables": {"id": identifier}}).encode("utf-8")
        headers = {"Content-Type": "application/json", "Authorization": key}
        try:
            status, raw = self._transport(body, headers)
        except (OSError, urllib.error.URLError) as exc:
            raise SnapshotError(
                identifier, "inaccessible", f"Linear request failed: {exc}"
            ) from exc
        finally:
            del key, headers
        if status in (401, 403):
            raise SnapshotError(
                identifier, "inaccessible", f"Linear refused the request ({status})"
            )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SnapshotError(
                identifier, "inaccessible", f"Linear answered {status} with non-JSON"
            ) from exc
        if status != 200:
            raise SnapshotError(identifier, "inaccessible", f"Linear answered {status}")
        issue = (payload.get("data") or {}).get("issue")
        if issue is None:
            messages = [e.get("message", "") for e in payload.get("errors") or []]
            text = "; ".join(m for m in messages if m) or "no such issue"
            raise SnapshotError(identifier, "unknown", f"Linear has no issue {identifier}: {text}")
        return self._shape(identifier, issue)

    @staticmethod
    def _shape(identifier: str, issue: dict[str, Any]) -> dict[str, Any]:
        def names(connection: Any) -> list[str]:
            nodes = (connection or {}).get("nodes") or []
            return [n.get("name", "") for n in nodes if isinstance(n, dict)]

        description = issue.get("description") or ""
        truncated = len(description) > MAX_DESCRIPTION_CHARS
        comments = []
        for node in ((issue.get("comments") or {}).get("nodes") or [])[:MAX_COMMENTS]:
            body = node.get("body") or ""
            comments.append(
                {
                    "author": (node.get("user") or {}).get("name"),
                    "body": body[:MAX_COMMENT_CHARS],
                    "truncated": len(body) > MAX_COMMENT_CHARS,
                    "createdAt": node.get("createdAt"),
                }
            )
        return {
            "snapshot_kind": "linear_issue",
            "synthetic": False,
            "source": "linear",
            "fetched_at": _now(),
            "requested_identifier": identifier,
            "team_labels": sorted(set(names((issue.get("team") or {}).get("labels")))),
            "issue": {
                "identifier": issue.get("identifier"),
                "url": issue.get("url"),
                "title": issue.get("title"),
                "description": description[:MAX_DESCRIPTION_CHARS],
                "description_truncated": truncated,
                "state": (issue.get("state") or {}).get("name"),
                "priority": issue.get("priorityLabel") or "None",
                "labels": names(issue.get("labels")),
                "createdAt": issue.get("createdAt"),
                "updatedAt": issue.get("updatedAt"),
                "comments": comments,
            },
        }


def source_from_settings(
    spec: dict[str, Any], config_dir: Path
) -> FixtureSource | LinearSource | InboxSource:
    """Build the source described by ``settings.snapshot_source``.

    Unknown kinds fail at startup, not at run time.
    """
    kind = spec.get("kind", "linear")
    if kind == "inbox":
        return InboxSource(config_dir / "inbox")
    if kind == "fixture":
        directory = spec.get("dir")
        if not directory:
            raise ValueError("snapshot_source.dir is required for the fixture source")
        return FixtureSource(Path(directory).expanduser())
    if kind == "linear":
        key_file = spec.get("api_key_file") or str(config_dir / "linear-api-key")
        return LinearSource(Path(key_file).expanduser())
    raise ValueError(f"unknown snapshot_source.kind {kind!r}")
