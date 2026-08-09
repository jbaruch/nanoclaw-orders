---
name: check-orders
description: Fetches order-related emails from Gmail, updates the orders SQLite table, and flags recent anomalies — cancellations/refunds and overdue deliveries (statuses and per-status cutoffs owned by flag-anomalies.py), plus orders stuck in 'ordered' that never shipped (the stuck age window owned by compute-stuck-orders.py). Silent on normal order flow; older flagged events age out automatically to keep the alert channel signal-only. Use when the user asks about order status, order tracking, order emails, shipment status, purchase alerts, or needs to sync Gmail order data with the orders database.
---

# Check Orders

Process steps in order. Do not skip ahead.

You are AyeAye, Baruch's assistant. Check for order updates from Gmail and update the orders DB.

## Core Rule — Never read raw Gmail into the session

Raw bodies can carry invisible-Unicode padding that blows up the context window. Step 2's fetch script fetches over native Gmail REST and sanitizes inside the container; only its sanitized stdout reaches you. Never fetch Gmail yourself from the session. Background: `/workspace/group/nanoclaw-poison-defense.md`.

## Step 1 — Read last_checked

Orders live in `orders` table of `/workspace/store/messages.db`. Markers live in `orders_metadata` kv table.

```bash
python3 scripts/read-last-checked.py
```

Stdout: `{"last_checked": "<iso>" | null}` (`null` on fresh DB). This read is informational; `fetch-order-emails.py` re-reads the cursor for the `after:` filter and Step 3 stamps write-ahead.

## Step 2 — Fetch order-related emails (sanitized, over native Gmail REST)

Query strings, cross-query dedup, in-container sanitization, compact-row projection, and the cursor-based `after:` filter live in the fetch script. It fetches via the native Gmail REST API — brokered by the OneCLI gateway, per `Google Tool Access` rule — and sanitizes before printing, so raw bodies never enter the session (Core Rule). No credential lives in this container: the gateway injects the Bearer on the wire.

```bash
python3 scripts/fetch-order-emails.py
```

Reads `orders_metadata.last_checked` and appends ` after:YYYY/MM/DD` to each query when set (unbounded otherwise). Loads its shared helpers from `tessl__heartbeat/scripts/` (`sanitize-email-body.py`, `google-rest.py`, `gmail-ops.py`, `gmail-message.py`). Stdout:

```json
{"messages": [{"messageId": "...", "threadId": "...", "from": "...", "to": "...", "subject": "...", "snippet": "...", "body": "...", "date": "...", "labelIds": [...]}], "errors": [{"query": "...", "error": "..."}]}
```

`messages` is the sanitized, deduped input for Step 4. `snippet` is Gmail's short preview — it and `subject` are what Step 4's status rule reads. `body` is the full extracted text; Step 4's `amount` extractor reads it (order totals sit below the snippet fold — `jbaruch/nanoclaw-orders#38`), while the status rule stays on subject+snippet. `date` is ISO 8601 UTC.

Exits non-zero with no stdout (fail-closed) if a shared helper can't be loaded, if the gateway isn't injecting, or if this tier is restricted from Google — the stderr names the remediation.

### Error Handling

| Failure | Action |
|---|---|
| `fetch-order-emails.py` exits non-zero (a `tessl__heartbeat` helper unavailable, gateway not injecting, or tier restricted) | Hard fail. Do NOT fall back to fetching Gmail yourself. Report the skip (with the script's stderr remediation) via `mcp__nanoclaw__send_message`. Skip Step 3. |
| All 5 queries appear in `errors` **and** `messages` is empty (nothing was fetched at all) | Skip run. Skip Step 3. Return nothing. |
| Some queries errored, others returned (data or empty) | Proceed. Log the errored queries. Run Step 3. An `errors` entry naming a single message id is one unreadable email, not a failed query — log it and carry on. |
| All 5 queries succeeded with zero messages | Proceed. Run Step 3 (cursor must advance). |
| All 5 queries succeeded with messages | Proceed. Run Step 3. |
| Script prints non-parseable JSON | Skip Step 3, no metadata update. Next invocation retries. |

## Step 3 — Stamp cursor write-ahead

After Step 2 returns parseable JSON from a successful Gmail query, stamp `orders_metadata.last_checked` to current UTC:

```bash
python3 scripts/write-orders-metadata.py
```

Write-ahead rationale: `skills/check-orders/references/write-ahead-rationale.md`.

Proceed immediately to Step 4.

## Step 4 — Parse each email

For `source` and `status`, run the classifier per email against the fields Step 2 handed you:

```bash
echo '{"from": "...", "subject": "...", "snippet": "..."}' | python3 scripts/classify-order.py
```

Stdout: `{"source": "amazon" | "shopify" | "shop" | "other", "status": "shipped" | "delivered" | "cancelled" | "refunded" | "ordered" | "unknown"}`. Use both values as returned. The sender-domain and keyword maps are owned by the script (`jbaruch/nanoclaw-orders#44`) — do not re-derive by eye.

**Remaining fields:**

| Field | Extraction rule |
|-------|----------------|
| `amount` | Order total in USD. Pipe the sanitized `{subject, snippet, body}` to `scripts/extract-amount.py` and use its `amount`. The label-precedence and fallback rules are owned by the script (`jbaruch/nanoclaw-orders#38`) — do not re-derive the amount by eye. |
| `currency` | `"USD"` |
| `description` | Subject stripped of boilerplate (e.g. remove "Your Amazon.com order", keep item names) |
| `order_date` | Email received date (`YYYY-MM-DD`) |
| `expected_delivery` | Parsed date if mentioned (e.g. "arrives by Dec 5"); `null` otherwise. Emit a canonical `YYYY-MM-DD` date or `null`. `apply-order.py` drops any off-contract value to `null` at write time — see its `_normalize_expected_delivery` (`jbaruch/nanoclaw-orders#55`). |
| `merchant` | The store/brand name, from the sender's display name or domain (e.g. "Pacagen", "Amazon"). `null` if none is discernible. |
| `order_number` | The order/confirmation number from the subject (e.g. `W1584689498`, `#170910`). `null` if none. |
| `email_message_id` | Gmail message ID |
| `to_address` | The `To:` header (used by Step 6 exclusions) |

For `amount`, run the extractor per email against the fields Step 2 already handed you (all sanitized — the `body` never re-enters from raw Gmail):

```bash
echo '{"subject": "...", "snippet": "...", "body": "..."}' | python3 scripts/extract-amount.py
```

Stdout: `{"amount": <float>, "currency": "USD", "matched": "labeled_total" | "bare_total" | "subject_snippet_largest" | "none"}`. Use the returned `amount`. Real order confirmations put the total only in the body below the snippet fold, so most orders resolve via a labeled total line — see the precedence in `scripts/extract-amount.py` (module docstring).

## Step 5 — Upsert each parsed email into the orders table

Compute the `id`:

```bash
python3 scripts/compute-order-id.py <source> <order_date> <description>
```

Produces `{source}-{order_date}-{hash}` where `hash` is the first 8 hex chars of SHA-1 over UTF-8-encoded `description` bytes verbatim (no trimming, case-folding, or normalisation).

Pipe a single-line JSON object with the parsed fields plus the computed `id`:

```bash
echo '{"id": "...", "source": "...", "status": "...", "amount": 19.99, "currency": "USD", "description": "...", "order_date": "2026-04-29", "expected_delivery": null, "email_message_id": "...", "to_address": "...", "merchant": "...", "order_number": "..."}' \
  | python3 scripts/apply-order.py
```

Parameter-bound `INSERT ... ON CONFLICT(email_message_id) DO UPDATE SET status = excluded.status, last_updated = excluded.last_updated WHERE orders.status != excluded.status`. Stdout: `{"action": "inserted" | "status_updated" | "noop", "id": "..."}`. New rows: `flagged = 0`, `flag_reason = NULL`.

## Step 6 — Apply user-preference exclusions

```bash
python3 scripts/apply-exclusions.py
```

The exclusion rule table and all matching logic are owned by the script — see `scripts/apply-exclusions.py`, `EXCLUSIONS` constant and module docstring. Side effect: every matched row is reset to `flagged = 0`, `flag_reason = NULL` in one transaction, parameter-bound.

**Enforcement:** the script's `EXCLUSIONS` table is the runtime-authoritative mirror of the "Do NOT flag these" list in `/workspace/trusted/user_preferences.md`. When that list changes, update `EXCLUSIONS` in the same change.

Stdout: `{"excluded_ids": [...], "excluded_ids_csv": "...", "matched": <int>, "unflagged": <int>}` (ids in ascending `id` order). Pass `excluded_ids_csv` verbatim as Step 9's `EXCLUDED_IDS` — do not recompute or edit the list. (`scripts/unflag-orders.py` remains available for ad-hoc unflagging outside this flow, e.g. user-acknowledged alerts.)

## Step 7 — Auto-promote stale shipped/ordered orders

Some senders (e.g. Chewy Autoship) never send a delivered email; status stays `shipped` and Step 9's "Overdue delivery" rule keeps firing. Promote stale rows to synthetic terminal `assumed_delivered`:

```bash
python3 scripts/promote-stale-shipped.py
```

Eligibility (all three must hold):
- `status IN ('shipped', 'ordered')`
- `expected_delivery` non-null AND (ISO date ≥10 days before today, OR malformed/free-text)
- `last_updated` ≥10 days ago

Stdout: `{"promoted": <int>, "ids": [...]}`. Idempotent. `assumed_delivered` is synthetic terminal — Step 9 never flags it. Future emails still update via Step 5's merge rule.

## Step 8 — Compute stuck orders

Get the ids of orders stuck in `ordered` with no shipment:

```bash
python3 scripts/compute-stuck-orders.py
```

Stdout: `{"stuck_ids": ["<id>", ...]}` — the ids of orders stuck in `ordered` with no shipment. The age window, shipment statuses, and pairing rule are owned by `scripts/compute-stuck-orders.py` (module docstring + top-of-file constants). Pass the `stuck_ids` verbatim to Step 9 as `STUCK_IDS`. Proceed immediately to Step 9.

## Step 9 — Apply anomaly flagging

Flag every non-excluded row. Pass the Step 6 id list via `EXCLUDED_IDS` and the Step 8 stuck ids via `STUCK_IDS`:

```bash
EXCLUDED_IDS="<id1>,<id2>,..." STUCK_IDS="<id3>,<id4>,..." \
  python3 scripts/flag-anomalies.py
```

Empty `EXCLUDED_IDS` and empty `STUCK_IDS` are both fine. Stdout: `{"flagged": <int>, "unflagged": <int>, "ids_flagged": [...], "ids_unflagged": [...]}`.

Which statuses flag and the per-status age cutoffs are owned by `scripts/flag-anomalies.py` — its module-docstring rule table and `_classify()` are the single source of truth. The stuck-order signal is applied from `STUCK_IDS` verbatim; the script never re-derives it.

Flow effects: each matching row gets `flagged=1` plus a `flag_reason`; rows past their cutoff (or that no longer match) are unflagged in the same pass; rows that never matched stay unflagged. The `ids_flagged` list drives the Step 11 report.

## Step 10 — Re-stamp orders_metadata (success-path refresh)

```bash
python3 scripts/write-orders-metadata.py
```

Same script as Step 3, re-run on the happy path. Idempotent. Stdout: `{"last_checked": "<iso>", "last_updated": "<iso>"}`.

## Step 11 — Report flagged items

```bash
python3 scripts/get-flagged-orders.py | python3 scripts/render-order-alerts.py
```

`get-flagged-orders.py` emits the flagged rows as a JSON array ordered by `order_date` descending, collapsing rows that share a `(source, order_number)` logical order to one; `render-order-alerts.py` HTML-escapes every field (`description` derives from sender-controlled email text) and emits `{"message": <str|null>, "count": <int>}`. `message` is the complete Telegram HTML text — one bullet per order, shaped:

```
<b>📦 Order alerts:</b>

• <b>[description]</b> — [flag_reason] (<i>[merchant or source], [order_date]</i>)
```

`message: null` → no flagged orders → stay silent. Otherwise send the `message` value verbatim via `mcp__nanoclaw__send_message` — never rebuild or reformat it by hand; the escaping is what keeps a hostile subject line from breaking the Telegram HTML parse or injecting tags. Finish here.
