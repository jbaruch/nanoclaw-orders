#!/usr/bin/env python3
"""Render flagged orders as a ready-to-send Telegram HTML alert.

Step 12 of check-orders SKILL.md: `get-flagged-orders.py` emits raw
rows whose `description` derives from email subject/body text —
untrusted, sender-controlled content. A subject containing `<`, `>`,
`&`, or Telegram HTML tags would break the message parse (suppressing
the alert) or inject unintended formatting/links into the
notification. This script owns the rendering: it HTML-escapes every
field before interpolating into the Telegram HTML template, so the
agent sends its stdout verbatim and never rebuilds the message by
hand (per `coding-policy: script-delegation`).

Input (stdin): the JSON array `get-flagged-orders.py` prints —
`[{"description", "flag_reason", "source", "order_date", "merchant"}, ...]`.

Output (stdout): a single JSON object (structured data per
`coding-policy: script-delegation`):

    {"message": "<full Telegram HTML text>" | null, "count": <int>}

`message` is the complete ready-to-send Telegram HTML, one bullet per
order in input order:

    <b>📦 Order alerts:</b>

    • <b>{description}</b> — {flag_reason} (<i>{merchant or source}, {order_date}</i>)

The meta leads with `merchant` when present and falls back to `source`,
so a flagged item whose `source` is `other` is still identifiable
(`jbaruch/nanoclaw-orders#55`).

An empty input array yields `{"message": null, "count": 0}` (the
SKILL's stay-silent signal). Fields are coerced to str and
HTML-escaped; null fields render as empty strings.

Exit codes: 0 success (including empty input), 1 malformed input
(non-JSON stdin, a non-array payload, or a non-object array element)
with a stderr diagnostic and no stdout.
"""

from __future__ import annotations

import html
import json
import sys

HEADER = "<b>📦 Order alerts:</b>"


def _esc(value) -> str:
    """Coerce a row field to text and HTML-escape it. None renders as
    an empty string — flagged rows always carry a reason, but the
    template must not crash on a hand-edited NULL."""
    if value is None:
        return ""
    return html.escape(str(value))


def render(orders: list) -> str:
    """Return the full Telegram HTML message, or '' for no orders."""
    if not orders:
        return ""
    lines = [HEADER, ""]
    for order in orders:
        # Prefer the specific merchant; fall back to the coarse source
        # (amazon/shopify/other) when no merchant was captured.
        identifier = order.get("merchant") or order.get("source")
        lines.append(
            f"• <b>{_esc(order.get('description'))}</b> — "
            f"{_esc(order.get('flag_reason'))} "
            f"(<i>{_esc(identifier)}, {_esc(order.get('order_date'))}</i>)"
        )
    return "\n".join(lines)


def main() -> int:
    raw = sys.stdin.read()
    try:
        orders = json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.stderr.write(
            f"render-order-alerts: stdin is not valid JSON ({exc}). "
            f"Pipe get-flagged-orders.py output directly into this "
            f"script — do not hand-edit it.\n"
        )
        return 1
    if not isinstance(orders, list):
        sys.stderr.write(
            f"render-order-alerts: expected a JSON array of order rows, "
            f"got {type(orders).__name__}. Pipe get-flagged-orders.py "
            f"output directly into this script.\n"
        )
        return 1
    non_dicts = sorted({type(o).__name__ for o in orders if not isinstance(o, dict)})
    if non_dicts:
        sys.stderr.write(
            f"render-order-alerts: every array element must be an order "
            f"object; got {', '.join(non_dicts)}. Pipe get-flagged-orders.py "
            f"output directly into this script.\n"
        )
        return 1
    message = render(orders)
    json.dump({"message": message or None, "count": len(orders)}, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
