#!/usr/bin/env python3
"""Compute a stable order ID from source + order_date + description.

Usage:
    compute-order-id.py <source> <order_date> <description>

Writes to stdout:
    {source}-{order_date}-{hash}

where `hash` is the first 8 hex characters of SHA-1 over the
description string (UTF-8 bytes).
"""

import hashlib
import sys


def main() -> None:
    if len(sys.argv) != 4:
        print(
            f"Usage: {sys.argv[0]} <source> <order_date> <description>",
            file=sys.stderr,
        )
        sys.exit(2)

    source, order_date, description = sys.argv[1], sys.argv[2], sys.argv[3]
    digest = hashlib.sha1(description.encode("utf-8")).hexdigest()[:8]
    print(f"{source}-{order_date}-{digest}")


if __name__ == "__main__":
    main()
