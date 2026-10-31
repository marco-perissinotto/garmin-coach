# Decisions log

ADR-style notes appended during implementation. Each entry: context → decision → consequence. Dates are 2026-10-28
unless stated.

## Phase 0

**D-001 Repo location.** On Windows, OneDrive Known Folder Move can redirect "Documents" to
`%USERPROFILE%\OneDrive - <organisation>\Documents`, while `%USERPROFILE%\Documents` may still exist as a plain local
folder. The repo must live in a folder that is not synced; `doctor` checks this with `is_cloud_synced()` (OneDrive
env vars and path markers).

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

**D-028 "Today" = this machine's local date (§14 Q7, partial).** The profile time zone Garmin reports may be a
different zone name with the same offset as the machine's. Travel behaviour still to be confirmed.

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

**D-033 Budget-limited backfills chain automatically.** A coach that knows the whole Garmin history of a
long-standing account needs about a thousand calls or more. A backfill job that ends `partial` only because of
`backfill_budget_calls` now enqueues its continuation (same trigger) *before* turning terminal, so the history loads
in one go; `backfill` in the CLI follows the chain. Orphaned/cancelled continuations are still picked up by the next
startup check. For a full history set e.g. `backfill_days: 4000` and `backfill_call_gap_ms: 1500` in
`settings.yaml` (~25 min in the background for ~1 000 calls). Covered by `test_budget_limited_backfill_chains_until_done`.

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

**D-036 Full-history result (2026-10-28).** 4 chained backfill jobs, ~1 050 HTTP calls at 1.5 s, no 429. Each data
type starts when the devices of the time began recording it, and gaps in the activity list come from Garmin, not from
the sync. `weight.all` was rejected again (body `{}`) on an account without weigh-ins; the endpoint stays disabled
for 7 days at a time.

## Phase 2: metrics and context packs

**D-040 No VO2max: efficiency proxies instead.** `vO2MaxValue` is empty on every activity of the test account and no other
Personal API payload carries VO2max. `metrics_daily.vo2max_run/bike` stay NULL; the packs show running metres per
heartbeat and cycling watts per bpm on steady sessions, indoor and outdoor apart (`docs/METRICS.md`).

**D-041 Schema v2/v3.** v2 adds per-activity fields needed by the metrics (HR time in zone, mean-maximal power,
running cadence, grade-adjusted speed, multisport parent flag) and rebuilds the normalized layer from raw offline.
v3 adds `derived_state` (fingerprint of the data `metrics_daily` was computed from). A migration can declare that it
needs a rebuild; `db.connect` runs it.

**D-042 TRIMP fallback calibrated to Garmin load.** Calibration factor = median of `load / TRIMP` on activities
having both (close to 1 in practice); older rides recorded without Garmin load use the fallback.

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
optical artefact (values typical of hard efforts seen on rest days), so `daily.hr_max` is not used by any metric or pack.

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

**D-056 Client registration.** The Microsoft Store build of Claude Desktop keeps its config in
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
a real multi-year account.

## Coaching state (2026-10-30)

**D-060 Short-term state is a separate, overwritable YAML file.** `coach-notes.md` is append-only and for durable
rules (PLAN §11.2); mixing in symptoms, plans and pending decisions would make the rules file grow with stale facts
and the skill would have to parse dates to ignore them. `coaching-state.yaml` in the config folder holds the
*current* state only, with three typed sections (reported symptoms, scheduled plan, decisions open/taken), a
file-level `updated` timestamp and the same limits/redaction as the note tool. YAML + pydantic because the other
owner inputs already work that way, because per-section overwrite and typed dates need structure, and because the
model never reads the raw file (it gets JSON from the tools and Markdown from the brief). Implemented in `state.py`
(pure, no MCP imports); the schema is in PLAN §11.3.

**D-061 Per-section replace.** `update_coaching_state` replaces the sections it receives and keeps the others; an
empty section clears it; `confirm=true` bumps the date without changes. Whole-file replace risks silently dropping
a section the model did not re-read; item-level patching needs ids the model would have to track. A section is at
most 8–14 short items, cheap to restate. The answer lists the sections written in `changed` (`["confirmed"]` for a
bare confirmation).

**D-062 The state enters the daily brief, not the profile.** `ContextPack.coaching_state` is the same object
`get_coaching_state` returns (with `state: null` and a note when there is no file, `state: null` and `problems` when
the file is unreadable), rendered right after "Segnali" (reported symptoms outrank HRV and sleep); it is `None` only
in past-date packs, which omit the section and the `get_daily_brief` key. `daily_token_budget` default
2 500 → 3 500 because a full state is ≈ 1 000 tokens (tested with a state near the 4 000-character cap).
`get_coaching_state` exists for explicit re-reads; `get_athlete_profile` is unchanged.

**D-063 Stale = older than `context.state_stale_days` (7).** Flagged in the brief and in the tool answer, confirmed
by the coach with the athlete; never auto-deleted or hidden. `plan_expired` and `decisions_due` are derived on read
(the plan end is `plan.to`, or the last plan day when a hand edit left `to` out). A due decision is flagged "entro
oggi" on its deadline and "scaduta il dd/mm" once the deadline has passed. Symptoms older than 28 days are a skill
rule ("propose moving it to history.md"), not a tool rule.

**D-064 Validation, limits, atomic write.** Same path as `append_coach_note`: whitespace collapse, `redact()`,
min 3 / max 300 characters per text, item caps per section, 4 000 characters per file; dates ISO within
today − 7 .. today + 90 (`since`/`noted`/`date` not in the future). Read-modify-write under a lock, temp file +
`os.replace`, LF line endings, no YAML anchors, the fixed header re-emitted (hand comments are dropped, the header
says so). A partial update refuses to overwrite a file it cannot parse; passing all three sections replaces it.
Amendment: the lower bound of the date window (today − 7) applies only to the sections being written. Reading a
file applies the limits and the rules that cannot expire (not in the future, at most today + 90), so a state kept
for weeks is flagged stale instead of turning "invalid", and an update of one section never fails because of the
dates of another. "Today" always comes from the injectable clock (`load_inputs(paths, today)`, `save_state(…, now)`).

**D-065 The metrics fingerprint excludes the state.** `inputs_mtime()` used the newest mtime of every file in
`config/`, so each state write would have forced a ~0.7 s `metrics_daily` recompute (D-045). It now looks only at
`athlete.yaml` and `events.yaml` (`builder.METRICS_INPUTS`); edits of the Markdown files no longer trigger it either.

**D-066 Migration is manual and one-off.** When the brief carries no state, the skill tells the coach to propose an
initial state from state-like lines found in `coach-notes.md`; the owner deletes those lines by hand. The note tool
stays append-only (no delete surface), there is no DB migration, and `doctor` does not try to classify note lines:
it only reports the state file as absent ("none yet"), unreadable, or present with its age and size.

## Phase 5a: FIT ingestion (2026-10-31)

The design document `PLAN-FIT.md` guided the implementation and was removed afterwards, as the coaching-state design
was: `FIT.md` holds the schema and the ingestion rules, these entries hold the decisions and the deviations.

**D-070 Storage: ZIP on disk + one JSON column.** As designed: the ZIP is the raw layer
(`<app data>/fit/<activityId>.zip`, kept by default), the summary is `activity_fit.data` (JSON) with scalar columns for
status and counts; no per-lap/per-record tables; bytes never go to `raw_payload`. Schema v4 adds `activity_fit` and
`app_flag` without a rebuild (an existing v3 database keeps every row, tested).

**D-071 Downloads are pseudo-tier F.** As specified: a 404, a parse error, a 5xx or a budget stop on a download only
adds F to the failed tiers; A completes on search + detail + workouts. F is not in `REFRESH_TIERS`, has no TTL (the
runner treats it like H: never skipped as "fresh") and the gate never requests it. Downloads wait in a second queue
that runs only when the main one is empty, so paging and detail calls always go first, across pages too.
Consequence: the first tier A refresh after the upgrade also downloads the FITs of the activities of the last 28 days
(one call each, after the calls that complete A, inside the 30-call / 25 s synchronous budget); what does not fit is
picked up by the next refresh.

**D-072 Fetched once.** As specified. "Tried" = `sync_state` status 2xx or 4xx except 429: a 400 is not retried,
429 and 5xx are (planner and `pending_ids` share the rule). A parse failure keeps the ZIP (`parse_error`) and is fixed
offline by `fit reparse`.

**D-073 Historical FIT backfill = job kind `fit`.** As specified (whole plan newest first, budget-limited, chained,
yielding, never auto-started). `fit backfill` and `refresh(scope="fit")` refuse when `fit.download` is false.

**D-074 Parser pure and versioned.** As specified: `fit/parse.py` has no DB, paths or content logging and imports the
SDK lazily (the MCP boot and `context show --offline` never load it, checked). `normalize --all` does not re-parse; it
restores `activity.has_fit` from `activity_fit`.

**D-075 Privacy and size.** As specified. Measured on the synthetic 31-minute interval file (1 870 records, 11 laps):
8.8 KB of JSON with series, 0.1 s to decode.

**D-076 Settings move.** As specified: `fit:` block, `SyncSettings.download_fit` removed. An old `sync.download_fit`
key is ignored (the models allow extra keys) and `doctor` warns about it.

**D-077 Small additions to the FitSummary.** (a) Computed zones (`source: "computed"`) carry `floors_bpm` (the Garmin
zone floors used) instead of `boundaries_bpm`: they are floors, not the device's high boundaries. (b) `device.sensors`
lists only `antplus_device_type` / `ble_device_type` names: `source_type` names the transport (`antplus`, `local`),
not a sensor. (c) `records.power_source` is omitted instead of `null` (the "never null" rule of §5). (d)
`fit.files_mb` in the status is a float with one decimal. (e) `meta.profile_version` comes from the file header
(÷1000 from 22.0, ÷100 before, the SDK's rule). No key was renamed or removed.

**D-078 `workout_rpe` is not scaled by the SDK.** The design expected the SDK to turn a stored 70 into 7.0; in
profile 21.218 the field has scale 1, so the SDK returns 70. `session.rpe` = value ÷ 10 when above 10. `feel` is kept
as stored (0-100: Garmin's five steps 0/25/50/75/100), not mapped to 1-5. To confirm on a real file with an RPE.

**D-079 `fit.store` API: `prepare` + `save`.** The design had `ingest` write the file, parse and upsert, and the
runner call it inside `BEGIN IMMEDIATE`. Decoding inside the transaction would hold the SQLite write lock for the
parse time (seconds for a long ride), blocking every other writer (the gate of another process). The
runner calls `prepare` (write the file, parse; reads only) before `BEGIN` and `save` (row + `has_fit`) inside it,
with `sync_state`; `ingest` = `prepare` + `save` for the CLI and tests. `hr_zones` is the list of the five zone floors,
looked up per sport (`RUNNING`/`CYCLING`/`SWIMMING` zones when Garmin has them, else `DEFAULT`). `parser_version` is
stored for `no_fit` rows too (NULL only for `not_found`), so `fit reparse` does not revisit them forever.

**D-080 `get_activity` lazy fetch.** `_lazy_detail` became `_lazy_fetch(conn, row)`: it now runs also on an activity
that has its detail but no FIT attempt (1 call), skips the download while tier F is in cooldown, and reports
`detail_problem` and `fit_problem` separately (`fit_status` stays `not_downloaded` after a failed download, which is
retried on a later request).

**D-081 Yielding without losing the chain.** Three amendments to the designed yield scheme, found while writing the job tests:
(a) a fit job does not take the lock while `sync_wanted_at` is fresh (it waits, at most 30 s since the flag then goes
stale), and a fit job that yielded is continued even with 0 calls done: otherwise a continuation that wins the race
with the gate yields at once and the chain ends. (b) Refresh and backfill jobs that find the lock busy also set the
flag while they wait (the design said background jobs do not): `fit backfill --background` runs in its own process, so
without it the MCP server's background C/D/E refresh would fail `locked` during the whole chain. (c) A fit job retries
the lock up to 10 times with `sync.lock_wait_seconds` each (the design said 5 s apart), i.e. it waits for a background
refresh of ~60 calls instead of failing `locked`. The flag is cleared by whoever set it; two concurrent waiters may
clear each other's flag, which only costs a later yield.

**D-082 Test fixtures.** `tests/fitgen.py` builds every FIT with `garmin_fit_sdk.Encoder` (no binary committed): an
11-lap interval ride with time_in_zone, a creator device and an HR strap, plus variants (running, developer power
field, timer pause, no positions, session-level zones). The sync tests use a 110-record variant so that a backfill
with ~12 downloads stays fast. `FakeGarmin` serves it for `activity.download` (`no_fit_ids` → 404, `bad_zip_ids` →
random bytes, `gpx_ids` → a ZIP with a `.gpx`).

**D-083 Live findings.** To be filled in after the owner's live run: ZIP member naming and the answer
for multisport children / manual entries / GPX uploads; whether session TSS, TE and training load peak are present;
the meaning of `time_in_hr_zone[0]`; the shape of `left_right_balance`; decode time per file; 429 behaviour of a
100-call fit job at 1.5 s; disk use after the full backfill.
