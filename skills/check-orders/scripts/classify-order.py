#!/usr/bin/env python3
"""Classify an order email's `source` and `status` from its headers/subject.

Step 4 of `check-orders/SKILL.md` reads `source` and `status` from here rather
than from a prose table. Both are fully-enumerable deterministic maps (sender
domain → source, subject/snippet keyword → status), so they belong in a tested
script per `jbaruch/coding-policy: script-delegation` / `script-as-black-box`.

Why this exists as a script now (`jbaruch/nanoclaw-orders#44`)
-------------------------------------------------------------
The Shopify fetch query moved to `from:t.shopifyemail.com` (Shopify's
transactional sending subdomain). Those emails' `From` is
`store+NNN@t.shopifyemail.com`, not `shopify.com`, and their subject reads
`Order #NNN confirmed`, not `order confirmation`. Under the old prose maps they
would have classified as `source="other"` / `status="unknown"` — fetched but
useless. So `shopifyemail.com` normalizes to `shopify`, and `confirmed` joins
the `ordered` keywords.

Source (matched against the `From` domain, suffix match so subdomains count):
  amazon.com → amazon; shopifyemail.com / shopify.com → shopify;
  shop.app → shop; anything else → other.
Merchant-custom-domain Shopify senders (e.g. support@pacagen.com) carry no
Shopify signal in the domain and classify as `other` — the known #44 limitation.

Status (keyword match on `subject + snippet`, rules tried top-to-bottom, first
hit wins so a shipped/delivered/cancelled/refunded signal outranks a bare
`confirmed`):
  shipped   ← "on the way", "shipped"
  delivered ← "has been delivered", "delivered"
  cancelled ← "cancelled", "canceled"
  refunded  ← "refunded", "refund"
  ordered   ← "order confirmation", "ordered", "confirmed"
  unknown   ← no keyword matched

Stdin contract: a single JSON object with `from`, `subject`, `snippet` strings
(any may be absent or null). Stdout on success: `{"source": "<s>", "status":
"<st>"}`. Exit codes: 0 success, 2 usage error (no JSON on stdin, or the
payload is not a JSON object).
"""

from __future__ import annotations

import json
import sys

# Suffix → source. Order matters only for readability; the suffixes are
# disjoint. Subdomains match by suffix, so `t.shopifyemail.com` → shopify.
_SOURCE_SUFFIXES = (
    ("amazon.com", "amazon"),
    ("shopifyemail.com", "shopify"),
    ("shopify.com", "shopify"),
    ("shop.app", "shop"),
)

# Status keyword rules, highest priority first. First rule with any keyword in
# `subject + snippet` wins, so a shipment/cancellation/refund signal is never
# masked by the `confirmed` in a subject like "Order #12 confirmed ... shipped".
_STATUS_RULES = (
    ("shipped", ("on the way", "shipped")),
    ("delivered", ("has been delivered", "delivered")),
    ("cancelled", ("cancelled", "canceled")),
    ("refunded", ("refunded", "refund")),
    ("ordered", ("order confirmation", "ordered", "confirmed")),
)


def _domain(from_header: str | None) -> str:
    if not from_header or "@" not in from_header:
        return ""
    tail = from_header.rsplit("@", 1)[1]
    # Strip an angle-bracket/paren wrapper and any trailing tokens, then lower.
    return tail.strip().rstrip(">)").split()[0].rstrip(">)").lower() if tail.strip() else ""


def classify_source(from_header: str | None) -> str:
    domain = _domain(from_header)
    for suffix, source in _SOURCE_SUFFIXES:
        if domain == suffix or domain.endswith("." + suffix):
            return source
    return "other"


def classify_status(subject: str | None, snippet: str | None) -> str:
    text = f"{subject or ''} {snippet or ''}".lower()
    for status, keywords in _STATUS_RULES:
        if any(keyword in text for keyword in keywords):
            return status
    return "unknown"


def classify(from_header: str | None, subject: str | None, snippet: str | None) -> dict:
    return {
        "source": classify_source(from_header),
        "status": classify_status(subject, snippet),
    }


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        sys.stderr.write("classify-order.py: no JSON on stdin\n")
        return 2
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"classify-order.py: invalid JSON on stdin: {exc}\n")
        return 2
    if not isinstance(payload, dict):
        sys.stderr.write("classify-order.py: stdin payload must be a JSON object\n")
        return 2

    result = classify(payload.get("from"), payload.get("subject"), payload.get("snippet"))
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
