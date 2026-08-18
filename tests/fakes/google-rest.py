"""Local test double for heartbeat's google-rest.py.

fetch-order-emails.py loads the real module at runtime from the co-loaded
`tessl__heartbeat` plugin mount; this plugin does not ship it. This fake
exposes the exact surface the script touches — `GatewayNotInjecting`,
`TierAccessRestricted`, `surface_url()`, `google_request()` — so the
module-load path is testable locally without the heartbeat sibling
(nanoclaw#638).

`surface_url` is real: the tests' fake transport routes on the URL it
builds, so a fake that invented its own URL shape would assert nothing
about the real one. `google_request` is never exercised through this
module — tests inject their own transport, so reaching this one is a bug
and it says so rather than silently returning empty.
"""

DEFAULT_API_BASES = {
    "gmail": "https://gmail.googleapis.com/gmail/v1",
    "calendar": "https://www.googleapis.com/calendar/v3",
    "tasks": "https://tasks.googleapis.com/tasks/v1",
    "drive": "https://www.googleapis.com/drive/v3",
}


class GatewayNotInjecting(RuntimeError):
    """Google answered 401 — the OneCLI gateway is not on the request path
    or the app is disconnected. Config fault, not transient."""


class TierAccessRestricted(RuntimeError):
    """The gateway refused to inject for this agent's tier (untrusted runs
    with secretMode=selective). Expected, not a fault."""


def surface_url(surface, path):
    if surface not in DEFAULT_API_BASES:
        raise KeyError(f"unknown Google surface {surface!r}; known: {sorted(DEFAULT_API_BASES)}")
    return f"{DEFAULT_API_BASES[surface]}/{path.lstrip('/')}"


def google_request(method, url, *, params=None, body=None):
    raise NotImplementedError(
        "fake google-rest: google_request must not be reached — inject a fake "
        "transport via bind_gmail() for happy-path coverage"
    )
