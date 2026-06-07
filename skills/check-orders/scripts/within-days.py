#!/usr/bin/env python3
"""Check whether a YYYY-MM-DD date string is within N days of today.

Usage:
    within-days.py <date_str> <days>

Exit codes:
    0  within       — 0 <= (today - date_str).days <= days
    1  not within   — includes empty or malformed date strings
                      (error annotated on stderr so the caller can still see)
    2  invalid args — usage error

`date_str` may carry trailing time/zone info; only the first 10
characters (YYYY-MM-DD) are parsed. `days` must parse as a
non-negative integer.
"""

import sys
from datetime import date


def main() -> None:
    if len(sys.argv) != 3:
        print(
            f"Usage: {sys.argv[0]} <date_str> <days>",
            file=sys.stderr,
        )
        sys.exit(2)

    # Strip once up front so the empty-check and the parse slice both
    # see the same value. A leading-whitespace input like "  2026-04-21"
    # would otherwise pass the strip() check (non-empty after trim)
    # but date.fromisoformat(date_str[:10]) would see "  2026-04-" and
    # raise ValueError, producing a misleading "malformed date" message
    # for what's actually a valid-but-padded input.
    date_str = sys.argv[1].strip()
    days_str = sys.argv[2]

    try:
        days = int(days_str)
    except ValueError:
        print(f"days must be an integer, got: {days_str!r}", file=sys.stderr)
        sys.exit(2)

    if days < 0:
        print(f"days must be non-negative, got: {days}", file=sys.stderr)
        sys.exit(2)

    if not date_str:
        print(
            "within-days: empty or whitespace-only date string",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        parsed = date.fromisoformat(date_str[:10])
    except ValueError:
        print(f"malformed date: {date_str!r}", file=sys.stderr)
        sys.exit(1)

    delta = (date.today() - parsed).days
    sys.exit(0 if 0 <= delta <= days else 1)


if __name__ == "__main__":
    main()
