# Changelog

## 0.1.42 — 2026-08-18

### Chore — migrate from `tile.json` to `.tessl-plugin/plugin.json` (`jbaruch/nanoclaw-core#97`)

Ran `Skill(skill: "tessl__migrate-to-plugin")`: `tessl plugin migrate` wrote `.tessl-plugin/plugin.json` at the current version (0.1.41), `.tileignore` became `.tesslignore`, and `tile.json` is gone. The exclusion set is unchanged — `/tessl.json` was already anchored there — so nothing new enters or leaves the package. `tessl plugin pack` still synthesizes a legacy `tile.json` into the published archive for older consumers; that one is generated, not committed. Verified against the packed archive (29 entries): all 17 `check-orders` scripts, both `nightly-order-sync` scripts, every `state-schema.md`, and both `references/` files still ship. The only path present on disk and absent from the archive is `__pycache__`, which the exclusion set has always dropped.

Terminology reconciliation (skill Step 2): package-sense "tile" → "plugin" across the README, `state-schema.md`, three `check-orders` scripts, the test suite and its fakes, `pyproject.toml`, and the plugin description itself. Two live contracts keep the old spelling: `containerConfig.additionalTiles` is the orchestrator's config key, and the `hostile` fixtures in `test_render_order_alerts.py` merely contain the substring. The `publish.yml` display name goes from "Review & Publish Tile" to "Review & Publish Plugin" — the only CI edit here, matching the fleet-wide rename tracked in `jbaruch/nanoclaw-host#50`. Historical `tile.json` references in this CHANGELOG stay as written; they name a file that existed at the time.

## 0.1.41 — 2026-08-18

### Chore — commit `tessl.json` as the dependency manifest it is

`.gitignore` excluded `tessl.json`, so the repo carried no committed declaration of what it depends on, and `hooks/check-tessl-latest.sh` in `jbaruch/coding-policy` — the deterministic enforcement for the Runtime-Managed Manifest Carve-Out — took its "no manifest, not a consumer" silent no-op path every session. With nothing watching, the untracked local manifest drifted to `"mode": "vendored"` with literal version pins.

The manifest is now committed and `"mode": "managed"`. Every `jbaruch/*` dependency floats at `latest` under the carve-out; `finsi/codex-review` is third-party and stays pinned, with its renewal cadence recorded in `README.md`. The ignore file keeps the manifest out of the published package.

All notable changes to this tile are documented here.

## 0.1.39 — 2026-08-13

### Fixed — `check-orders` stops flagging never-ship merchants and honours owner snoozes

The two refinements left over from `jbaruch/nanoclaw-orders#61`, closing `#63`.

- **Never-ship merchants.** Kickstarter and Indiegogo pledges, Patreon and Substack subscriptions never emit a shipment email, so "ordered with no shipment" is their steady state rather than an anomaly — yet they entered the stuck pool and flagged for the whole `[7, ceiling]` window until auto-resolve drained them. `compute-stuck-orders.py` now suppresses them from a curated `NEVER_SHIP_MERCHANTS` tuple, fully enumerable per `script-delegation`'s "Regex Trap" with no pattern-inference path. Matching mirrors `apply-exclusions.py`'s precedence: a populated `merchant` (from `state-017`) is authoritative and the description is not consulted, so an Amazon order whose description mentions a Kickstarter refund stays flaggable. A NULL/blank merchant — a legacy row — falls through to a description substring match, but only on `source = 'other'`, where every never-ship merchant classifies (`classify-order.py` maps only amazon/shopify/shop domains to their own source). Without that gate a NULL-merchant Amazon row could be suppressed by description text alone.
- **Owner snoozes.** `ack-orders.py` answers "these arrived" by transitioning rows to `assumed_delivered`. It cannot answer the other half of the same owner reply — "this one truly not shipped, all the rest shipped and delivered" — because `assumed_delivered` would record a delivery that never happened, while leaving it `ordered` re-flags it nightly. New `snooze-orders.py` (ids on stdin, `SNOOZE_UNTIL` env) writes the `snooze_until` column from `jbaruch/nanoclaw#917` and touches nothing else, so the row keeps its honest `ordered` status while the nightly run stops reporting it. Suppression is `today < snooze_until`, so the boundary day itself re-flags. The writer rejects a missing, non-canonical, or non-future date with exit 2 rather than writing a no-op snooze the owner would believe had taken effect — the ISO basic (`20260601`) and week (`2026-W40-1`) forms included, since `date.fromisoformat` accepts them but the readers honour the canonical `YYYY-MM-DD` alone.

Both readers honour the marker, which matters more than it first looked: Step 8 reaches only `ordered` rows via the stuck rule, so a snooze enforced there alone would still let an `ordered` row with an overdue `expected_delivery` flag on the higher-priority "Overdue delivery" rule, and would do nothing at all for the `shipped` rows the writer accepts — reporting `snoozed: 1` while the order kept alerting. `flag-anomalies.py` (Step 9) now suppresses every rule for a snoozed row and unflags it if already flagged, so "stop asking" means the order goes quiet rather than going quiet only when one particular rule would have fired.

Step 8's reader tolerates the column being absent per `stateful-artifacts` cross-pipeline reader discipline — on a database that has not applied `state-018` yet it reads "nothing is snoozed" and runs normally, so the tile need not ship in lock-step with the orchestrator. That path is covered against a real un-migrated table rather than a mock.

## 0.1.38 — 2026-08-13

### Fixed — `check-orders` drains the stuck-`ordered` "roach motel" and persists owner acks

The nightly stuck-order alert re-dumped the whole `ordered` backlog every run, including orders the owner had already acknowledged as delivered (`jbaruch/nanoclaw-orders#61`). Two root causes, both fixed here without a schema change:

- **No auto-resolve.** An order whose shipment/delivery email never matched (merchant mismatch, no tracking email, digital/subscription/Kickstarter/autoship) kept a NULL `expected_delivery`, so `promote-stale-shipped.py` (Step 7) never touched it and it sat in `ordered` forever. Step 7 now has a second promotion path: an `ordered` row past an age ceiling (aligned with `compute-stuck-orders.py`'s stuck-window upper bound) auto-resolves to `assumed_delivered` on `order_date` alone — leaving the stuck pool instead of re-flagging every run. Recent `ordered` rows stay put as the genuine "placed weeks ago, never shipped" signal.
- **No ack memory.** `unflag-orders.py` only clears the flag for one run; the row stays `ordered` and resurfaces next night. New `ack-orders.py` (ids on stdin) persists an owner acknowledgement by transitioning `ordered`/`shipped` rows to the terminal `assumed_delivered`, so an acked order never re-flags. Terminal rows (cancelled/refunded/already-delivered) are left untouched. The SKILL's Step 6 note now points owner acks at this tool instead of `unflag-orders.py`.

The remaining refinement — acknowledging a *genuinely*-stuck order without mislabeling it delivered (a snooze marker the detector reads) — needs a new `orders` column and is tracked upstream at `jbaruch/nanoclaw#917`.

## 0.1.37 — 2026-08-09

### Changed — `check-orders` dedups by the persisted `order_number`

With `order_number` now stored on every row (previous release), stuck-order pairing is a deterministic join on `(source, order_number)` and moves back into a script: the new `compute-stuck-orders.py` (Step 8) computes the stuck ids directly, replacing the interim agent-pairing step, and `flag-anomalies.py` flags them (Step 9). Reasoning at extraction (the agent reads the order number off the subject), determinism at use. The report step (`get-flagged-orders.py`) now collapses flagged rows that share a `(source, order_number)` logical order to one alert line, so an order never surfaces once per email. Removes SKILL Step 9's agent pairing and renumbers the tail (12 steps → 11). This is the dedup half of `jbaruch/nanoclaw-orders#55`.

Also folded a review advisory: trimmed rationale clauses from the Step 4 `merchant`/`order_number` extraction rules and the Step 11 render note, per `context-writing-style`.

## 0.1.36 — 2026-08-09

### Added — `check-orders` captures merchant + order_number, alerts identify the merchant

Now that the shared `orders` table carries `merchant` and `order_number` columns (nanoclaw `state-017`), Step 4 extracts both and `apply-order.py` stores them (normalized to a stripped non-empty string or NULL). The flagged-order alert now leads its meta with the captured merchant and falls back to `source`, so a flagged item whose `source` is `other` is identifiable instead of an anonymous subject fragment (`jbaruch/nanoclaw-orders#55`). `order_number` is captured here as the structured key that a follow-up uses to pair and dedup an order's confirmation and shipment emails.

Also folded in a review advisory: trimmed the Step 4 `expected_delivery` note to a compact directive plus a pointer to `apply-order.py`'s `_normalize_expected_delivery`, instead of restating the script's internal validation predicate.

## 0.1.35 — 2026-08-09

### Fixed — `apply-order.py` drops non-date `expected_delivery` at write time

The agent parses `expected_delivery` from free-text subjects, which let scraped non-dates ("today", "March", "overnight") land in the column (`jbaruch/nanoclaw-orders#55`, follow-up). `apply-order.py` now stores only a value that parses as an ISO date; anything else is dropped to `NULL` with a stderr note, so the overdue-delivery check never keys off garbage. A `null` or absent value passes through unchanged.

## 0.1.34 — 2026-08-08

### Fixed — `check-orders` flags stuck orders and drops large-purchase noise

`check-orders` gained the primary signal the owner actually wants surfaced (`jbaruch/nanoclaw-orders#55`): an order stuck in status `ordered` for 7–90 days with no matching shipment is now flagged "Ordered, not yet shipped". Detection is split to keep the deterministic and reasoning halves in their right places — a new `list-stuck-candidates.py` selects the aged `ordered` rows and shipment rows (SKILL Step 8), the agent pairs them by the order number written in each subject and returns the unpaired ids (Step 9), and `flag-anomalies.py` flags exactly those `STUCK_IDS` (Step 10). Pairing lives in the agent because matching sender-controlled subject text is reasoning, not scripting. So the confirmation and "on its way" emails of one order collapse — an `ordered` row whose order shipped is not counted as stuck.

Removed the "Large purchase" rule. It flagged purchases the owner made himself and already knew about (concert tickets, a laptop) with no action attached, and it was why one logical order surfaced twice in a brief — both the confirmation and the shipment row cleared the old $200 threshold. Rows carrying a legacy `Large purchase: $...` reason unflag on the next pass.

Deeper follow-ups from the same issue are tracked separately: true row-level dedup by order number (persisted, replacing the per-run agent pairing), merchant capture, and rejecting non-date `expected_delivery` values at ingestion.

## 0.1.33 — 2026-08-05

### Removed — `tests/test_changelog_sync.py`, a guard with no consumer

The test asserted that `CHANGELOG.md`'s first `## X.Y.Z` heading equals `tile.json`'s version. That invariant cannot hold in this fleet: Dependabot cannot author CHANGELOG entries, fleet convention exempts pure plumbing bumps from carrying one, and the shared stamp step is a documented no-op when there is nothing un-headed to stamp. So every bot merge advances the manifest past the CHANGELOG by construction.

What it actually cost, all on 2026-08-05: main red for 15 days from the 0.1.29 publish, three Dependabot PRs blocked behind it, and two backfill PRs (#51, #52) to clear the same defect twice in one day. It would have fired again on the next dependency merge.

What it bought was heading completeness for releases that contain nothing to describe — this repo's plugin CHANGELOG is read for its substantive entries, not as a version ledger, and a bodyless `## 0.1.31` heading carries no information for anyone. The alternative fix considered and rejected was teaching the shared stamp step to emit those bodyless headings fleet-wide: 40 such releases exist across the six sibling repos, and stamping them would add noise to six CHANGELOGs to satisfy a check only this repo ran.

Substantive changes still get CHANGELOG entries under the un-headed `### ` convention, unchanged. Only the completeness assertion is gone.

## 0.1.32 — 2026-08-05

### CI — refresh review-trigger.yml from the canonical template

The consumer copy predated the template's `github.actor != 'dependabot[bot]'` guard, so the workflow fired on Dependabot PRs, read an empty `FLEET_DISPATCH_TOKEN` (their runs use the Dependabot secret store, not Actions), and its own guard exited 1 — a permanent red check on every dependency PR. The coding-policy cron poll reviews those PRs regardless, so nothing went unreviewed. Refreshed to the template verbatim so this repo stops drifting from canonical.

### Fixed — backfill the 0.1.31 heading its publish never stamped

Same failure as 0.1.30's entry, one release later: #48 was a Dependabot CI bump carrying no un-headed `### ` block, the stamp step had nothing to stamp, and `tile.json` advanced to 0.1.31 alone while the newest heading stayed 0.1.30.

Backfilling is not a fix, it is the third instance of one defect (0.1.7–0.1.9 as issue #17, then 0.1.29, now 0.1.31). Dependabot cannot author CHANGELOG entries, and fleet convention exempts pure plumbing bumps from needing one, so every future dependency merge into this repo reproduces this exactly. The durable fix belongs in the shared stamp step — emit a bodyless `## <version> — <date>` heading when a release has no un-headed entries — so the manifest can never advance past the CHANGELOG regardless of what a PR carries. Tracked separately against `jbaruch/coding-policy`.

## 0.1.31 — 2026-08-05

### CI — bump actions/setup-python from 6 to 7

Dependabot bump of `actions/setup-python` from v6 to v7 in the test workflow. (#48)

## 0.1.30 — 2026-08-05

### CI — backfill the 0.1.29 heading its publish never stamped

`test_first_changelog_heading_matches_tile_version` has failed on every PR and every push to main since 0.1.29 published on 2026-07-21: `tile.json` declared 0.1.29 while the newest CHANGELOG heading was still 0.1.28. That red main blocked all three of this repo's open Dependabot PRs.

The mechanism is the one the test was written for (issue #17, versions 0.1.7–0.1.9). #47 was pure CI plumbing, so it carried no un-headed `### ` entry block; the publish workflow's stamp step is a documented no-op when the top section already has a `## ` heading, so it wrote nothing while `tile.json` advanced alone. The guard caught it one version deep, as designed.

This backfills 0.1.29's section below. That entry is itself the un-headed block for the release publishing it — a CHANGELOG-only PR shipped without one would reproduce the exact gap it is fixing.

## 0.1.29 — 2026-07-21

### CI — adopt the canonical reusable publish workflow

Replaced the per-repo publish workflow with a thin `publish.yml` delegating to the fleet's canonical reusable workflow `jbaruch/coding-policy/.github/workflows/publish-plugin.yml` (`jbaruch/coding-policy#206`, Phase 2). No behavior change: the same `tessl plugin lint` + changed-only skill review (`credit-outage: skip`) + publish, defined once instead of per-repo. The workflow display name is unchanged so run-name watchers keep working, and the secret is scoped to `TESSL_TOKEN`. (#47)

## 0.1.28 — 2026-07-21

### CI — PR-time fleet-review trigger

Added a thin `review-trigger.yml` that dispatches an immediate single-PR review in `jbaruch/coding-policy` so the policy verdict lands before merge; the coding-policy cron poll stays the backstop. No tile-content change.

## 0.1.27 — 2026-07-21

### CI — Move to the central fleet policy reviewer

Migrated from the per-repo review workflow to the central `jbaruch/coding-policy` fleet reviewer (one ChatGPT-subscription credential held only in coding-policy). No tile-content change.

## 0.1.26 — 2026-07-20

### CI — Repoint copilot-instructions at the review workflow

Docs-only pointer update accompanying the reviewer migration. No tile-content change.

## 0.1.25 — 2026-07-20

### CI — Migrate to the Codex CLI subscription reviewer

Switched the policy reviewer to the Codex CLI authenticated by a ChatGPT subscription (no API key). No tile-content change.

## 0.1.24 — 2026-07-19

### Fix — check-orders catches Shopify orders via the transactional sending subdomain (`jbaruch/nanoclaw-orders#44`)

The Shopify query `from:noreply@shopify.com OR from:no-reply@shopify.com` matched **nothing** — Shopify never sends order mail from `shopify.com`. A live sample found Shopify order mail splits two ways: platform-hosted senders on `store+NNN@t.shopifyemail.com` (55 threads, all transactional) and merchant-custom-domain senders (pacagen.com, knifeaid.com, talesofvalhalla.com) with no queryable Shopify signal. The existing subject-keyword queries recalled only 28 of the 55 platform-hosted orders (query 5 caught **zero** — Shopify subjects read `Order #NNN confirmed`, never the `order confirmation` bigram), so 27 Shopify orders were missed entirely.

Replace the dead query with `from:t.shopifyemail.com` — Shopify's **transactional** subdomain, ~100% order precision. Marketing rides the `m.`/`g.` subdomains, so the swap brings in no promo bleed. Store-hosted senders on their own domain still depend on the subject/`"Your order"` queries — this recovers the platform-hosted stream, not every Shopify order.

Fetching the emails is not enough on its own: the recovered messages come from `store+NNN@t.shopifyemail.com` with subjects like `Order #NNN confirmed`, which the old prose maps would have classified as `source="other"` / `status="unknown"`. The `source`/`status` classification moves out of the Step 4 prose table into a new tested `scripts/classify-order.py`: `shopifyemail.com` (any subdomain) normalizes to `shopify`, and `confirmed` joins the `ordered` keywords, with status priority ordered so a shipment/cancellation/refund signal still outranks a bare `confirmed`. Merchant-custom-domain senders remain `source="other"` (no queryable Shopify signal) — the documented limitation.

**Surface sync:** `skills/check-orders/scripts/fetch-order-emails.py` (the `QUERIES` constant + rationale comment), `skills/check-orders/scripts/classify-order.py` (new), `skills/check-orders/SKILL.md` (Step 4 source/status now call the classifier), `tests/test_classify_order.py` (new), `README.md` (script list). The query and map logic live in the scripts per `coding-policy: script-as-black-box`.

## 0.1.23 — 2026-07-18

### Fix — check-orders reads the order total from the email body (`jbaruch/nanoclaw-orders#38`)

Step 4's `amount` rule read `$XX.XX` from subject+snippet only and took the largest match. A live sample of real order emails (the 5 production queries) found the total sits ONLY in the body for 8 of 9 order confirmations — Amazon `Ordered:`/`Shipped:` and store-hosted Shopify confirmations print nothing but `Ordered: <item>` / `Order #X confirmed` above the ~200-char snippet fold — so the old rule defaulted `amount` to `0` for essentially every order. The fetched-but-unread `body` field (`#37` removed a contract sentence that claimed Step 4 read it) is now read by a named rule, resolving the "read or don't project" question in favour of read.

New `scripts/extract-amount.py` owns the extraction: prefer a labeled `Order Total` / `Grand Total` / `Total` line (searched subject→snippet→body, last match wins) over any largest-amount pick, and restrict the unlabeled largest-amount fallback to subject+snippet. The sample showed why "largest in body" is wrong — two of three Shopify bodies carried a struck-through list price ($98.99, $69.00) larger than the true total ($94.14, $62.10); a labeled total is never a struck price. The `subtotal`, `sub-total`, and `sub total` spellings are excluded from the bare-`Total` match. `tests/test_extract_amount.py` covers the body-only total, the struck-price trap, refund-in-snippet, and subtotal disambiguation.

Not addressed here (separate follow-up): the `from:*@shopify.com` production query matches nothing, because Shopify sends order mail from each merchant's own domain — real Shopify confirmations are caught only by the subject/`"Your order"` queries.

**Surface sync:** `skills/check-orders/scripts/extract-amount.py` (new), `tests/test_extract_amount.py` (new), `skills/check-orders/SKILL.md` (Step 2 body note, Step 4 amount rule + invocation), `skills/check-orders/scripts/fetch-order-emails.py` (docstring + projection comment), `README.md` (script list).

## 0.1.21 — 2026-07-18

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
