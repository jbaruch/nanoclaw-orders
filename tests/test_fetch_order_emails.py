"""Tests for skills/check-orders/scripts/fetch-order-emails.py.

Locks down the documented contract per `coding-policy: testing-standards`:

  - `fetch_order_emails(execute, sanitize_message, queries)` runs each
    query via the injected REST caller, dedups across queries, sanitizes
    every kept message, and projects the workbench-shaped
    {"messages": [...], "errors": [...]} rows
  - A query whose `execute` raises, or returns a tool-error envelope,
    becomes one `{"query", "error"}` entry — the run still proceeds
  - `_gmail_after_filter` subtracts 1 day, coerces naive to UTC, and
    falls through on None/empty/malformed
  - `_queries_with_filter` paren-groups every query under `after:` so
    Gmail's AND-binds-tighter-than-OR parsing can't leak
  - `_read_last_checked` reads the cursor from DB_PATH, None on miss
  - `main()` exits 2 with a stderr diagnostic and no stdout when the
    shared sanitizer, the REST helper, or the Composio credentials are
    unavailable (fail-closed) — exercised against the local fakes in
    tests/fakes/, which mirror the heartbeat modules' load surface
  - the loaded sanitizer runs on every message BEFORE projection (the
    sanitizer's own body-cap / invisible-unicode behavior is covered
    in jbaruch/nanoclaw-admin's heartbeat suite; this tile owns the
    integration contract that raw fields never reach the projection
    unsanitized)
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

_FAKES = Path(__file__).resolve().parent / "fakes"
FAKE_SANITIZER = str(_FAKES / "sanitize-email-body.py")
FAKE_COMPOSIO_REST = str(_FAKES / "composio-rest.py")


def _ok(messages):
    return {"successful": True, "error": None, "data": {"messages": messages}}


def _fake_execute(by_query):
    """Build an injected `execute(action, arguments)` that returns the
    mapped response for each query. A mapped `Exception` instance is
    raised instead of returned (simulates an HTTP/timeout failure)."""

    def execute(action, arguments):
        assert action == "GMAIL_FETCH_EMAILS"
        resp = by_query[arguments["query"]]
        if isinstance(resp, Exception):
            raise resp
        return resp

    return execute


def test_dedups_across_queries_and_projects_shape(fetch_order_emails, sanitize_email_body):
    module, _, _ = fetch_order_emails
    sanitize = sanitize_email_body.sanitize_message
    shared = {
        "messageId": "m1",
        "threadId": "t1",
        "from": "store@x.com",
        "to": "me@x.com",
        "subject": "Shipped",
        "snippet": "on the way",
        "messageText": "Your order shipped",
        "date": "2026-05-30",
        "labelIds": ["INBOX"],
    }
    by_query = {
        "q1": _ok([shared, {"messageId": "m2", "subject": "Delivered", "messageText": "b2"}]),
        "q2": _ok([shared]),  # m1 again → must dedup
    }
    result = module.fetch_order_emails(_fake_execute(by_query), sanitize, ["q1", "q2"])
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
    assert row["body"] == "Your order shipped"
    assert row["from"] == "store@x.com"


def test_sender_and_to_field_fallbacks(fetch_order_emails, sanitize_email_body):
    module, _, _ = fetch_order_emails
    sanitize = sanitize_email_body.sanitize_message
    # Uses `sender` / `toAddress` instead of `from` / `to`, and `id`
    # instead of `messageId` — the projection's documented fallbacks.
    msg = {
        "id": "x9",
        "sender": "a@b.com",
        "toAddress": "me@b.com",
        "subject": "S",
        "messageText": "body",
    }
    result = module.fetch_order_emails(_fake_execute({"q": _ok([msg])}), sanitize, ["q"])
    row = result["messages"][0]
    assert row["messageId"] == "x9"
    assert row["from"] == "a@b.com"
    assert row["to"] == "me@b.com"


def test_sanitizer_runs_before_projection(fetch_order_emails):
    """Integration contract: every kept message passes through the
    loaded sanitizer BEFORE the compact-row projection reads its
    body-ish fields. The sanitizer's own body-cap / invisible-unicode
    behavior is heartbeat's (tested in admin's suite); what this tile
    owns is that the projection only ever sees post-sanitizer state —
    a marking sanitizer double proves the ordering."""
    module, _, _ = fetch_order_emails

    def marking_sanitize(msg):
        msg["messageText"] = "SANITIZED::" + str(msg.get("messageText"))
        return msg

    msg = {"messageId": "m", "subject": "S", "messageText": "raw body"}
    result = module.fetch_order_emails(_fake_execute({"q": _ok([msg])}), marking_sanitize, ["q"])
    assert result["messages"][0]["body"] == "SANITIZED::raw body"


def test_snippet_dict_coerced_to_text(fetch_order_emails, sanitize_email_body):
    module, _, _ = fetch_order_emails
    sanitize = sanitize_email_body.sanitize_message
    # Composio's v3 REST returns `snippet` as a {body, subject} object,
    # not a plain string (observed live: 80/80 rows) — the projection must
    # flatten it to text so Step 4 parses a string, not a nested dict.
    msg = {
        "messageId": "m",
        "subject": "S",
        "messageText": "full body text",
        "snippet": {"body": "preview text", "subject": "S"},
    }
    result = module.fetch_order_emails(_fake_execute({"q": _ok([msg])}), sanitize, ["q"])
    row = result["messages"][0]
    assert isinstance(row["snippet"], str)
    assert row["snippet"] == "preview text"
    assert isinstance(row["body"], str)
    assert row["body"] == "full body text"


def test_as_text_coercion_cases(fetch_order_emails):
    module, _, _ = fetch_order_emails
    assert module._as_text("hi") == "hi"
    assert module._as_text({"body": "b", "subject": "s"}) == "b"
    assert module._as_text({"subject": "s"}) == "s"
    assert module._as_text({"other": 1}) == ""
    assert module._as_text(["a", 2, "b"]) == "a b"
    assert module._as_text(None) == ""


def test_per_query_exception_becomes_error_marker(fetch_order_emails, sanitize_email_body):
    module, _, _ = fetch_order_emails
    sanitize = sanitize_email_body.sanitize_message
    by_query = {
        "good": _ok([{"messageId": "m1", "subject": "S", "messageText": "b"}]),
        "bad": OSError("connection reset"),
    }
    result = module.fetch_order_emails(_fake_execute(by_query), sanitize, ["good", "bad"])
    assert [m["messageId"] for m in result["messages"]] == ["m1"]
    assert len(result["errors"]) == 1
    assert result["errors"][0]["query"] == "bad"
    assert "connection reset" in result["errors"][0]["error"]


def test_tool_error_envelope_becomes_error_marker(fetch_order_emails, sanitize_email_body):
    module, _, _ = fetch_order_emails
    sanitize = sanitize_email_body.sanitize_message
    by_query = {"q": {"successful": False, "error": "rate limited", "data": {}}}
    result = module.fetch_order_emails(_fake_execute(by_query), sanitize, ["q"])
    assert result["messages"] == []
    assert result["errors"] == [{"query": "q", "error": "rate limited"}]


def test_extract_messages_tolerates_nested_data(fetch_order_emails):
    module, _, _ = fetch_order_emails
    nested = {"data": {"data": {"messages": [{"messageId": "m"}]}}}
    assert module._extract_messages(nested) == [{"messageId": "m"}]
    flat = {"messages": [{"messageId": "n"}]}
    assert module._extract_messages(flat) == [{"messageId": "n"}]
    assert module._extract_messages({"data": {}}) == []


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


def test_main_fail_closed_when_sanitizer_missing(fetch_order_emails, capsys):
    module, sanitizer_path, _ = fetch_order_emails
    assert not sanitizer_path.exists()
    rc = module.main()
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert "sanitizer unavailable" in captured.err
    assert "Refusing" in captured.err


def test_main_fail_closed_when_credentials_missing(fetch_order_emails, monkeypatch, capsys):
    module, _, _ = fetch_order_emails
    # Point both loaders at the local fakes so main() reaches the
    # credential pre-flight; then drop the creds. The fake composio-rest
    # mirrors the heartbeat module's load surface (MissingCredentials,
    # MISSING_CREDENTIALS_HINT, require_credentials, composio_execute).
    monkeypatch.setattr(module, "SANITIZER_MOUNT", FAKE_SANITIZER)
    monkeypatch.setattr(module, "COMPOSIO_REST_MOUNT", FAKE_COMPOSIO_REST)
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)
    monkeypatch.delenv("COMPOSIO_USER_ID", raising=False)
    rc = module.main()
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert "missing Composio credentials" in captured.err
    assert "COMPOSIO_API_KEY" in captured.err


def test_main_fail_closed_when_rest_helper_missing(
    fetch_order_emails, tmp_path, monkeypatch, capsys
):
    module, _, _ = fetch_order_emails
    monkeypatch.setattr(module, "SANITIZER_MOUNT", FAKE_SANITIZER)
    monkeypatch.setattr(module, "COMPOSIO_REST_MOUNT", str(tmp_path / "composio-rest.py"))
    monkeypatch.setattr(module, "COMPOSIO_REST_FALLBACK", tmp_path / "composio-rest.py")
    rc = module.main()
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert "REST helper unavailable" in captured.err
