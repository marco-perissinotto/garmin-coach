# Decisions log

ADR-style notes appended during implementation. Each entry: context → decision → consequence. Dates are 2026-10-28
unless stated.

## Phase 0

**D-001 Repo location.** On the Windows laptop "Documents" is redirected by OneDrive Known Folder Move to
`%USERPROFILE%\OneDrive - <tenant>\Documents`. The repo lives in `%USERPROFILE%\Documents\Projects\garmin-coach`,
which is a plain local folder, not a junction and not the redirected one, so it is not synced. `doctor` checks this
with `is_cloud_synced()` (OneDrive env vars and path markers).

**D-002 Owner data moved out of the repo.** `athlete.yaml`, `events.yaml`, `history.md` and `coach-notes.md` moved from
`docs/` to `%LOCALAPPDATA%\garmin-coach\config\`. `.gitignore` ignores these names anywhere; only `config/*.example.*`
are versioned. `docs/knowledge-transfer-prompt.md` contains no personal data and stays in the repo.

**D-003 Secret scanning.** `.githooks/pre-commit` (enabled with `git config core.hooksPath .githooks`) refuses staged
diffs containing a key-shaped string (`GCDC_` + ≥ 8 chars) or a JWT; `tests/test_redaction.py` scans every committable
file the same way. The literal prefix never appears in code (it is built as `"GCDC" + "_"`). A plain `git grep GCDC_`
still matches the *prose* of `docs/PLAN.md` (the rule text itself); no key-shaped string exists in the repo.

**D-004 MCP SDK 2.x.** The installed `mcp` is 2.3: `FastMCP` was renamed `MCPServer` (`mcp.server.mcpserver`).
The plan's "FastMCP" means this class.

## Phase 1: schema (deviations from PLAN §5.2)

**D-010 HRV baseline (answers §14 Q1).** `HrvSummaryDTO.baseline` is `BaselineScoreDTO {lowUpper, balancedLow,
balancedUpper, markerValue}`. Columns: `baseline_low_upper`, `baseline_balanced_low`, `baseline_balanced_upper`,
`baseline_marker` (instead of the placeholders `baseline_low/upper`).

**D-011 `raw_payload.last_seen_at`.** An identical body is not re-inserted (dedupe by hash) but its `last_seen_at` is
bumped. Normalization replays payloads in `last_seen_at, id` order, newest non-null wins (`COALESCE` upserts; activity
list rows replace the summary columns). Incremental normalization and `normalize --all` therefore yield identical
tables (tested).

**D-012 `profile_snapshot` primary key `(taken_at, kind)`.** `taken_at` is the raw row's first `fetched_at`, so
re-seeing an identical body adds no snapshot.

**D-013 Extra columns/tables.** `activity.summary_hash` (detects edited activities), `sync_state.note`,
`job.force`, `job.cancel_requested` (cross-process cancel), and the `endpoint_cap` table (learned window caps;
`cap_days = 0` = endpoint rejected, see D-030).

**D-014 Weight units.** The spec documents kilograms; Connect historically returns grams. Values < 1000 are
treated as kg and stored as grams (`weight_g`, `muscle_mass_g`). To verify against real weigh-ins (none yet, see D-030).

## Phase 1: sync behaviour

**D-020 Activity search uses `client.get()`.** The official wrapper `search_activities()` lacks `sortBy`/`sortOrder`;
the documented params are passed through `get()` (PLAN §0.4 allows `get()`).

**D-021 Dynamic activity discovery.** The plan is computed before execution, except for activity paging and details:
after each search page the planner adds the next page and detail/workouts calls, and `calls_planned` grows.

**D-022 Paging stop rule.** The list is newest first, so recent refreshes page further only when *every* row of a full
page is new or changed (max 3 pages). The literal rule ("stop when a page has only known IDs") would cost one extra page
per new activity. Backfill pages through the whole window.

**D-023 Aligned backfill chunks.** Backfill range chunks are aligned to fixed windows (anchor 2000-01-03), so a backfill
resumed on a later day finds the same `sync_state` keys and does not refetch history. Activity list pages are keyed by
start date, so a backfill resumed on another day re-pages the list (≈ 2–4 calls).

**D-024 Backfill completes A, C, E too.** A finished H job also marks A, C and E fresh: it fetched the same endpoints with
a window ending today. B (today's single-day endpoints) is not covered.

**D-025 404 tombstones (partial answer to §14 Q4).** 404 = no data, counted as success. For days before yesterday it is
not retried for 24 h; for today/yesterday it follows the tier TTL, because the watch may sync later.

**D-026 Tier-level TTL in the runner.** Without `--force`, a refresh job skips tiers that are still within their TTL
(`sync` twice in a row makes 0 calls), and within a stale tier the fresh slices are skipped too.

**D-027 Weight in backfill.** Backfill uses `weight.all` only (every weigh-in); `weight.range` is used in the 7-day C
refresh, which saves ~12 calls.

**D-028 "Today" = this machine's local date (§14 Q7, partial).** Garmin reports profile time zone `Europe/Paris`,
same offset as the owner's Europe/Rome. Travel behaviour still to be confirmed.

**D-029 CLI process model.** `sync`/`backfill` run their job in-process and show a progress bar; Ctrl-C kills the job,
which the next start marks `orphaned` and resumes. `--background` spawns a detached `garmin-coach` child process,
because a thread would die with the CLI.

## Phase 1: findings from the first live backfill

**D-030 `weight.all` returns 400 for every window, down to 1 day.** On the first real backfill the 400-halving rule
cascaded into ~50 single-day calls, after which Garmin answered 429 three times and the circuit breaker opened
(2 h cooldown on tier H). Fix: halving stops at 7 days; a 400 on a window ≤ 7 days marks the endpoint *rejected* for 7
days (`endpoint_cap.cap_days = 0`), drops its remaining calls in the run, and counts as tombstoned, so the tier can still
complete. After 7 days it is probed again. The 400 body is now logged (redacted, ≤ 200 chars) to learn the cause.
Hypothesis: the account has no weigh-ins.

**D-031 Rate limit data point (§14 Q6).** The 429s arrived after ~195 calls in ~2 min, right after the 400 flood on a
single endpoint, so this may be per-endpoint abuse protection rather than a global budget. No `Retry-After` was
recorded (logging added: every 429 is now logged with attempt, run call count and `Retry-After`). Backfill jobs now
use `backfill_call_gap_ms: 1000` (on-demand refreshes keep 500 ms).

**D-032 Normalization per call, in the same transaction as the raw write.** In the live kill test, job #1's 78 payloads
were stored raw but never normalized, because normalization ran only at the end of a run. Now every successful call
commits `raw_payload` + normalized rows + `sync_state` atomically. Activity paging decisions are taken *before* the
page is normalized. Covered by `test_killed_run_leaves_normalized_tables_consistent`.

**D-033 Budget-limited backfills chain automatically.** The owner wants the coach to know the whole Garmin history
(activities start in 2016), i.e. ~1 050 calls on this account. A backfill job that ends `partial` only because of
`backfill_budget_calls` now enqueues its continuation (same trigger) *before* turning terminal, so the history loads
in one go; `backfill` in the CLI follows the chain. Orphaned/cancelled continuations are still picked up by the next
startup check. On this machine `settings.yaml` sets `backfill_days: 4000` and `backfill_call_gap_ms: 1500`
(~25 min in the background). Covered by `test_budget_limited_backfill_chains_until_done`.

**D-034 Window caps are learned on success, never below 7 days.** Launching the full-history backfill planned 4 886
calls instead of ~1 050: the first live run had persisted a learned cap of 1 day for `weight.all` at the moment of
each 400, before knowing whether the smaller window worked, so 4 000 days became 4 000 one-day calls. The job was
cancelled after 11 calls. Now a shrunk window that *succeeds* sets the cap (≥ 7 days), and the planner ignores any
stored cap below 7 days. The stale row was deleted. Regression test:
`test_learned_cap_only_after_success_and_never_below_7_days`.

**D-035 Persistent 5xx on a range call halves the window.** During the full-history load, `usersummary.range`
answered HTTP 500 (after 5 tries) to the full-year windows 2023-12-28..2024-12-26 and 2024-12-27..2025-12-26, while
half-year windows worked. A `ServerError` on a range call wider than 7 days now splits it like a 400 does; the cap
learned on success (182 days) is used from then on. Unlike a 400, a 5xx never disables the endpoint.

**D-036 Full-history result (2026-10-28).** 4 chained backfill jobs (#4–#7), ~1 050 HTTP calls at 1.5 s, no 429.
Cache: 1 311 activities from 2016-05-30, daily data from 2020, sleep from 2022, HRV from 2023 (what the devices of the
time recorded). Activity gap 2017–2020 is in Garmin, not a sync error. `weight.all` rejected again (body `{}`),
so the account has no weigh-ins via this endpoint; still disabled for 7 days at a time.
