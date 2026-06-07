# Why the cursor stamps write-ahead at Step 3 (not write-after at Step 9)

Reference for SKILL.md Step 3. Loaded only when an operator is investigating why the same `write-orders-metadata.py` script is invoked twice per run.

## The pre-write-ahead failure mode

Before the write-ahead stamp landed, `orders_metadata.last_checked` was stamped exactly once per run — at Step 9, after Steps 4-8's parse/upsert/exclusion/flag passes had all completed. If the agent died any time between Step 2's fetch return and Step 9's stamp, the cursor stayed at its previous value.

The watchdog death window is real and load-bearing for this skill. Per `container/agent-runner/src/hard-exit-watchdog.ts` (`jbaruch/nanoclaw#545`), the post-close hard-exit watchdog kills the agent after 30 seconds of no SDK events following the `_close` sentinel. Scheduled fires flip `_close` early (no follow-up IPC is expected), so for a 30-min batch like check-orders, the 30s idle budget covers every long thinking turn, every Composio call, every script invocation in Steps 4-8. A single >30s pause anywhere in that window — typical when the agent is planning how to batch a large fetch result — kills the container and the cursor stays frozen.

Verified across 2026-05-10 → 5-12: nightly-external-sync's check-orders sub-skill got killed mid-processing every day for nine consecutive days. `orders_metadata.last_checked` stayed at `2026-05-03T21:23:25.007Z` the entire time. `fetch-order-emails.py`'s `after:YYYY/MM/DD` clause derives from that cursor. With the cursor frozen, every retry re-fetched the same growing backlog (74 emails on 2026-05-12), the agent planned harder against the bigger context each time, the watchdog hit earlier, and durations grew 88s → 146s → 276s in a textbook self-reinforcing loop.

## Why write-ahead breaks the loop

Stamping at Step 3 — immediately after the fetch returns a parseable JSON blob — advances the cursor regardless of whether Steps 4-10 complete. On a kill anywhere in Steps 4-10, the next run's `fetch-order-emails.py` reads the new cursor, applies `after:<new-date>`, and fetches one day's worth of fresh emails. That batch fits comfortably inside the 30s idle budget, the run completes, the cursor advances again. Steady state restored.

## The cost: bounded data loss on a kill

The trade-off is explicit: emails fetched at Step 2 but not processed by Steps 4-10 are silently dropped from the `orders` table when the cursor moves past them. They're not gone — they're still in the user's Gmail inbox; the user can manually invoke check-orders later to re-process them if a specific order alert is missing. The loss is bounded to the kill window (at most one run's worth of fetched-but-unprocessed orders), not unbounded as in the pre-write-ahead behavior.

The user-facing acceptance is the silent-success-shape framing: a forward-progress system that occasionally misses one batch is strictly better than a permanently-frozen system that misses every batch. The cursor-frozen / backlog-growing failure mode is invisible to the user (every run reports `status: success`); the write-ahead failure mode is also invisible BUT it heals on its own the next day. Healing > permanent failure.

## Why Step 9 still calls the same script

Step 9 re-stamps `orders_metadata.last_checked` on the happy path. Step 3 already advanced the cursor to the fetch boundary; Step 9 advances it again to the more accurate "fully processed through" boundary (current time, after Steps 4-8 finished). The gap is at most the duration of Steps 4-8 (typically tens of seconds for a normal-sized batch), so the re-stamp is barely different from Step 3's value, but it carries the more accurate semantic for downstream consumers reading the cursor.

The two stamps are NOT mutually exclusive — they're a write-ahead / write-after pair that keeps the cursor honest under both kill paths (Step 3 covers kills in Steps 4-10; Step 9 refines the happy-path value).
