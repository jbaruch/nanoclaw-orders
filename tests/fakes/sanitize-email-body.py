"""Local test double for heartbeat's sanitize-email-body.py.

Identity sanitizer exposing the only surface fetch-order-emails.py loads
(`sanitize`). `sanitize_message` / `DEFAULT_FIELDS` are gone with the
Composio field names they mapped (nanoclaw#638) — native field selection
lives in `gmail-message.py` now.

The real sanitizer's behavior (invisible-unicode collapse, body cap) is
covered in jbaruch/nanoclaw-admin's heartbeat suite. This fake exists so
`main()`'s load sequence and the projection contract are testable locally
without the heartbeat sibling; identity keeps the assertions about
fetch-order-emails' own logic readable.
"""


def sanitize(text, max_len=2000):
    return text
