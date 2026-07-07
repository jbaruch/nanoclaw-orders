"""Local test double for heartbeat's sanitize-email-body.py.

Identity sanitizer exposing the surface fetch-order-emails.py loads
(`sanitize_message`, `sanitize`). The real sanitizer's behavior
(body cap, invisible-unicode collapse) is covered in
jbaruch/nanoclaw-admin's heartbeat suite; this fake exists so
`main()`'s load-and-preflight sequence is testable locally.
"""


def sanitize(text, max_len=2000):
    return text


def sanitize_message(msg):
    return msg
