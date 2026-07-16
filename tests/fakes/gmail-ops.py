"""Local test double for heartbeat's gmail-ops.py.

fetch-order-emails.py loads the real module at runtime from the co-loaded
`tessl__heartbeat` tile mount; this tile does not ship it (nanoclaw#638).
This fake mirrors the real module's contract rather than stubbing it out,
because that contract is exactly what this tile's fetch logic is built on:

  - `list_messages` answers a query with `{id, threadId}` STUBS ONLY,
    unwrapped from Gmail's `{"messages": [...]}` envelope, and returns []
    when the mailbox has no match (Gmail omits the key entirely).
  - `get_message` returns the raw `users.messages.get` resource.

A stub that returned full messages from `list_messages` would hide the
N+1 the script is written around, and the dedup-before-`get` assertions
would prove nothing. The real module's own behavior (param encoding,
pagination refusal) is covered in jbaruch/nanoclaw-admin's heartbeat
suite; what this tile owns is the fan-out built on top.
"""

_USER = "me"


def _gmail_path(*segments):
    return "/".join(("users", _USER) + segments)


def list_messages(
    google_request,
    *,
    limit,
    label_ids=None,
    query=None,
    include_spam_trash=False,
    surface_url,
):
    params = {"maxResults": limit, "includeSpamTrash": str(bool(include_spam_trash)).lower()}
    if label_ids:
        params["labelIds"] = list(label_ids)
    if query:
        params["q"] = query
    resp = google_request("GET", surface_url("gmail", _gmail_path("messages")), params=params)
    messages = resp.get("messages")
    return messages if isinstance(messages, list) else []


def get_message(google_request, message_id, *, fmt="full", surface_url):
    return google_request(
        "GET",
        surface_url("gmail", _gmail_path("messages", message_id)),
        params={"format": fmt},
    )
