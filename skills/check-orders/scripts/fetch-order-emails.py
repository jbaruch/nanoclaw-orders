#!/usr/bin/env python3
"""Fetch + dedup + sanitize order-related Gmail messages over REST.

Replaces the COMPOSIO_REMOTE_WORKBENCH path (nanoclaw#655): the #649
custom MCP server cannot run the workbench tool (not a tool-router
session). This script runs the same fetch the workbench orchestration
ran, but via Composio's v3 REST execute endpoint, and sanitizes inside
the container before printing. Raw bodies never reach the agent's
context — only this script's sanitized stdout does, so the
poison-defense invariant (`/workspace/group/nanoclaw-poison-defense.md`)
holds.

Why a script (per script-delegation rule): the deterministic parts —
the 5 fixed queries, the cursor-based `after:` boundary, REST fan-out,
cross-query dedup, sanitize, compact-row projection — are all here.
SKILL.md only runs this script and parses its output.

Output (single-line JSON on stdout, exit 0 on success) — the same
shape the workbench produced, so SKILL.md Steps 3-10 are unchanged:
    {"messages": [{"messageId", "threadId", "from", "to", "subject",
                   "snippet", "body", "date", "labelIds"}],
     "errors": [{"query": "...", "error": "..."}]}

Exit non-zero with a stderr diagnostic if the shared sanitizer can't
be loaded (fail-closed — never emit unsanitized bodies).
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sqlite3
import sys
import urllib.error
from datetime import datetime, timedelta, timezone

DB_PATH = os.environ.get("ORDERS_DB_PATH", "/workspace/store/messages.db")

# Shared helpers owned by the heartbeat skill. Resolved at the tile
# mount path in production, with a repo-relative fallback for tests /
# dev clones. If the sanitizer can't load, main() fails closed.
_SCRIPTS = pathlib.Path(__file__).resolve().parent
_SKILLS_ROOT = _SCRIPTS.parents[1]
SANITIZER_MOUNT = "/home/node/.claude/skills/tessl__heartbeat/scripts/sanitize-email-body.py"
SANITIZER_FALLBACK = _SKILLS_ROOT / "heartbeat/scripts/sanitize-email-body.py"
COMPOSIO_REST_MOUNT = "/home/node/.claude/skills/tessl__heartbeat/scripts/composio-rest.py"
COMPOSIO_REST_FALLBACK = _SKILLS_ROOT / "heartbeat/scripts/composio-rest.py"

# Fixed Composio Gmail multi-query fetch slug (verified against the v3
# tool schema). Hardcoded per `rules/composio-preamble.md`'s REST-fetch
# exception — discovery via COMPOSIO_SEARCH_TOOLS is dead (nanoclaw#655).
FETCH_EMAILS_SLUG = "GMAIL_FETCH_EMAILS"

QUERIES = [
    "from:auto-confirm@amazon.com",
    "from:shipment-tracking@amazon.com",
    '"Your order" (shipped OR delivered OR cancelled OR refund)',
    "from:noreply@shopify.com OR from:no-reply@shopify.com",
    "subject:(order confirmation OR order shipped OR order delivered OR order cancelled OR refund)",
]


def _load_module(modname: str, mount: str, fallback: pathlib.Path):
    path = mount if os.path.exists(mount) else str(fallback)
    spec = importlib.util.spec_from_file_location(modname, path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"cannot load {modname} from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read_last_checked(db_path: str) -> str | None:
    """Return `orders_metadata.last_checked` (raw ISO-8601) or None on
    any read failure / missing row. Failures fall through to the
    unbounded-fetch fallback — the alternative (raise) would freeze the
    skill on a transient DB issue, and the fetch is still bounded by
    `max_results: 20` per query."""
    if not pathlib.Path(db_path).exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT value FROM orders_metadata WHERE key = 'last_checked'"
        ).fetchone()
        return row[0] if row and row[0] else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _gmail_after_filter(last_checked_iso: str | None) -> str:
    """Return ` after:YYYY/MM/DD` suffix (leading space) or empty string.

    Subtracts 1 day from the cursor to avoid same-day boundary losses
    (Gmail's `after:` is local-TZ-midnight, `last_checked` is a UTC
    instant). Duplicate fetches across the overlap are safe —
    apply-order.py upserts on email_message_id."""
    if not last_checked_iso:
        return ""
    try:
        dt = datetime.fromisoformat(last_checked_iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    cutoff = (dt - timedelta(days=1)).strftime("%Y/%m/%d")
    return f" after:{cutoff}"


def _queries_with_filter(after_suffix: str) -> list[str]:
    """Wrap each base query in parens before appending `after:`. Gmail
    binds implicit AND tighter than OR, so `from:a OR from:b after:DATE`
    leaks all `from:a` regardless of date; `(from:a OR from:b) after:DATE`
    restores `(A OR B) AND after:DATE`. Redundant parens on
    single-operator queries are accepted."""
    if not after_suffix:
        return list(QUERIES)
    return [f"({q}){after_suffix}" for q in QUERIES]


def _extract_messages(resp: dict) -> list:
    """Pull the message list out of the Composio REST envelope,
    tolerating the nested-`data` shape variants Composio emits — the
    same fallbacks the workbench orchestration used."""
    data = resp.get("data", {}) if isinstance(resp.get("data"), dict) else {}
    nested = data.get("data", {}) if isinstance(data.get("data"), dict) else {}
    msgs = data.get("messages") or resp.get("messages") or nested.get("messages") or []
    return msgs if isinstance(msgs, list) else []


def _as_text(v) -> str:
    """Flatten a Composio field to text. The v3 REST `GMAIL_FETCH_EMAILS`
    returns `snippet` (and sometimes a body-ish field) as a
    `{body, subject}` object — not the plain string the workbench's
    `run_composio_tool` produced — so the projection must coerce, else
    Step 4 parses a nested dict instead of email text."""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        for k in ("body", "text", "subject"):
            if isinstance(v.get(k), str):
                return v[k]
        return ""
    if isinstance(v, list):
        return " ".join(x for x in v if isinstance(x, str))
    return ""


def fetch_order_emails(execute, sanitize_message, queries) -> dict:
    """Run each query via `execute`, dedup across queries, sanitize in
    place, project compact rows. `execute(action, arguments)` is the
    injected REST caller (composio_execute); `sanitize_message(msg)`
    sanitizes body-ish fields in place. Returns the workbench-shaped
    {"messages": [...], "errors": [...]}."""
    seen: set[str] = set()
    all_msgs: list = []
    errors: list = []
    for q in queries:
        try:
            resp = execute(
                FETCH_EMAILS_SLUG,
                {"query": q, "max_results": 20, "include_spam_trash": False},
            )
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            errors.append({"query": q, "error": f"{type(exc).__name__}: {exc}"})
            continue
        if not isinstance(resp, dict):
            errors.append({"query": q, "error": "non-dict response"})
            continue
        if resp.get("error") or resp.get("successful") is False:
            errors.append({"query": q, "error": str(resp.get("error") or "tool reported failure")})
            continue
        for m in _extract_messages(resp):
            mid = m.get("messageId") or m.get("id")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            all_msgs.append(m)

    for m in all_msgs:
        sanitize_message(m)

    compact = []
    for m in all_msgs:
        body = _as_text(m.get("messageText") or m.get("body") or m.get("plainText"))
        compact.append(
            {
                "messageId": m.get("messageId") or m.get("id"),
                "threadId": m.get("threadId"),
                "from": m.get("from") or m.get("sender"),
                "to": m.get("to") or m.get("toAddress") or m.get("toRecipients"),
                "subject": m.get("subject"),
                "snippet": _as_text(m.get("snippet") or m.get("preview")),
                "body": body[:4000],
                "date": m.get("date") or m.get("messageTimestamp"),
                "labelIds": m.get("labelIds", []),
            }
        )
    return {"messages": compact, "errors": errors}


def main() -> int:
    try:
        sanitizer = _load_module("sanitize_email_body", SANITIZER_MOUNT, SANITIZER_FALLBACK)
    except (FileNotFoundError, PermissionError, ImportError, OSError) as e:
        sys.stderr.write(
            f"fetch-order-emails: sanitizer unavailable ({e}). "
            "Refusing to fetch without sanitization.\n"
        )
        return 2

    try:
        composio_rest = _load_module("composio_rest", COMPOSIO_REST_MOUNT, COMPOSIO_REST_FALLBACK)
    except (FileNotFoundError, PermissionError, ImportError, OSError) as e:
        sys.stderr.write(
            f"fetch-order-emails: Composio REST helper unavailable ({e}) — expected at "
            "tessl__heartbeat/scripts/composio-rest.py. Refusing to fetch.\n"
        )
        return 2

    try:
        composio_rest.require_credentials()
    except composio_rest.MissingCredentials as e:
        sys.stderr.write(
            f"fetch-order-emails: missing Composio credentials ({e}) — "
            f"{composio_rest.MISSING_CREDENTIALS_HINT}. Refusing to fetch.\n"
        )
        return 2

    after_suffix = _gmail_after_filter(_read_last_checked(DB_PATH))
    result = fetch_order_emails(
        composio_rest.composio_execute,
        sanitizer.sanitize_message,
        _queries_with_filter(after_suffix),
    )
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
