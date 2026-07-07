# jbaruch/nanoclaw-orders

[![tessl](https://img.shields.io/endpoint?url=https%3A%2F%2Fapi.tessl.io%2Fv1%2Fbadges%2Fjbaruch%2Fnanoclaw-orders)](https://tessl.io/registry/jbaruch/nanoclaw-orders)

Order-email triage for NanoClaw. Fetches order-related Gmail, keeps the `orders` SQLite table current, and flags recent anomalies — cancellations/refunds, large purchases until delivered, and overdue deliveries — surfacing only the flagged events so the alert channel stays signal-only.

Per-chat overlay tile. Install via NanoClaw's `containerConfig.additionalTiles` mechanism.

## Capabilities

1. **Order-email fetch** — Composio Gmail multi-query fetch over the v3 REST endpoint, cursor-bounded by `last_checked`, sanitized inside the container before any body reaches the session
2. **Order table maintenance** — upserts each order on `email_message_id` into the `orders` table; idempotent across overlapping fetch windows
3. **Anomaly flagging** — flags cancellations/refunds, large purchases until delivered, and overdue deliveries (statuses, dollar threshold, and age cutoffs owned by `flag-anomalies.py`)
4. **Signal-only alerts** — silent on normal order flow; older flagged events age out automatically
5. **Scheduled refresh** — the `nightly-order-sync` cadence wrapper runs the lookup on a 3-day-capped `15 6` cadence and emits the observable-silence cursor marker the silent-success watchdog reads

## Installation

```
tessl install jbaruch/nanoclaw-orders
```

Add to a chat's overlay tile list via `update_group_config`:

```
additionalTiles: ["nanoclaw-orders"]
```

Load the overlay at the **main or trusted** tier. Two reasons: the Gmail fetch needs the Composio credentials NanoClaw forwards only to main/trusted containers, and the `orders` table lives in `messages.db`, which is mounted read-write only on main/trusted (read-only filtered copy on untrusted).

## Required environment

| Variable | Purpose |
|----------|---------|
| `COMPOSIO_API_KEY` | Composio v3 REST auth for the Gmail fetch |
| `COMPOSIO_USER_ID` | Composio connected-account id for the owner's Gmail |

NanoClaw forwards both into main/trusted containers; the tile reads them at fetch time and fails closed (no fetch) when either is unset.

## Runtime data

The skill reads and writes the orchestrator's `messages.db` under the `/workspace/store/` mount (read-write on main/trusted):

| Table | Access | Owner |
|-------|--------|-------|
| `orders` | read+write | this tile |
| `orders_metadata` (`last_checked` cursor) | read+write | this tile |

`nanoclaw-admin`'s `morning-brief` and `check-email` skills read flagged orders from the same `orders` table; those cross-tile reads resolve because admin co-loads with this overlay in the same chat via the shared store mount.

## Cross-tile dependency

`scripts/fetch-order-emails.py` loads two shared helpers owned by `nanoclaw-admin`'s `heartbeat` skill at runtime via the co-loaded `tessl__heartbeat` tile mount:

- `sanitize-email-body.py` — sanitizes message bodies inside the container before they reach the session
- `composio-rest.py` — the Composio v3 REST execute helper + credential preflight

Both resolve when admin co-loads with this overlay (the owner's main/trusted chat). The fetch fails closed if either is unavailable.

## Skills

| Skill | Description |
|-------|-------------|
| [check-orders](skills/check-orders/SKILL.md) | Fetches order-related emails from Gmail, updates the orders SQLite table, and flags recent anomalies. Use when the user asks about order status, order tracking, order emails, shipment status, purchase alerts, or needs to sync Gmail order data with the orders database. |
| [nightly-order-sync](skills/nightly-order-sync/SKILL.md) | Cadence wrapper (cron `15 6`, precheck-gated by a 3-day cadence cursor) that runs `check-orders` on a schedule, surfaces only its order alerts, and emits the observable-silence cursor marker the silent-success watchdog reads. |

## Skill scripts

`check-orders` invokes these deterministic scripts from its SKILL.md steps:

- `scripts/read-last-checked.py` — reads the `last_checked` cursor from `orders_metadata`
- `scripts/fetch-order-emails.py` — multi-query Composio Gmail fetch, cross-query dedup, in-container sanitization, compact-row projection
- `scripts/compute-order-id.py` — deterministic SHA-1-prefix order id from `(source, order_date, description)`
- `scripts/apply-order.py` — upserts an order row on `email_message_id`
- `scripts/apply-exclusions.py` — owns the user-preference exclusion table and matching; unflags matches and emits the id list Step 8 consumes via `EXCLUDED_IDS`
- `scripts/flag-anomalies.py` — applies the anomaly predicates (owns the statuses, dollar threshold, age cutoffs)
- `scripts/get-flagged-orders.py` — returns currently-flagged orders for the alert channel
- `scripts/render-order-alerts.py` — HTML-escapes flagged rows into the ready-to-send Telegram alert envelope
- `scripts/unflag-orders.py` — clears flags the user has acknowledged (ad-hoc, outside the Step 6 flow)
- `scripts/promote-stale-shipped.py` — ages shipped orders past the delivery window into the overdue state
- `scripts/within-days.py` — date-window predicate helper
- `scripts/write-orders-metadata.py` — write-ahead cursor + metadata writer

The `nightly-order-sync` cadence wrapper carries its own scripts:

- `scripts/precheck-nightly-order-sync.py` — fire-time precheck that gates wake-ups by the cadence cursor
- `scripts/stamp-cursor.py` — advances the success cursor after a clean run

## Status

- **V1** — migrated `check-orders` + its `nightly-order-sync` cadence wrapper from `nanoclaw-admin` as a standalone per-chat overlay tile (`jbaruch/nanoclaw-admin#319`). The wrapper materialises one `scheduled_tasks` row in chats that load this overlay.

See [CHANGELOG.md](CHANGELOG.md) for version history.
