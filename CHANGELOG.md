# Changelog

All notable changes to this tile are documented here.

### Changed — pin `nightly-order-sync` to Haiku via `agentModel:` (`jbaruch/nanoclaw#613`)

Order-email data sync is triage, not synthesis. Pin `agentModel: "claude-haiku-4-5-20251001"` in the skill's frontmatter so it stops defaulting to Opus (cadence-registry plumbs it to `scheduled_tasks.agent_model`). Full model ID matches the gateway `model_list` row. Part of the #613 Claude tier-down.

## 0.1.0

### Added

- Initial tile: `check-orders` + its `nightly-order-sync` cadence wrapper migrated from `nanoclaw-admin` into a standalone public per-chat overlay tile (`jbaruch/nanoclaw-admin#319`). `check-orders` fetches order-related Gmail over the Composio v3 REST endpoint, upserts the `orders` table in the orchestrator's `messages.db`, and flags cancellations/refunds, large purchases until delivered, and overdue deliveries — staying silent on normal order flow. `nightly-order-sync` runs it on a 3-day-capped `15 6` cadence and emits the observable-silence cursor marker the silent-success watchdog reads. Co-locating the cadence driver with the skill it drives keeps the orders domain self-contained in one tile (same pattern as `nanoclaw-conferences`'s `check-cfps` + `nightly-cfp-sync`) and removes the cross-tile `Skill()` call that would otherwise span admin → orders. Carries the ten `check-orders` helper scripts, the two `nightly-order-sync` scripts, and both clusters' tests unchanged from the admin originals.
  - Origin: `nightly-order-sync` was peeled off the `nightly-external-sync` bundle in `jbaruch/nanoclaw#581` so order fetching gets its own bounded container instead of being cut off in the bundle's long tail.

### Cross-tile dependencies

- `skills/check-orders/scripts/fetch-order-emails.py` loads `sanitize-email-body.py` and `composio-rest.py` at runtime from `nanoclaw-admin`'s `heartbeat` skill via the co-loaded `tessl__heartbeat` tile mount; the orders state (`orders` + `orders_metadata` tables) lives in the orchestrator's shared `messages.db` (read-write on main/trusted), where admin's `morning-brief` and `check-email` read flagged orders. These resolve because admin co-loads with this overlay in the owner's main/trusted chat. The Gmail fetch path still rides on Composio, which `jbaruch/nanoclaw#637`–`#640`/`#564` are retiring in favor of OneCLI — the moved fetch script will be reworked when that lands. Tests that assert the heartbeat sanitizer's own behavior (body cap, invisible-unicode collapse) or load the real heartbeat files are skipped here with a `tessl__heartbeat`-dependency reason; that coverage lives in admin's heartbeat suite. The orders-owned projection, dedup, field-fallback, cursor, and fail-closed-on-missing-sanitizer logic stays fully tested with an identity sanitizer double.

### Rules

- **Closed-loop carve-out claimed for `jbaruch/coding-policy: plugin-evals`** (2026-06-07). This tile is part of the `jbaruch/nanoclaw-*` plugin fleet — a fully-automated agent loop satisfying all three preconditions of the rule's "Narrow exception for closed-loop automated systems with no human eval-result consumption" clause: (1) no human reviews eval output for this tile in any form (no eval scores, no lift deltas, no scenario-by-scenario diffs, no regression alerts); (2) no automated gate consumes eval results (no `evals.yml` workflow, no publish-tile eval step, no downstream dashboard or paging route); (3) the owner accepts that re-introducing any consumption of eval results later — whether human review OR automated gating — requires re-introducing evals first under the standard requirement. Matches the carve-out claimed by `jbaruch/nanoclaw-admin` on 2026-05-09 and inherited by every `jbaruch/nanoclaw-*` tile thereafter. Covers both decisional skills in this tile (`check-orders`, `nightly-order-sync`). No `evals/` directory ships in this tile.
