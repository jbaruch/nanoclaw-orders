"""Tests for skills/check-orders/scripts/fetch-order-emails.py.

Locks down the documented contract per `coding-policy: testing-standards`,
against native Gmail REST shapes brokered by the OneCLI gateway
(jbaruch/nanoclaw#638 — the Composio v3 REST path and its credential
preflight are gone):

  - `fetch_order_emails(gmail, sanitize, gmail_message, queries)` lists
    each query, dedups the `{id, threadId}` stubs ACROSS queries before
    any body is fetched, sanitizes every survivor, and projects the
    {"messages": [...], "errors": [...]} rows SKILL.md Steps 3-10 read
  - a query whose list call fails, and a message whose `get` fails, each
    become one `{"query", "error"}` entry — the run still proceeds
  - `snippet` (Gmail's ~200-char preview) and `body` (full extracted
    text) stay distinct — Step 4 reads keywords from one and the dollar
    amount from the other
  - `_gmail_after_filter` subtracts 1 day, coerces naive to UTC, and
    falls through on None/empty/malformed
  - `_queries_with_filter` paren-groups every query under `after:` so
    Gmail's AND-binds-tighter-than-OR parsing can't leak
  - `_read_last_checked` reads the cursor from DB_PATH, None on miss
  - `main()` exits 2 with a stderr diagnostic and no stdout when a shared
    heartbeat helper can't be loaded (fail-closed), and when the gateway
    isn't injecting / the tier is restricted (operator-actionable config
    faults, not per-query errors)
  - the loaded sanitizer runs on every message BEFORE projection (the
    sanitizer's own invisible-unicode behavior is covered in
    jbaruch/nanoclaw-admin's heartbeat suite; this tile owns the
    integration contract that raw fields never reach the projection
    unsanitized)

The fakes under tests/fakes/ speak native Gmail: list returns
`{"messages": [{"id", "threadId"}]}`, get returns a `users.messages.get`
resource with a raw `payload.headers` list, base64url part bodies in a
nested MIME tree, and `internalDate` as an epoch-millisecond string.
"""

import base64
import json
import types
from datetime import datetime, timedelta, timezone

import pytest

LIST_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"

# Frozen epoch-ms fixture — no wall clock anywhere in this suite.
# 1780099200000 ms == 2026-05-30T00:00:00Z.
MAY_30_2026_MS = "1780099200000"
MAY_30_2026_ISO = "2026-05-30T00:00:00+00:00"


def _b64(text):
    """Gmail base64url-encodes part bodies and OMITS the `=` padding."""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _native_message(
    mid,
    *,
    thread="t1",
    subject="",
    frm="",
    to="",
    snippet="",
    body="",
    labels=("INBOX",),
    internal_date=MAY_30_2026_MS,
):
    """A `users.messages.get` resource as Gmail really returns one: full
    raw header list (not a curated set), body base64url-encoded inside a
    NESTED MIME tree, `internalDate` an epoch-ms string."""
    return {
        "id": mid,
        "threadId": thread,
        "labelIds": list(labels),
        "internalDate": internal_date,
        "snippet": snippet,
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "Delivered-To", "value": "me@x.com"},
                {"name": "Received", "value": "by 2002:a05 with SMTP id x"},
                {"name": "From", "value": frm},
                {"name": "To", "value": to},
                {"name": "Subject", "value": subject},
            ],
            "parts": [
                {
                    "mimeType": "multipart/related",
                    "parts": [{"mimeType": "text/plain", "body": {"data": _b64(body)}}],
                }
            ],
        },
    }


def _transport(list_by_query, messages_by_id, *, calls=None):
    """Fake `google_request`, routing on the URL gmail-ops builds.

    `list_by_query` maps a query string to the ids it returns (or an
    Exception instance to raise); `messages_by_id` maps an id to its
    native resource (or an Exception to raise). `calls` collects
    (url, params) so a test can assert the native params and count the
    per-message `get` fan-out.
    """

    def google_request(method, url, *, params=None, body=None):
        assert method == "GET"
        assert params is not None, "gmail-ops always sends params — a bare call is a bug"
        if calls is not None:
            calls.append((url, params))
        if url == LIST_URL:
            found = list_by_query[params["q"]]
            if isinstance(found, Exception):
                raise found
            # Gmail OMITS the `messages` key entirely on a no-match query.
            return {"messages": [{"id": i, "threadId": "t1"} for i in found]} if found else {}
        mid = url.rsplit("/", 1)[-1]
        resource = messages_by_id[mid]
        if isinstance(resource, Exception):
            raise resource
        return resource

    return google_request


@pytest.fixture
def gmail_for(fetch_order_emails, heartbeat_fakes):
    """Build the bound `gmail` collaborator over a fake transport, through
    the real `bind_gmail` + the gmail-ops double."""
    module, _, _ = fetch_order_emails

    def build(list_by_query, messages_by_id, calls=None):
        google_rest = types.SimpleNamespace(
            google_request=_transport(list_by_query, messages_by_id, calls=calls),
            surface_url=heartbeat_fakes["google_rest"].surface_url,
        )
        return module.bind_gmail(google_rest, heartbeat_fakes["gmail_ops"])

    return build


@pytest.fixture
def run_fetch(fetch_order_emails, heartbeat_fakes):
    """Invoke the pure core with the fake sanitizer + gmail-message
    double, so tests only supply the transport and the queries."""
    module, _, _ = fetch_order_emails

    def run(gmail, queries, sanitize=None):
        return module.fetch_order_emails(
            gmail,
            sanitize or heartbeat_fakes["sanitize_email_body"].sanitize,
            heartbeat_fakes["gmail_message"],
            queries,
        )

    return run


def test_dedups_across_queries_and_projects_shape(gmail_for, run_fetch):
    msgs = {
        "m1": _native_message(
            "m1",
            subject="Shipped",
            frm="store@x.com",
            to="me@x.com",
            snippet="on the way",
            body="Your order shipped. Total: $19.99",
        ),
        "m2": _native_message("m2", subject="Delivered", body="b2"),
    }
    result = run_fetch(
        gmail_for({"q1": ["m1", "m2"], "q2": ["m1"]}, msgs),
        ["q1", "q2"],
    )
    assert result["errors"] == []
    ids = [m["messageId"] for m in result["messages"]]
    assert ids == ["m1", "m2"], "m1 must appear once despite two queries returning it"
    row = result["messages"][0]
    assert set(row.keys()) == {
        "messageId",
        "threadId",
        "from",
        "to",
        "subject",
        "snippet",
        "body",
        "date",
        "labelIds",
    }
    assert row["from"] == "store@x.com"
    assert row["to"] == "me@x.com"
    assert row["subject"] == "Shipped"
    assert row["labelIds"] == ["INBOX"]


def test_dedup_runs_before_bodies_are_fetched(gmail_for, run_fetch):
    """An id surfaced by three of the five queries costs exactly one
    `get`. Dedup against the stubs is the whole reason the native list's
    id-only response is not a regression."""
    calls = []
    msgs = {"m1": _native_message("m1", subject="S", body="b")}
    result = run_fetch(
        gmail_for({"q1": ["m1"], "q2": ["m1"], "q3": ["m1"]}, msgs, calls=calls),
        ["q1", "q2", "q3"],
    )
    assert [m["messageId"] for m in result["messages"]] == ["m1"]
    gets = [url for (url, _) in calls if url != LIST_URL]
    assert gets == [f"{LIST_URL}/m1"], "the deduped id must cost exactly one get"


def test_native_list_params_carry_the_query_and_bound(gmail_for, run_fetch):
    """The query reaches Gmail as the native `q` param, capped at
    MAX_RESULTS — the Composio `{"query", "max_results"}` argument map is
    gone."""
    calls = []
    run_fetch(gmail_for({"from:x@y.com": []}, {}, calls=calls), ["from:x@y.com"])
    (url, params) = calls[0]
    assert url == LIST_URL
    assert params["q"] == "from:x@y.com"
    assert params["maxResults"] == 20
    assert params["includeSpamTrash"] == "false"


def test_snippet_is_the_preview_and_body_is_the_full_text(gmail_for, run_fetch):
    """Native `snippet` is a ~200-char preview; `body` is the full
    extracted text. Step 4 matches status keywords against subject+snippet
    and reads the dollar amount out of `body`, so collapsing the two would
    silently cut its view of the mail."""
    msgs = {
        "m": _native_message(
            "m",
            subject="S",
            snippet="preview text",
            body="full body text with Total: $42.00",
        )
    }
    result = run_fetch(gmail_for({"q": ["m"]}, msgs), ["q"])
    row = result["messages"][0]
    assert row["snippet"] == "preview text"
    assert row["body"] == "full body text with Total: $42.00"


def test_body_decodes_from_nested_base64url_mime_tree(gmail_for, run_fetch):
    """The live MIME tree is multipart/mixed -> multipart/related ->
    text/*, and Gmail omits base64url padding. Anything reading
    `payload.body.data`, or walking one level, gets nothing."""
    msgs = {"m": _native_message("m", subject="S", body="Order #123 delivered")}
    result = run_fetch(gmail_for({"q": ["m"]}, msgs), ["q"])
    assert result["messages"][0]["body"] == "Order #123 delivered"


def test_internal_date_epoch_ms_becomes_the_date_field(gmail_for, run_fetch):
    """`internalDate` (epoch ms as a STRING) replaces Composio's date
    field; Step 4 derives `order_date` from it."""
    msgs = {"m": _native_message("m", subject="S", body="b", internal_date=MAY_30_2026_MS)}
    result = run_fetch(gmail_for({"q": ["m"]}, msgs), ["q"])
    assert result["messages"][0]["date"] == MAY_30_2026_ISO


def test_sanitizer_runs_before_projection(gmail_for, run_fetch):
    """Integration contract: every kept message passes through the loaded
    sanitizer BEFORE the compact-row projection reads its text fields. The
    sanitizer's own invisible-unicode behavior is heartbeat's (tested in
    admin's suite); what this tile owns is that the projection only ever
    sees post-sanitizer state — a marking sanitizer double proves the
    ordering."""

    def marking_sanitize(text, max_len=2000):
        return "SANITIZED::" + str(text)

    msgs = {"m": _native_message("m", subject="S", snippet="prev", body="raw body")}
    result = run_fetch(gmail_for({"q": ["m"]}, msgs), ["q"], sanitize=marking_sanitize)
    row = result["messages"][0]
    assert row["body"] == "SANITIZED::raw body"
    assert row["snippet"] == "SANITIZED::prev"
    assert row["subject"] == "SANITIZED::S"


def test_empty_list_response_yields_no_messages_and_no_errors(gmail_for, run_fetch):
    """Gmail omits the `messages` key on a no-match query — an empty
    result is success, not an error (SKILL Step 2 still stamps the
    cursor)."""
    result = run_fetch(gmail_for({"q": []}, {}), ["q"])
    assert result == {"messages": [], "errors": []}


def test_per_query_list_failure_becomes_error_marker(gmail_for, run_fetch):
    msgs = {"m1": _native_message("m1", subject="S", body="b")}
    result = run_fetch(
        gmail_for({"good": ["m1"], "bad": OSError("connection reset")}, msgs),
        ["good", "bad"],
    )
    assert [m["messageId"] for m in result["messages"]] == ["m1"]
    assert len(result["errors"]) == 1
    assert result["errors"][0]["query"] == "bad"
    assert "connection reset" in result["errors"][0]["error"]


def test_per_message_get_failure_is_reported_not_swallowed(gmail_for, run_fetch):
    """An unreadable message is an order alert that will not fire, so it
    becomes an error entry against the query that surfaced it — the rest
    of the batch still lands."""
    msgs = {
        "m1": _native_message("m1", subject="S", body="b"),
        "m2": OSError("read timed out"),
    }
    result = run_fetch(gmail_for({"q": ["m1", "m2"]}, msgs), ["q"])
    assert [m["messageId"] for m in result["messages"]] == ["m1"]
    assert len(result["errors"]) == 1
    assert result["errors"][0]["query"] == "q"
    assert "m2" in result["errors"][0]["error"]
    assert "read timed out" in result["errors"][0]["error"]


def test_http_error_from_google_becomes_a_query_error(gmail_for, run_fetch):
    """A Google 5xx surfaces as urllib's HTTPError (an OSError subclass) —
    a per-query error marker, not a run-ending fault."""
    import email.message
    import urllib.error

    boom = urllib.error.HTTPError(
        LIST_URL, 503, "Service Unavailable", email.message.Message(), None
    )
    result = run_fetch(gmail_for({"q": boom}, {}), ["q"])
    assert result["messages"] == []
    assert result["errors"][0]["query"] == "q"
    assert "503" in result["errors"][0]["error"]


def test_queries_module_constant_has_expected_count(fetch_order_emails):
    module, _, _ = fetch_order_emails
    assert len(module.QUERIES) == 5


def test_queries_with_filter_paren_groups_or_query(fetch_order_emails):
    module, _, _ = fetch_order_emails
    out = module._queries_with_filter(" after:2026/05/11")
    # Query 4 is the OR pair; it must be paren-grouped so `after:` AND-binds
    # to the whole group, not just the right operand.
    assert "(from:noreply@shopify.com OR from:no-reply@shopify.com) after:2026/05/11" in out
    # No-suffix path returns the raw queries unchanged.
    assert module._queries_with_filter("") == list(module.QUERIES)


def test_gmail_after_filter_unit_cases(fetch_order_emails):
    module, _, _ = fetch_order_emails
    assert module._gmail_after_filter("2026-05-12T00:30:00.000Z") == " after:2026/05/11"
    assert module._gmail_after_filter("2026-05-01T00:00:00.000Z") == " after:2026/04/30"
    assert module._gmail_after_filter("2026-05-12T13:30:35.000+00:00") == " after:2026/05/11"
    assert module._gmail_after_filter("2026-05-12T13:30:35.000") == " after:2026/05/11"
    assert module._gmail_after_filter("2026-01-01T12:00:00.000Z") == " after:2025/12/31"
    assert module._gmail_after_filter(None) == ""
    assert module._gmail_after_filter("") == ""
    assert module._gmail_after_filter("garbage") == ""
    expected = (
        datetime(2026, 5, 12, 13, 30, 35, tzinfo=timezone.utc) - timedelta(days=1)
    ).strftime("%Y/%m/%d")
    assert module._gmail_after_filter("2026-05-12T13:30:35Z") == f" after:{expected}"


def test_read_last_checked_reads_cursor(fetch_order_emails):
    import sqlite3

    module, _, db_path = fetch_order_emails
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO orders_metadata (key, value) VALUES ('last_checked', ?)",
            ("2026-05-12T13:30:35.000Z",),
        )
        conn.commit()
    finally:
        conn.close()
    assert module._read_last_checked(str(db_path)) == "2026-05-12T13:30:35.000Z"


def test_read_last_checked_missing_row_returns_none(fetch_order_emails):
    module, _, db_path = fetch_order_emails
    assert module._read_last_checked(str(db_path)) is None


def test_load_module_resolves_every_shared_helper_from_the_mount(fetch_order_emails, monkeypatch):
    """The four heartbeat filenames resolve from the mount dir. Guards the
    filename map against a rename on the admin side (which would otherwise
    only surface at 06:15 in production)."""
    from pathlib import Path

    module, _, _ = fetch_order_emails
    monkeypatch.setattr(module, "_HEARTBEAT_MOUNT", str(Path(__file__).resolve().parent / "fakes"))
    for modname, filename in module._SHARED_MODULES.items():
        assert module._load_module(modname, filename) is not None
    assert set(module._SHARED_MODULES) == {
        "sanitize_email_body",
        "google_rest",
        "gmail_ops",
        "gmail_message",
    }


def test_main_fail_closed_when_a_shared_helper_missing(fetch_order_emails, capsys):
    module, missing_scripts, _ = fetch_order_emails
    assert not missing_scripts.exists()
    rc = module.main()
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert "unavailable" in captured.err
    assert "Refusing" in captured.err
    assert "tessl__heartbeat" in captured.err


def _install_fakes(module, monkeypatch, heartbeat_fakes):
    """Point `main()`'s loader at the already-loaded doubles.

    Class identity matters: `main()` catches `google_rest.GatewayNotInjecting`
    off the module IT loaded, so a test raising the class from a separately
    loaded copy would sail past the handler. Handing main() the same module
    objects the test holds is what keeps the except clauses honest."""
    monkeypatch.setattr(module, "_load_module", lambda name, filename: heartbeat_fakes[name])


def test_main_prints_the_stdout_contract(
    fetch_order_emails, heartbeat_fakes, gmail_for, monkeypatch, capsys
):
    module, _, _ = fetch_order_emails
    _install_fakes(module, monkeypatch, heartbeat_fakes)
    msgs = {"m1": _native_message("m1", subject="Shipped", body="b")}
    gmail = gmail_for({q: ["m1"] for q in module.QUERIES}, msgs)
    monkeypatch.setattr(module, "bind_gmail", lambda *_: gmail)
    rc = module.main()
    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out)
    assert payload["errors"] == []
    assert [m["messageId"] for m in payload["messages"]] == ["m1"]


@pytest.mark.parametrize(
    "exc_name, expected",
    [
        ("GatewayNotInjecting", "gateway"),
        ("TierAccessRestricted", "tier"),
    ],
)
def test_main_exits_2_on_gateway_and_tier_faults(
    fetch_order_emails, heartbeat_fakes, monkeypatch, capsys, exc_name, expected
):
    """Config faults exit rather than degrading into a per-query error
    entry — the split the Composio path drew between missing credentials
    (exit 2) and a failed call (error entry)."""
    module, _, _ = fetch_order_emails
    _install_fakes(module, monkeypatch, heartbeat_fakes)
    exc_class = getattr(heartbeat_fakes["google_rest"], exc_name)

    def raising(**_kwargs):
        raise exc_class(f"the OneCLI {expected} said no")

    monkeypatch.setattr(
        module, "bind_gmail", lambda *_: types.SimpleNamespace(list=raising, get=raising)
    )
    rc = module.main()
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert expected in captured.err
    assert "fetch-order-emails" in captured.err
