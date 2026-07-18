#!/usr/bin/env python3
"""Extract the order-total amount from a sanitized order email.

Step 4 of `check-orders/SKILL.md` reads `amount` from here rather than from a
prose rule, because the extraction is trap-laden enough to need a tested black
box (`jbaruch/coding-policy: script-as-black-box`, `jbaruch/nanoclaw-orders#38`).

Why the body, and why not "largest"
-----------------------------------
The pre-#38 rule read `amount` from subject+snippet only, "use largest". A live
sample of real order emails (issue #38) found the total sits ONLY in the body
for 8 of 9 order confirmations: Amazon `Ordered:`/`Shipped:` and store-hosted
Shopify confirmations put nothing but `Ordered: <item>` / `Order #X confirmed`
above the ~200-char snippet fold, so the old rule defaulted `amount` to 0 for
essentially every order. So the body is now in scope.

But "largest amount in the body" is wrong: two of three sampled Shopify bodies
carried a struck-through list price ($98.99, $69.00) LARGER than the true total
($94.14, $62.10). The fix is to prefer a *labeled* total line — a struck list
price is never labeled `Total` — and to restrict the unlabeled largest-amount
fallback to subject+snippet, where the sample showed it is either absent or the
correct refund total. The body's largest raw amount is never taken.

Precedence (the first rule below that matches wins), searched across
`subject | snippet | body`:
  1. A labeled grand/order/final total (`Order Total: $X`, `Grand Total $X`, …).
     Of these, the LAST match in the joined text is used — the grand total is
     printed after any per-line subtotal, and joining the fields
     subject→snippet→body puts the body's total last.
  2. A bare `Total: $X` line (last match, same reasoning). `subtotal`,
     `sub-total`, and `sub total` are excluded, so a subtotal never wins over a
     genuine total.
  3. The largest `$X.XX` in subject+snippet only (the refund / amount-in-subject
     case). Never the body — see above.
  4. Default `0.0`.

So when subject/snippet and body disagree, a labeled total (rules 1-2, in any
field) beats an unlabeled largest-amount pick (rule 3, subject+snippet only).

Sanitizer note: heartbeat's `sanitize()` collapses all whitespace to single
spaces and strips newlines, so the body is one flat line; every pattern here
matches `label … $amount` inline rather than per-line. A total beyond the
sanitizer's 2000-char cap (rare; totals print early) is not seen and falls
through to a lower rule — the same fail-soft as any unparsed field.

Stdin contract: a single JSON object with the sanitized `subject`, `snippet`,
and `body` strings (any may be absent or null). Stdout on success: a single
JSON object `{"amount": <float>, "currency": "USD", "matched": "<rule>"}` where
`matched` is one of `labeled_total`, `bare_total`, `subject_snippet_largest`,
`none`. Exit codes: 0 success, 2 usage error (no JSON on stdin, or the payload
is not a JSON object).
"""

from __future__ import annotations

import json
import re
import sys

# A US-dollar figure with mandatory cents: "109.74", "1,299.00", "0.00".
# Requiring the ".dd" keeps "$5 off" / "$20 gift card" copy from being read as
# an amount — order totals always print cents.
_NUM = r"(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}"
_AMOUNT = re.compile(rf"\$\s?({_NUM})")

# Explicitly-labeled grand/order/final total. These labels name the figure the
# customer was charged; a struck list price or a line item never carries one.
_LABELED_TOTAL = re.compile(
    r"(?:grand\s+total|order\s+total|final\s+total|total\s+charged|"
    r"amount\s+charged|payment\s+total|total\s+amount|total\s+for\s+this\s+order)"
    rf"\s*[:=]?\s*\$\s?({_NUM})",
    re.IGNORECASE,
)

# Bare "Total: $X". Two negative lookbehinds keep a subtotal from being read as
# the total: the letter guard excludes the joined "subtotal" (the "b" before
# "total"), and the `sub[ -]` guard excludes the "sub-total" / "sub total"
# spellings, where the character just before "total" is a hyphen or space and so
# slips past the letter guard. A space-preceded "Order Total" still matches, but
# rule 1 already claims the order/grand total when present, so this fires only
# when a bare total is the sole total label.
_BARE_TOTAL = re.compile(
    rf"(?<![A-Za-z])(?<!sub[ -])total\s*[:=]?\s*\$\s?({_NUM})",
    re.IGNORECASE,
)


def _to_float(raw: str) -> float:
    return round(float(raw.replace(",", "")), 2)


def extract_amount(subject: str | None, snippet: str | None, body: str | None) -> dict:
    subject = subject or ""
    snippet = snippet or ""
    body = body or ""
    joined = f"{subject} | {snippet} | {body}"

    labeled = _LABELED_TOTAL.findall(joined)
    if labeled:
        return {"amount": _to_float(labeled[-1]), "currency": "USD", "matched": "labeled_total"}

    bare = _BARE_TOTAL.findall(joined)
    if bare:
        return {"amount": _to_float(bare[-1]), "currency": "USD", "matched": "bare_total"}

    head_amounts = [_to_float(m) for m in _AMOUNT.findall(f"{subject} {snippet}")]
    if head_amounts:
        return {
            "amount": max(head_amounts),
            "currency": "USD",
            "matched": "subject_snippet_largest",
        }

    return {"amount": 0.0, "currency": "USD", "matched": "none"}


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        sys.stderr.write("extract-amount.py: no JSON on stdin\n")
        return 2
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"extract-amount.py: invalid JSON on stdin: {exc}\n")
        return 2
    if not isinstance(payload, dict):
        sys.stderr.write("extract-amount.py: stdin payload must be a JSON object\n")
        return 2

    result = extract_amount(payload.get("subject"), payload.get("snippet"), payload.get("body"))
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
