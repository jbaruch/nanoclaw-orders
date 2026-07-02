#!/usr/bin/env python3
"""Stamp the success cursor for `tessl__nightly-order-sync`."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CURSOR_PATH = "/workspace/group/state/nightly-order-sync-cursor.json"
SUPPORTED_SCHEMA = 1


def _atomic_write_text(path: Path, content: str, default_mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        target_mode = os.stat(path).st_mode & 0o777
    except FileNotFoundError:
        target_mode = default_mode

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            dir=str(path.parent),
            delete=False,
            prefix=f".{path.name}.",
            suffix=".tmp",
            encoding="utf-8",
        ) as tf:
            tmp_path = tf.name
            tf.write(content)
            tf.flush()
            os.fsync(tf.fileno())
        os.chmod(tmp_path, target_mode)
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass


def stamp(cursor_path: Path, now_utc: datetime) -> dict:
    iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    record = {"schema_version": SUPPORTED_SCHEMA, "last_run": iso}
    _atomic_write_text(cursor_path, json.dumps(record, indent=2) + "\n")
    return {"status": "stamped", "last_run": iso, "cursor_path": str(cursor_path)}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stamp the success cursor for `tessl__nightly-order-sync`."
    )
    parser.add_argument(
        "--cursor",
        default=os.environ.get("NIGHTLY_ORDER_SYNC_CURSOR", DEFAULT_CURSOR_PATH),
        help="Path to the cursor file (default: %(default)s).",
    )
    args = parser.parse_args()

    cursor_path = Path(args.cursor)
    now_utc = datetime.now(timezone.utc)

    try:
        payload = stamp(cursor_path, now_utc)
    except OSError as exc:
        sys.stderr.write(f"stamp-cursor: write failed for {cursor_path}: {exc}\n")
        return 2

    sys.stdout.write(json.dumps(payload) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
