#!/usr/bin/env python3
"""Precheck for `tessl__nightly-order-sync`.

Filesystem cadence cap (3-day window). Peeled off the
`nightly-external-sync` bundle so check-orders runs in its own
bounded container. See `references/cadence-rationale.md`.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CADENCE = timedelta(days=3)
DEFAULT_CURSOR_PATH = "/workspace/group/state/nightly-order-sync-cursor.json"
SUPPORTED_SCHEMA_VERSION = 1


def _parse_iso(value: str) -> datetime | None:
    try:
        normalised = value[:-1] + "+00:00" if value.endswith("Z") else value
        return datetime.fromisoformat(normalised)
    except (TypeError, ValueError):
        return None


def _read_cursor(path: Path) -> tuple[str | None, str | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"cursor read failed: {exc}"
    except UnicodeDecodeError as exc:
        return None, f"cursor not valid UTF-8: {exc}"
    if not text.strip():
        return None, None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"cursor JSON malformed: {exc}"
    if not isinstance(data, dict):
        return None, f"cursor root is {type(data).__name__}; expected object"
    schema = data.get("schema_version")
    if schema != SUPPORTED_SCHEMA_VERSION:
        return None, f"cursor schema_version={schema!r}; expected {SUPPORTED_SCHEMA_VERSION}"
    last_run = data.get("last_run")
    if not isinstance(last_run, str):
        return None, f"cursor last_run is {type(last_run).__name__}; expected string"
    return last_run, None


def decide(now_utc: datetime, cursor_path: Path) -> dict:
    last_run_iso, err = _read_cursor(cursor_path)

    if err is not None:
        return {
            "wake_agent": True,
            "data": {"reason": "cursor_error", "error": err, "cursor_path": str(cursor_path)},
        }

    if last_run_iso is None:
        return {
            "wake_agent": True,
            "data": {"reason": "no_cursor", "cursor_path": str(cursor_path)},
        }

    last_run = _parse_iso(last_run_iso)
    if last_run is None:
        return {
            "wake_agent": True,
            "data": {
                "reason": "cursor_unparseable",
                "last_run_raw": last_run_iso,
                "cursor_path": str(cursor_path),
            },
        }

    if last_run.tzinfo is None:
        return {
            "wake_agent": True,
            "data": {
                "reason": "cursor_naive_datetime",
                "last_run_raw": last_run_iso,
                "cursor_path": str(cursor_path),
            },
        }

    age = now_utc - last_run
    age_hours = age.total_seconds() / 3600.0

    if age < timedelta(0):
        return {
            "wake_agent": True,
            "data": {
                "reason": "cursor_future",
                "last_run": last_run_iso,
                "age_hours": round(age_hours, 2),
            },
        }

    if age >= CADENCE:
        return {
            "wake_agent": True,
            "data": {
                "reason": "cadence_elapsed",
                "last_run": last_run_iso,
                "age_hours": round(age_hours, 2),
                "cadence_hours": CADENCE.total_seconds() / 3600.0,
            },
        }

    return {
        "wake_agent": False,
        "data": {
            "reason": "within_cadence",
            "last_run": last_run_iso,
            "age_hours": round(age_hours, 2),
            "cadence_hours": CADENCE.total_seconds() / 3600.0,
        },
    }


def main() -> int:
    cursor_path = Path(os.environ.get("NIGHTLY_ORDER_SYNC_CURSOR", DEFAULT_CURSOR_PATH))
    now_utc = datetime.now(timezone.utc)
    payload = decide(now_utc, cursor_path)
    sys.stdout.write(json.dumps(payload) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
