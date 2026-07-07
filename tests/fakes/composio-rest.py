"""Local test double for heartbeat's composio-rest.py.

fetch-order-emails.py loads the real module at runtime from the
co-loaded `tessl__heartbeat` tile mount; this tile does not ship it.
This fake exposes the exact surface `main()` touches —
`MissingCredentials`, `MISSING_CREDENTIALS_HINT`,
`require_credentials()`, `composio_execute()` — so the fail-closed
credential preflight is testable locally without the heartbeat
sibling. Behavior mirrors the real module's contract; the REST caller
itself is never exercised here and raises if reached.
"""

import os


class MissingCredentials(Exception):
    """Raised when the Composio credential env vars are unset."""


MISSING_CREDENTIALS_HINT = "set COMPOSIO_API_KEY and COMPOSIO_USER_ID in the container environment"


def require_credentials() -> None:
    missing = [k for k in ("COMPOSIO_API_KEY", "COMPOSIO_USER_ID") if not os.environ.get(k)]
    if missing:
        raise MissingCredentials(", ".join(missing) + " unset")


def composio_execute(action, arguments):
    raise NotImplementedError(
        "fake composio-rest: composio_execute must not be reached in "
        "fail-closed tests — inject a fake execute via "
        "fetch_order_emails() for happy-path coverage"
    )
