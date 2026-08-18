"""Local test double for heartbeat's gmail-message.py.

fetch-order-emails.py loads the real module at runtime from the co-loaded
`tessl__heartbeat` plugin mount; this plugin does not ship it (nanoclaw#638).

This fake implements `parse_message` against the REAL native shapes —
`payload.headers` as a raw name/value list, base64url part bodies in a
nested MIME tree, `internalDate` as an epoch-millisecond string — rather
than returning a canned flat dict. The reason is the same one that makes
the gmail-ops fake real: this plugin's projection maps parse_message's
output keys onto the stdout contract, so a fake that invented its own
output keys would assert nothing about the mapping under test.

It is deliberately NOT a copy of the real parser: no HTML-to-text
conversion, no attachment skipping, no malformed-part tolerance. Those
are heartbeat's behaviors, covered by jbaruch/nanoclaw-admin's heartbeat
suite. What this fake guarantees is the contract this plugin consumes:
sanitize() runs on every text field, and the output keys are
{messageId, threadId, labelIds, internalDate (ISO 8601 UTC), from, to,
subject, snippet, body}.
"""

import base64
import datetime


def _decode_b64url(data):
    if not isinstance(data, str) or not data:
        return ""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def _walk_parts(payload, acc):
    """Depth-first leaf collection. The live MIME tree really is nested
    (multipart/mixed -> multipart/related -> text/*), so a one-level read
    would miss the body."""
    if not isinstance(payload, dict):
        return acc
    parts = payload.get("parts")
    if isinstance(parts, list) and parts:
        for part in parts:
            _walk_parts(part, acc)
        return acc
    text = _decode_b64url((payload.get("body") or {}).get("data"))
    if text:
        acc.append((payload.get("mimeType") or "", text))
    return acc


def extract_body(payload):
    parts = _walk_parts(payload, [])
    plain = [t for (m, t) in parts if m == "text/plain"]
    if plain:
        return "\n".join(plain)
    return "\n".join(t for (_, t) in parts)


def header(payload, name):
    if not isinstance(payload, dict):
        return ""
    wanted = name.lower()
    for h in payload.get("headers") or []:
        if isinstance(h, dict) and (h.get("name") or "").lower() == wanted:
            return h.get("value") or ""
    return ""


def _internal_date_iso(raw):
    v = raw.get("internalDate")
    if v in (None, ""):
        return None
    try:
        ms = int(v)
    except (TypeError, ValueError):
        return None
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat()


def parse_message(raw, sanitize):
    if not isinstance(raw, dict):
        return {}
    payload = raw.get("payload") or {}
    return {
        "messageId": raw.get("id") or "",
        "threadId": raw.get("threadId") or "",
        "labelIds": raw.get("labelIds") or [],
        "internalDate": _internal_date_iso(raw),
        "from": sanitize(header(payload, "From"), max_len=300),
        "to": sanitize(header(payload, "To"), max_len=300),
        "subject": sanitize(header(payload, "Subject"), max_len=300),
        "snippet": sanitize(raw.get("snippet") or ""),
        "body": sanitize(extract_body(payload)),
    }
