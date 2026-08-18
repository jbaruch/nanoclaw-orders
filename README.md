# jbaruch/nanoclaw-orders

[![tessl](https://img.shields.io/endpoint?url=https%3A%2F%2Fapi.tessl.io%2Fv1%2Fbadges%2Fjbaruch%2Fnanoclaw-orders)](https://tessl.io/registry/jbaruch/nanoclaw-orders)

Order-email triage for NanoClaw. Fetches order-related Gmail, keeps the `orders` SQLite table current, and flags recent anomalies — cancellations/refunds, overdue deliveries, and orders stuck in `ordered` that never shipped — surfacing only the flagged events so the alert channel stays signal-only.

Per-chat overlay plugin. Install via NanoClaw's `containerConfig.additionalTiles` mechanism.

## Capabilities

1. **Order-email fetch** — native Gmail multi-query fetch over the Gmail REST API, brokered by the OneCLI gateway, cursor-bounded by `last_checked`, sanitized inside the container before any body reaches the session
2. **Order table maintenance** — upserts each order on `email_message_id` into the `orders` table; idempotent across overlapping fetch windows
3. **Anomaly flagging** — flags cancellations/refunds, overdue deliveries, and orders stuck in `ordered` that never shipped (statuses and per-status cutoffs owned by `flag-anomalies.py`; the stuck window and `(source, order_number)` pairing owned by `compute-stuck-orders.py`)
4. **Signal-only alerts** — silent on normal order flow; older flagged events age out automatically
5. **Scheduled refresh** — the `nightly-order-sync` cadence wrapper runs the lookup on a 3-day-capped `15 6` cadence and emits the observable-silence cursor marker the silent-success watchdog reads

## Installation

```
tessl install jbaruch/nanoclaw-orders
```

Add to a chat's overlay plugin list via `update_group_config`:

```
additionalTiles: ["nanoclaw-orders"]
```

Load the overlay at the **main or trusted** tier. Two reasons: the Gmail fetch reaches Google through the OneCLI gateway, which injects credentials only for main/trusted agents (the untrusted tier runs `secretMode: selective` and is gated from Google by design), and the `orders` table lives in `messages.db`, which is mounted read-write only on main/trusted (read-only filtered copy on untrusted).

## Required environment

None. This container holds no Google credential (`jbaruch/nanoclaw#638`): the OneCLI TLS-MITM gateway owns the OAuth connection and injects `Authorization: Bearer` on the wire to the Google API hosts, refreshing the token itself. The fetch sends no auth header and reads no key from the environment — `COMPOSIO_API_KEY` / `COMPOSIO_USER_ID` are gone.

The gateway reaches the fetch via `HTTPS_PROXY` + the mounted CA bundle, both set on the spawn by the orchestrator. When it isn't on the request path, Google answers 401 and the fetch fails closed with an operator-actionable diagnostic rather than retrying.

## Runtime data

The skill reads and writes the orchestrator's `messages.db` under the `/workspace/store/` mount (read-write on main/trusted):

| Table | Access | Owner |
|-------|--------|-------|
| `orders` | read+write | this plugin |
| `orders_metadata` (`last_checked` cursor) | read+write | this plugin |

`nanoclaw-admin`'s `morning-brief` and `check-email` skills read flagged orders from the same `orders` table. Admin co-loads with this overlay in the same chat via the shared store mount. The cross-plugin reads resolve through that mount.

## Cross-plugin dependency

`scripts/fetch-order-emails.py` loads four shared helpers owned by `nanoclaw-admin`'s `heartbeat` skill at runtime via the co-loaded `tessl__heartbeat` plugin mount:

- `sanitize-email-body.py` — `sanitize()`, applied to every text field inside the container before it reaches the session
- `google-rest.py` — the native Google REST transport over the OneCLI gateway (`google_request`, `surface_url`, and the `GatewayNotInjecting` / `TierAccessRestricted` faults)
- `gmail-ops.py` — `list_messages` / `get_message` against the Gmail REST API
- `gmail-message.py` — flattens a raw `users.messages.get` resource (nested MIME tree, base64url bodies, raw header list) into sanitized fields

All four resolve when admin co-loads with this overlay (the owner's main/trusted chat). The fetch fails closed if any is unavailable.

## Skills

| Skill | Description |
|-------|-------------|
| [check-orders](skills/check-orders/SKILL.md) | Fetches order-related emails from Gmail, updates the orders SQLite table, and flags recent anomalies. Use when the user asks about order status, order tracking, order emails, shipment status, purchase alerts, or needs to sync Gmail order data with the orders database. |
| [nightly-order-sync](skills/nightly-order-sync/SKILL.md) | Cadence wrapper (cron `15 6`, precheck-gated by a 3-day cadence cursor) that runs `check-orders` on a schedule, surfaces only its order alerts, and emits the observable-silence cursor marker the silent-success watchdog reads. |

## Skill scripts

`check-orders` invokes these deterministic scripts from its SKILL.md steps:

- `scripts/read-last-checked.py` — reads the `last_checked` cursor from `orders_metadata`
- `scripts/fetch-order-emails.py` — multi-query native Gmail fetch, cross-query dedup, in-container sanitization, compact-row projection
- `scripts/compute-order-id.py` — deterministic SHA-1-prefix order id from `(source, order_date, description)`
- `scripts/extract-amount.py` — extracts the order total from a sanitized email, preferring a labeled total line over any largest-amount pick
- `scripts/classify-order.py` — maps sender domain → `source` and subject/snippet keywords → `status`
- `scripts/apply-order.py` — upserts an order row on `email_message_id`
- `scripts/apply-exclusions.py` — owns the user-preference exclusion table and matching; unflags matches and emits the id list flagging consumes via `EXCLUDED_IDS`
- `scripts/compute-stuck-orders.py` — computes the ids of orders stuck in `ordered` with no shipment, pairing on the persisted `(source, order_number)` key
- `scripts/flag-anomalies.py` — applies the anomaly predicates (owns the statuses and per-status age cutoffs); flags the supplied stuck ids
- `scripts/get-flagged-orders.py` — returns currently-flagged orders for the alert channel, collapsing rows that share a `(source, order_number)` order
- `scripts/render-order-alerts.py` — HTML-escapes flagged rows into the ready-to-send Telegram alert envelope
- `scripts/unflag-orders.py` — clears flags the user has acknowledged (ad-hoc, outside the Step 6 flow)
- `scripts/promote-stale-shipped.py` — ages shipped orders past the delivery window into the overdue state
- `scripts/within-days.py` — date-window predicate helper
- `scripts/write-orders-metadata.py` — write-ahead cursor + metadata writer

The `nightly-order-sync` cadence wrapper carries its own scripts:

- `scripts/precheck-nightly-order-sync.py` — fire-time precheck that gates wake-ups by the cadence cursor
- `scripts/stamp-cursor.py` — advances the success cursor after a clean run

## Status

- **V1** — migrated `check-orders` + its `nightly-order-sync` cadence wrapper from `nanoclaw-admin` as a standalone per-chat overlay plugin (`jbaruch/nanoclaw-admin#319`). The wrapper materialises one `scheduled_tasks` row in chats that load this overlay.

See [CHANGELOG.md](CHANGELOG.md) for version history.

## Development dependencies

`tessl.json` declares this repo's dev-time plugin dependencies.

- Every `jbaruch/*` dependency floats at `latest` (Runtime-Managed Manifest Carve-Out, `jbaruch/coding-policy: dependency-management`).
- `finsi/codex-review` is third-party and pins. No dependency scanner covers the tessl ecosystem. Renewal cadence: quarterly — run `tessl outdated` and bump the pin in its own commit.
