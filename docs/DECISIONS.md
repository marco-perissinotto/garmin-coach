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

## Phase 2: metrics and context packs

**D-040 No VO2max: efficiency proxies instead.** `vO2MaxValue` is empty on all 1 311 activities and no other
Personal API payload carries VO2max. `metrics_daily.vo2max_run/bike` stay NULL; the packs show running metres per
heartbeat and cycling watts per bpm on steady sessions, indoor and outdoor apart (`docs/METRICS.md`).

**D-041 Schema v2/v3.** v2 adds per-activity fields needed by the metrics (HR time in zone, mean-maximal power,
running cadence, grade-adjusted speed, multisport parent flag) and rebuilds the normalized layer from raw offline.
v3 adds `derived_state` (fingerprint of the data `metrics_daily` was computed from). A migration can declare that it
needs a rebuild; `db.connect` runs it.

**D-042 TRIMP fallback calibrated to Garmin load.** Calibration factor = median of `load / TRIMP` on activities
having both (1.09 on the owner's data); 113 days use the fallback (mostly 2021–2023 rides without load).

**D-043 Readiness HRV penalty scales with consecutive nights below the normal range.** Garmin's HRV status follows
the weekly average and stayed BALANCED in a synthetic case of 3 nights clearly below the range; a flat −0.25 still
gave 75% of the HRV points. Now −0.25 after 1 night, −0.5 after 3+.

**D-044 Packs are written in Italian** (the owner's `advice_language`), including Garmin enum labels (sleep quality,
HRV status, training-effect label). `coach-instructions.md` has an English variant for other languages; the other
templates are Italian only for now.

**D-045 Metrics are cached, packs are rendered on demand.** A full recompute over ~3 800 days takes ~0.7 s, too close
to the 1 s budget of `context show --offline`. Metrics are recomputed after every sync that stored data
(post-run hook in Runner → `App.post_run`) and when the fingerprint changes; `show` renders from `metrics_daily`
(~0.05 s). The hook never raises: a rendering failure cannot fail a sync.

**D-046 Lazy imports for fast offline commands.** `requests`, the official client, the sync engine and `rich` are
imported only when needed (`garmin/errors.py` split from `client.py`; `secrets.py` duplicates two constants of the
official package, guarded by a test). `context show daily --offline`: median 0.86 s wall clock over 15 runs on the
Windows laptop (rendering ~0.05 s; the rest is Python start-up; occasional antivirus spikes up to ~4 s).

**D-047 Flags beyond the plan.** The brief also flags: yesterday's load ≥ 2× the chronic daily load (the readiness
score barely moves after a single hard day), no sleep data for last night (watch not synced), events within 7 days,
and problems loading the owner's files.

**D-048 Daily HR maximum is not used.** Garmin's daily HR maximum can be an
optical artefact (174 bpm on a rest morning, 2026-10-29), so `daily.hr_max` is not used by any metric or pack.

## Phase 3: MCP server and Claude integration (2026-10-29)

**D-050 One SQLite connection per tool call.** The SDK runs sync tools in worker threads, so two questions can run
at once. Each call opens its own connection and its own freshness gate; the refresh lock (with its double-check)
keeps refreshes single, and the "one background job" guard of the gate is now process-wide instead of per gate
instance. Covered by `test_concurrent_calls_make_one_refresh`.

**D-051 Tool contract.** Tool logic lives in `mcp/tools.py` (`CoachService`), registration and model-facing
descriptions in `mcp/server.py`. Tools never raise: bad input and internal errors come back as `{"error": ...}`;
a missing API key adds an `error` pointing to `doctor` while the cached data is still served. Payload guard:
60 000 characters per answer, plus per-tool caps (92 days day-by-day, 731 days by week, 100 activities, 40 splits,
Body Battery curve at 15 min, HR samples averaged per 10 min). `get_daily_brief` returns the brief as structured JSON
(the Italian labels of the packs included); the Markdown is available through `get_context_pack`. A brief for a past
date is built "as of" that day, while metrics are always computed for the real today.

**D-052 `max_age_minutes=0`.** Two gate bugs found in the live test: after a forced refresh the answer still said
`stale=true` (with max age 0 every age is stale, including the tier just refreshed), and forced background tiers were
enqueued without `force`, so the job skipped them as fresh. Fixed: tiers refreshed by the request count as fresh, and
the background job inherits `force`. In the MCP layer, 0 forces only the synchronous tiers (A/B) when the tool needs
them ("I just synced my watch" = ~7 calls, not ~20); background tiers are forced only when they are all the tool needs.

**D-053 Lazy activity detail does not touch tier state.** `get_activity` on an activity without detail runs the two
calls with an empty tier set, so a single-activity fetch never marks tier A fresh; cooldowns on A are respected.

**D-054 `refresh` rate limit.** Once per tier per 5 minutes, counting only refresh jobs requested through MCP: a
startup or freshness-gate job must not block "I just synced my watch" right after Claude starts. An active job that
already covers the tiers is returned instead of enqueuing another (unless `force`).

**D-055 Prompts.** The five prompts embed the packs they need and give their instructions in the owner's
`advice_language` (Italian or English). `explain_metric` quotes the matching section of `docs/METRICS.md`, read from
the repo the server runs from (`uv run --project`).

**D-056 Client registration.** On this laptop Claude Desktop is the Microsoft Store build: its config lives in
`%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\`, not in `%APPDATA%\Claude\`; `install` picks the package
folder when it exists. The `uv` path is absolute but not symlink-resolved (the WinGet/Homebrew shim survives uv
upgrades). The file is backed up (`.bak`), other entries are kept, invalid JSON is refused. `--dry-run` prints only the
garmin-coach entry, because other servers' entries may carry their own secrets. `install claude-code` replaces our
own entry (`claude mcp remove` then `add`). The committed `.mcp.json` uses `uv` from PATH and `--project .`, so it is
portable between machines.

**D-057 HTTP mode.** `mcp --http` serves streamable HTTP on 127.0.0.1 only, behind the SDK's bearer-token check with
a random token generated at each start and printed to stderr (never logged).

**D-058 Measurements.** Server start to MCP `initialize` over stdio: 1.6–2.1 s; about 0.9 s of it is importing the
MCP SDK, our modules add ~0.1 s. A tool on a fresh cache answers in ~0.05 s; a live forced A+B refresh made 7 calls
in 4–8 s (the activity search alone took 0.9–4.3 s on Garmin's side). The `daily_checkin` prompt is ~5k tokens with
the owner's real data.
