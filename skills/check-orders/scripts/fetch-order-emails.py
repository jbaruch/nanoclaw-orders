#!/usr/bin/env python3
"""Fetch + dedup + sanitize order-related Gmail messages natively.

Native Gmail REST via the OneCLI gateway (nanoclaw#638), replacing the
Composio v3 REST path. This container holds NO Google credential: the
gateway injects the `Authorization: Bearer` on the wire to Google and
refreshes it (see heartbeat's `google-rest.py`). Nothing here reads a
key from the environment — `COMPOSIO_API_KEY` / `COMPOSIO_USER_ID` are
gone, and their credential preflight with them.

Sanitization still runs in-process, so raw bodies never reach the
agent's context — only this script's sanitized stdout does, preserving
the poison-defense invariant
(`/workspace/group/nanoclaw-poison-defense.md`).

Why a script (per script-delegation rule): the deterministic parts —
the 5 fixed queries, the cursor-based `after:` boundary, the REST
fan-out, cross-query dedup, sanitize, compact-row projection — are all
here. SKILL.md only runs this script and parses its output.

The list/get split
------------------
Composio's `GMAIL_FETCH_EMAILS` returned full messages for a query in
one call. Native `users.messages.list` answers a query (native `q`)
with `{id, threadId}` stubs only, so every message costs a second
`get` — an N-message query is N+1 calls. Dedup therefore runs against
the STUBS, before any body is paid for: an id returned by three of the
five queries costs exactly one `get`, not three.

Output (single-line JSON on stdout, exit 0 on success) — unchanged
from the Composio path, so SKILL.md Steps 3-10 are untouched:
    {"messages": [{"messageId", "threadId", "from", "to", "subject",
                   "snippet", "body", "date", "labelIds"}],
     "errors": [{"query": "...", "error": "..."}]}

`snippet` is Gmail's own ~200-char preview and `body` is the full
extracted text — the same split the Composio path projected, where
`snippet` came from Composio's snippet object and `body` from
`messageText`. Step 4 reads subject+snippet for status keywords and
`body` for the dollar amount, so the two must not be collapsed.

Per-query error isolation: a query whose list call fails, and a message
whose `get` fails, each become one `{"query", "error"}` entry rather
than sinking the run. Nothing is dropped silently — an order email we
could not read is an alert that will not fire, so it is reported.

Exit non-zero with a stderr diagnostic when a shared helper can't be
loaded (fail-closed — never emit unsanitized bodies), or when the
gateway is not injecting / the tier is restricted. Those are
operator-actionable config errors, so they exit rather than degrade
into a per-query error entry — the same split the Composio path drew
between missing credentials (exit 2) and a failed call (error entry).
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sqlite3
import sys
import types
import urllib.error
from datetime import datetime, timedelta, timezone

DB_PATH = os.environ.get("ORDERS_DB_PATH", "/workspace/store/messages.db")

# Shared helpers owned by the heartbeat skill, consumed cross-skill over
# the co-loaded `tessl__heartbeat` tile mount, with a repo-relative
# fallback for tests / dev clones. If any can't load, main() fails closed.
_SCRIPTS = pathlib.Path(__file__).resolve().parent
_SKILLS_ROOT = _SCRIPTS.parents[1]
_HEARTBEAT_MOUNT = "/home/node/.claude/skills/tessl__heartbeat/scripts"
_HEARTBEAT_FALLBACK = _SKILLS_ROOT / "heartbeat/scripts"

_SHARED_MODULES = {
    "sanitize_email_body": "sanitize-email-body.py",
    "google_rest": "google-rest.py",
    "gmail_ops": "gmail-ops.py",
    "gmail_message": "gmail-message.py",
}

# Per-query cap on one Gmail list. The bounded fetch is what keeps the
# per-message `get` count flat; `gmail-ops.list_messages` deliberately
# does not paginate past it (nanoclaw#656). Matches the `max_results: 20`
# the Composio path passed, so the migration doesn't change the window.
MAX_RESULTS = 20

QUERIES = [
    "from:auto-confirm@amazon.com",
    "from:shipment-tracking@amazon.com",
    '"Your order" (shipped OR delivered OR cancelled OR refund)',
    "from:noreply@shopify.com OR from:no-reply@shopify.com",
    "subject:(order confirmation OR order shipped OR order delivered OR order cancelled OR refund)",
]

# Transport failures that belong to one query / one message rather than
# to the run. HTTPError subclasses OSError, so a Google 5xx lands here as
# an error entry; GatewayNotInjecting / TierAccessRestricted are
# RuntimeErrors and deliberately do NOT — they propagate to main().
_CALL_ERRORS = (urllib.error.URLError, OSError, json.JSONDecodeError)


def _load_module(modname: str, filename: str):
    base = _HEARTBEAT_MOUNT if os.path.exists(_HEARTBEAT_MOUNT) else str(_HEARTBEAT_FALLBACK)
    path = os.path.join(base, filename)
    spec = importlib.util.spec_from_file_location(modname, path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"cannot load {modname} from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def bind_gmail(google_rest, gmail_ops):
    """Bind the gmail-ops functions to the transport once, so
    `fetch_order_emails` takes one collaborator instead of threading
    `google_request` + `surface_url` through every call site (and so
    tests can hand it a fake with two methods)."""
    request, surface_url = google_rest.google_request, google_rest.surface_url
    return types.SimpleNamespace(
        list=lambda **kw: gmail_ops.list_messages(request, surface_url=surface_url, **kw),
        get=lambda message_id: gmail_ops.get_message(request, message_id, surface_url=surface_url),
    )


def _read_last_checked(db_path: str) -> str | None:
    """Return `orders_metadata.last_checked` (raw ISO-8601) or None on
    any read failure / missing row. Failures fall through to the
    unbounded-fetch fallback — the alternative (raise) would freeze the
    skill on a transient DB issue, and the fetch is still bounded by
    MAX_RESULTS per query."""
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


def _list_stubs(gmail, queries, errors):
    """List every query and return the deduped `[(message_id, query)]`,
    query order preserved. `query` is the one that first surfaced the id —
    it attributes a later `get` failure to a real query for the errors
    entry. A failing list becomes an error entry and abandons that query
    only."""
    seen: set[str] = set()
    stubs: list[tuple[str, str]] = []
    for q in queries:
        try:
            found = gmail.list(limit=MAX_RESULTS, query=q, include_spam_trash=False)
        except _CALL_ERRORS as exc:
            errors.append({"query": q, "error": f"{type(exc).__name__}: {exc}"})
            continue
        for stub in found:
            mid = stub.get("id") or ""
            if not mid or mid in seen:
                continue
            seen.add(mid)
            stubs.append((mid, q))
    return stubs


def fetch_order_emails(gmail, sanitize, gmail_message, queries) -> dict:
    """List each query, dedup the stubs across queries, fetch and
    sanitize each survivor, project compact rows.

    `gmail` is the bound collaborator from `bind_gmail` (`.list`/`.get`);
    `sanitize` is heartbeat's `sanitize()`; `gmail_message` is heartbeat's
    parser module. Returns {"messages": [...], "errors": [...]}."""
    errors: list = []
    compact: list = []
    for mid, query in _list_stubs(gmail, queries, errors):
        try:
            raw = gmail.get(mid)
        except _CALL_ERRORS as exc:
            # One unreadable message must not sink the rest of the batch,
            # but it must not vanish either: it is an order alert that
            # will not fire, so it is reported against the query that
            # surfaced it.
            errors.append({"query": query, "error": f"get {mid}: {type(exc).__name__}: {exc}"})
            continue
        msg = gmail_message.parse_message(raw, sanitize)
        if not msg:
            # Same invariant as the fetch failure above: a message that
            # won't parse is still an order alert that will not fire, so it
            # is reported rather than dropped. parse_message returns {} only
            # for a non-dict resource — a shape Gmail should never send,
            # which is precisely why it must not pass silently.
            errors.append({"query": query, "error": f"parse {mid}: unrecognised message resource"})
            continue
        compact.append(
            {
                "messageId": msg.get("messageId"),
                "threadId": msg.get("threadId"),
                "from": msg.get("from"),
                "to": msg.get("to"),
                "subject": msg.get("subject"),
                # Gmail's native ~200-char preview, mirroring what the
                # Composio path projected from its snippet object. Step 4
                # matches status keywords against subject+snippet.
                "snippet": msg.get("snippet"),
                # Full extracted body — Step 4's dollar amount comes from
                # here, so it must stay distinct from the preview above.
                # No truncation: parse_message hands every field through
                # sanitize(), which already caps at its 2000-char max_len.
                "body": msg.get("body"),
                "date": msg.get("internalDate"),
                "labelIds": msg.get("labelIds", []),
            }
        )
    return {"messages": compact, "errors": errors}


def main() -> int:
    mods = {}
    for modname, filename in _SHARED_MODULES.items():
        try:
            mods[modname] = _load_module(modname, filename)
        except (FileNotFoundError, PermissionError, ImportError, OSError) as e:
            sys.stderr.write(
                f"fetch-order-emails: {filename} unavailable ({e}) — expected under "
                f"tessl__heartbeat/scripts/. Refusing to fetch without it.\n"
            )
            return 2

    google_rest = mods["google_rest"]
    after_suffix = _gmail_after_filter(_read_last_checked(DB_PATH))
    try:
        result = fetch_order_emails(
            bind_gmail(google_rest, mods["gmail_ops"]),
            mods["sanitize_email_body"].sanitize,
            mods["gmail_message"],
            _queries_with_filter(after_suffix),
        )
    except google_rest.GatewayNotInjecting as e:
        sys.stderr.write(f"fetch-order-emails: {e}\n")
        return 2
    except google_rest.TierAccessRestricted as e:
        sys.stderr.write(f"fetch-order-emails: {e}\n")
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
