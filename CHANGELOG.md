# Changelog

All notable changes to this tile are documented here.

### Fix — nightly-order-sync cadence cap drops below the cron-interval multiple (`jbaruch/nanoclaw#803`)

`precheck-nightly-order-sync.py` set `CADENCE = timedelta(days=3)` — an exact multiple of the 24h daily cron interval. The cursor stamps at run *completion*, so the intended every-third-day fire lands ~71.8h old, `age >= CADENCE` fails, and the run slips to every fourth day (live `task_run_logs`: ran 07-11, then 07-14). This is the fleet-wide near-miss `jbaruch/nanoclaw-admin#353` / `jbaruch/nanoclaw-admin#354` first fixed elsewhere. The cap drops to `60h`: above the 48h two-day fire so the run isn't every other day, below the 72h near-miss with a half-period of slack that also absorbs the local-TZ cron's DST drift. The `three_day_boundary` test becomes a `three_day_near_miss` regression guard; the cap value is de-hardcoded from `SKILL.md`, `state-schema.md`, and `references/cadence-rationale.md` per `coding-policy: script-as-black-box`.

## 0.1.20 — 2026-07-16

### Changed — migrate the order-email fetch from Composio to native Gmail REST (`jbaruch/nanoclaw#638`)

`fetch-order-emails.py` loaded `composio-rest.py` from `nanoclaw-admin`'s heartbeat skill over the co-loaded `tessl__heartbeat` mount. `#638` deletes that file: Google access now goes to the native Gmail REST API through the OneCLI TLS-MITM gateway, which owns the OAuth connection and injects `Authorization: Bearer` on the wire. Separate registries mean admin's publish breaks this tile's 06:15 nightly fetch until this one ships, so this lands immediately after admin.

The fetch is rewritten onto heartbeat's new foundation — `google-rest.py` (transport), `gmail-ops.py` (list/get), `gmail-message.py` (native message parsing) — and `sanitize-email-body.py` now exports only `sanitize()`, its Composio-shaped `sanitize_message()` / `DEFAULT_FIELDS` having died with the invented field names they mapped. The stdout contract is unchanged, so SKILL.md Steps 3-10 are untouched.

Two shape facts drove the rewrite. Native `users.messages.list` answers a query with `{id, threadId}` stubs only, where Composio's `GMAIL_FETCH_EMAILS` returned full messages — so every message costs a second `get`, and dedup now runs against the stubs before any body is paid for (an id surfaced by three of the five queries costs one `get`, not three). And native `snippet` is a real ~200-char preview rather than Composio's `{body, subject}` object, so `snippet` and `body` map to genuinely distinct fields instead of needing the `_as_text()` coercion — that helper, the envelope-shape sniffing, the `successful: false` branch, and the `messageId or id` / `from or sender` dual reads were all Composio artifacts and are deleted rather than ported. `internalDate` (epoch milliseconds) replaces Composio's date field.

Credential handling is gone, not relocated: no `COMPOSIO_API_KEY` / `COMPOSIO_USER_ID`, no preflight, nothing in the container's environment to miss. The two failure modes it hid are actionable instead — a 401 (`GatewayNotInjecting`: the gateway is off the request path or the app is disconnected) and a 403 `access_restricted` (`TierAccessRestricted`: the untrusted tier is gated from Google by design) both exit 2 with a remediation rather than being retried as if transient. Per-query error isolation is preserved and extended: an unreadable individual message is an order alert that will not fire, so it becomes an `errors[]` entry against the query that surfaced it rather than being dropped silently. Step 2's error table reads on "all 5 queries errored **and** nothing was fetched", so a partially-failing run still advances the cursor — the forward-progress property `references/write-ahead-rationale.md` exists to protect.

**Surface sync:** `skills/check-orders/scripts/fetch-order-emails.py`, `skills/check-orders/SKILL.md` (Core Rule, Step 2, error table), `skills/check-orders/references/write-ahead-rationale.md`, `README.md` (capabilities, tier rationale, required environment, cross-tile dependency, script list), `tests/test_fetch_order_emails.py`, `tests/conftest.py`, and `tests/fakes/` — `composio-rest.py` deleted, `sanitize-email-body.py` trimmed to `sanitize()`, plus new native-shaped doubles for `google-rest.py`, `gmail-ops.py`, and `gmail-message.py`.

## 0.1.19 — 2026-07-13

### Changed — align `nightly-order-sync` wording with the sqlite order store (`jbaruch/nanoclaw-orders#32`)

The `nightly-order-sync` skill still called the order store `orders-db` in its frontmatter `description` and Step 1. That literal `orders-db.json` file was migrated into the sqlite `orders` table in `#294` (2026-04-30); the canonical name is now the `orders` table in `/workspace/store/messages.db`, per `check-orders/SKILL.md`. Replace the stale shorthand in both spots. Documentation wording only — no behavior change.

## 0.1.18 — 2026-07-08

### Changed — bump github/gh-aw-actions/setup to v0.82.5 (`jbaruch/nanoclaw-orders#30`)

Dependabot retargeted its `jbaruch/nanoclaw-orders#9` from 0.82.2 to 0.82.5 minutes after `jbaruch/nanoclaw-orders#29` landed 0.82.2 — the registry moved mid-campaign. Bump the pin in both compiled gh-aw review locks (uses lines, comment inventory, and embedded `gh-aw-manifest` entries) to v0.82.5. No workflow logic change.

## 0.1.17 — 2026-07-08

### Changed — refresh pinned GitHub Actions (`jbaruch/nanoclaw-orders#29`)

Adopt the four open Dependabot GitHub Actions bumps as one CI-scoped change, with each bump kept as its own Dependabot-authored commit: `actions/checkout` v4 → v7 (`test.yml`, `publish-tile.yml` — also silences the runners' Node 20 deprecation warning), `actions/setup-python` v5 → v6 (`test.yml`), plus `actions/cache/restore` v5.0.5 → v6.1.0 and `github/gh-aw-actions/setup` v0.81.6 → v0.82.2 in the compiled gh-aw review locks (Dependabot maintains those pins between `gh aw compile` runs). Review follow-ups: `actions/cache/save` is bumped to the same v6.1.0 Dependabot left it trailing at v5.0.5, and the locks' embedded `gh-aw-manifest` entries are updated to match the visible pins. Supersedes `jbaruch/nanoclaw-orders#6`–`#9`, which lacked the CHANGELOG blocks the stamp step needs. No workflow logic change.

## 0.1.16 — 2026-07-08

### Changed — bump pytest to 9.1.1 (`jbaruch/nanoclaw-orders#28`)

Dev-toolchain pin refresh across a pytest major version. The suite runs clean at 9.1.1 — 130 passed, zero skips, no deprecation warnings — no code change. Supersedes Dependabot's `jbaruch/nanoclaw-orders#11`, which lacked the CHANGELOG block the stamp step needs.

## 0.1.15 — 2026-07-08

### Changed — bump pyright to 1.1.411 (`jbaruch/nanoclaw-orders#27`)

Dev-toolchain pin refresh. Supersedes Dependabot's `jbaruch/nanoclaw-orders#12`, which lacked the CHANGELOG block the stamp step needs. Zero findings at the new version — no code change.

## 0.1.14 — 2026-07-07

### Changed — freeze wall-clock tests and unskip fail-closed coverage (`jbaruch/nanoclaw-orders#20`, `jbaruch/nanoclaw-orders#21`)

Two test-hygiene fixes. First (#21): `test_flag_anomalies.py` and `test_promote_stale_shipped.py` computed fixtures from `date.today()` / `datetime.now()`, so the boundaries under test moved every day; both now freeze `module.date` / `module.datetime` to fixed test doubles (the `test_within_days.py` pattern) with literal fixture dates. Second (#20): the `fetch-order-emails.py` fail-closed main-path tests were skipped as heartbeat-dependent; new local fakes under `tests/fakes/` mirror the heartbeat modules' load surface, unskipping the missing-credentials and missing-REST-helper paths, and the two sanitizer-behavior skips are replaced by a contract test proving the loaded sanitizer runs before projection. The suite now runs with zero skips.

## 0.1.13 — 2026-07-07

### Changed — move Step 6 exclusion matching into deterministic code (`jbaruch/nanoclaw-orders#18`)

Step 6 specified address parsing, multi-recipient handling, case-insensitive comparison, and the description fallback as prose for the agent to re-implement on every run — a mismatch could re-flag orders the user explicitly excluded. New `apply-exclusions.py` owns the `EXCLUSIONS` table and all matching (recipient parsing via `email.utils.getaddresses`), unflags matches in one transaction, and emits the id list Step 8 passes as `EXCLUDED_IDS`. Tests cover display-name wrapping, comma-separated recipients, case-insensitivity, the NULL `to_address` description fallback, and same-domain non-matches. `unflag-orders.py` stays as the ad-hoc unflagging utility.

## 0.1.12 — 2026-07-07

### Fixed — HTML-escape untrusted fields in Telegram order alerts (`jbaruch/nanoclaw-orders#19`)

Step 10 interpolated raw `description` / `flag_reason` / `source` / `order_date` into Telegram HTML. `description` derives from email subject text — untrusted, sender-controlled — so a subject carrying `<`, `>`, `&`, or tags could break the message parse (suppressing the alert entirely) or inject links/formatting into the notification. New `render-order-alerts.py` owns the rendering: it HTML-escapes every field and emits `{"message": <str|null>, "count": <int>}` with the ready-to-send text; the SKILL pipes `get-flagged-orders.py` into it and sends `message` verbatim (null → stay silent). Hostile-input tests cover `A&B <tag>` and `</b><a href="...">x</a>` descriptions plus non-object array elements.

## 0.1.11 — 2026-07-07

### Fixed — backfill missing release entries and guard the version/CHANGELOG sync (`jbaruch/nanoclaw-orders#17`)

Versions 0.1.7–0.1.9 published without CHANGELOG entries: their PRs added no un-headed entry blocks, so the stamp step (wired in 0.1.7 itself) had nothing to stamp and `tile.json` advanced while the CHANGELOG stood still. Backfill the three sections from the merge commits that produced each release, and add `tests/test_changelog_sync.py` — a pytest guard asserting the first `## X.Y.Z` heading in `CHANGELOG.md` matches `tile.json`'s version, so the next entry-less release fails the very next PR's CI instead of drifting silently for three versions.

## 0.1.10 — 2026-07-07

### Changed — bump ruff to 0.15.20 and reformat (`jbaruch/nanoclaw-orders#16`)

Land the ruff 0.7.4 → 0.15.20 toolchain bump together with the mechanical reformat it forces on three files (implicit string-concat collapse, assert-message wrapping). Under the previously pinned 0.7.4 the tree was format-clean — the drift issue #16 reported reproduces only under newer ruff, which is exactly what was blocking Dependabot's ruff bump PR #10 on the format gate. Bump and reformat land as one PR; either half alone leaves CI red. Supersedes PR #10.

## 0.1.9 — 2026-07-07

### Changed — ignore tessl-generated `.github/mcp.json` (`jbaruch/nanoclaw-orders#15`)

Current tessl CLI init emits `.github/mcp.json` (GitHub Copilot CLI MCP config) alongside the scaffolding the existing ignore block already covers; the block predated it and let it leak through as untracked noise. Entry backfilled by #17 — the release shipped without one.

## 0.1.8 — 2026-07-03

### Changed — refresh coding-policy PR review workflows

Upgrade the gh-aw `jbaruch/coding-policy` PR review workflow templates (OpenAI and Anthropic reviewers, sources and compiled `.lock.yml` forms) to the latest published version. Entry backfilled by #17 — the release shipped without one.

## 0.1.7 — 2026-07-02

### Changed — wire coding-policy stamp-changelog step before publish (`jbaruch/nanoclaw-orders#14`)

Add the `jbaruch/coding-policy` stamp-changelog action immediately before `tesslio/patch-version-publish`, matching nanoclaw-travel: authors add un-headed `### ` CHANGELOG blocks and the step writes the `## <version> — <date>` heading at publish time. Entry backfilled by #17 — the release that wired the stamper shipped, fittingly, without one.

## 0.1.6 — 2026-07-02

### Changed — backfill CHANGELOG entries for released versions 0.1.1–0.1.5

Versions 0.1.1, 0.1.3, and 0.1.4 shipped without CHANGELOG entries, and the 0.1.2 agentModel note sat un-versioned at the top of this file. Every released version now has a heading; the entries are reconstructed from the merge commits that produced each release. No code change.

## 0.1.5 — 2026-07-02

### Added — gate language diagnostics in CI with pyright (`jbaruch/nanoclaw-orders#2`)

Adopt a pyright zero-findings gate: `pyrightconfig.json` for the skill-bundle layout and a `python -m pyright --warnings skills/ tests/` CI step after ruff, before pytest (`--warnings` fails on warnings too). The first run surfaced two real bugs — `stamp-cursor.py` built its argparse description from `__doc__.splitlines()[0]`, which crashes at startup under `python -OO` (docstrings stripped to `None`), and the test module loaders subscripted a `ModuleSpec | None`. Fixed with a literal description and explicit `if ...: raise` guards, no suppressions. Adds a weekly Dependabot for the pinned dev toolchain.

## 0.1.4 — 2026-07-02

### Changed — refresh coding-policy PR review workflows (`jbaruch/nanoclaw-orders#4`)

Upgrade the gh-aw `jbaruch/coding-policy` PR review workflow templates to the latest published version.

## 0.1.3 — 2026-07-01

### Changed — refresh coding-policy PR review workflows (`jbaruch/nanoclaw-orders#3`)

Upgrade the gh-aw `jbaruch/coding-policy` PR review workflow templates to the latest published version.

## 0.1.2 — 2026-06-08

### Changed — pin `nightly-order-sync` to Haiku via `agentModel:` (`jbaruch/nanoclaw#613`)

Order-email data sync is triage, not synthesis. Pin `agentModel: "claude-haiku-4-5-20251001"` in the skill's frontmatter so it stops defaulting to Opus (cadence-registry plumbs it to `scheduled_tasks.agent_model`). Full model ID matches the gateway `model_list` row. Part of the #613 Claude tier-down.

## 0.1.1 — 2026-06-07

### Added — script tests omitted from the initial scaffold

Add the script unit tests that were left out of the initial tile scaffold.

## 0.1.0

### Added

- Initial tile: `check-orders` + its `nightly-order-sync` cadence wrapper migrated from `nanoclaw-admin` into a standalone public per-chat overlay tile (`jbaruch/nanoclaw-admin#319`). `check-orders` fetches order-related Gmail over the Composio v3 REST endpoint, upserts the `orders` table in the orchestrator's `messages.db`, and flags cancellations/refunds, large purchases until delivered, and overdue deliveries — staying silent on normal order flow. `nightly-order-sync` runs it on a 3-day-capped `15 6` cadence and emits the observable-silence cursor marker the silent-success watchdog reads. Co-locating the cadence driver with the skill it drives keeps the orders domain self-contained in one tile (same pattern as `nanoclaw-conferences`'s `check-cfps` + `nightly-cfp-sync`) and removes the cross-tile `Skill()` call that would otherwise span admin → orders. Carries the ten `check-orders` helper scripts, the two `nightly-order-sync` scripts, and both clusters' tests unchanged from the admin originals.
  - Origin: `nightly-order-sync` was peeled off the `nightly-external-sync` bundle in `jbaruch/nanoclaw#581` so order fetching gets its own bounded container instead of being cut off in the bundle's long tail.

### Cross-tile dependencies

- `skills/check-orders/scripts/fetch-order-emails.py` loads `sanitize-email-body.py` and `composio-rest.py` at runtime from `nanoclaw-admin`'s `heartbeat` skill via the co-loaded `tessl__heartbeat` tile mount; the orders state (`orders` + `orders_metadata` tables) lives in the orchestrator's shared `messages.db` (read-write on main/trusted), where admin's `morning-brief` and `check-email` read flagged orders. These resolve because admin co-loads with this overlay in the owner's main/trusted chat. The Gmail fetch path still rides on Composio, which `jbaruch/nanoclaw#637`–`#640`/`#564` are retiring in favor of OneCLI — the moved fetch script will be reworked when that lands. Tests that assert the heartbeat sanitizer's own behavior (body cap, invisible-unicode collapse) or load the real heartbeat files are skipped here with a `tessl__heartbeat`-dependency reason; that coverage lives in admin's heartbeat suite. The orders-owned projection, dedup, field-fallback, cursor, and fail-closed-on-missing-sanitizer logic stays fully tested with an identity sanitizer double.

### Rules

- **Closed-loop carve-out claimed for `jbaruch/coding-policy: plugin-evals`** (2026-06-07). This tile is part of the `jbaruch/nanoclaw-*` plugin fleet — a fully-automated agent loop satisfying all three preconditions of the rule's "Narrow exception for closed-loop automated systems with no human eval-result consumption" clause: (1) no human reviews eval output for this tile in any form (no eval scores, no lift deltas, no scenario-by-scenario diffs, no regression alerts); (2) no automated gate consumes eval results (no `evals.yml` workflow, no publish-tile eval step, no downstream dashboard or paging route); (3) the owner accepts that re-introducing any consumption of eval results later — whether human review OR automated gating — requires re-introducing evals first under the standard requirement. Matches the carve-out claimed by `jbaruch/nanoclaw-admin` on 2026-05-09 and inherited by every `jbaruch/nanoclaw-*` tile thereafter. Covers both decisional skills in this tile (`check-orders`, `nightly-order-sync`). No `evals/` directory ships in this tile.
