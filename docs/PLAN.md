# garmin-coach — Implementation Plan

> **Audience:** this document is written for an implementing AI model (Claude Opus) and for the project owner.
> It is a *specification and work plan*, not an implementation. Follow it phase by phase, finish each phase's
> acceptance criteria before starting the next, and do not invent API endpoints that are not listed here.
>
> **Goal in one sentence:** build a local, read-only pipeline that keeps a private cache of the owner's Garmin Connect
> health and training data, **refreshes that cache on demand when a consumer asks for data and the cache is older than
> its TTL** (no background scheduler), turns it into a compact "athlete context", and exposes that context to AI
> assistants (Claude Desktop and Claude Code through a local MCP server) so the owner gets daily coaching advice toward upcoming sport events without
> pasting screenshots by hand.

---

## 0. Hard rules for the implementer

1. **Never print, log, commit, or echo the Garmin API key or a bearer token.** Not in debug output, not in error messages,
   not in test fixtures, not in process arguments. Redact anything that starts with `GCDC_` or looks like a JWT.
2. **Read-only.** The Personal API only allows `GET`. The code must never attempt `POST/PUT/DELETE` against Garmin
   (the only `POST` is the token exchange).
3. **Local-only data.** Health data lives only on the owner's machines. No telemetry, no cloud sync, no third-party SaaS
   in the pipeline. The MCP server must bind to stdio or `127.0.0.1` only.
4. **Use the official client.** `garmin-connect-personal-api-python` (PyPI, by Garmin) handles token exchange and caching.
   Do not reimplement auth. Use its `get()` method for endpoints that have no wrapper.
5. **Use only the 26 documented endpoints** in §3 (also in `docs/garmin-personal-api-openapi.json`). Undocumented paths
   return 404 on this host.
6. **Be polite to the API.** Rate limits are not published. Garmin is called only in three situations: (a) a consumer
   (MCP tool, CLI `context` command) asks for data and the relevant cache slice is older than its TTL (default 60 min);
   (b) a process starts (MCP server or CLI) and the startup check (§6.6) finds the cache empty or stale, in which case
   **one** background job is launched; (c) the owner forces a sync. There is no periodic polling. Every refresh has a
   request budget, retries with backoff on `429`/`5xx`, and a circuit breaker (see §6.4).
7. **Deterministic context.** The context-building step is pure code, no LLM calls. The LLM is the *consumer*, not part
   of the pipeline.
8. **Keep the repo and the data separate.** Code in git (private). Data, secrets, token cache in OS user directories
   that are *not* synced by OneDrive/iCloud.
9. **Do not store the `.txt` files from the original folder in the repo.** `api-key.txt` must be deleted after the key is
   moved to its proper location (see Phase 0).

---

## 1. Current situation and decisions

### 1.1 Starting point
- The owner has a Garmin Connect **Personal API key** (1-year expiry at most, read-only by design).
- Until now, daily training advice came from a general-purpose chat assistant fed with pasted screenshots.
- One or two machines: Windows 11 and/or macOS, with Python and `uv`.

### 1.2 Security checks to do first (Phase 0)
- If the key was ever saved in a **cloud-synced folder** (OneDrive, iCloud, Dropbox), it is in that service's version
  history. **Delete that key in Garmin Connect and create a new one**, then store the new key only in
  `~/.config/garmin/connect-personal-api-key` (the official client's default path, mode `600`).
- The project folder itself should **not** live under a synced folder, otherwise the SQLite cache with health data is
  synced too. Use e.g. `C:\dev\garmin-coach` (Windows) / `~/dev/garmin-coach` (Mac). Data goes to the per-OS
  app-data directory (§5.1).

### 1.3 Technology decision
| Concern | Decision | Why |
|---|---|---|
| Language | **Python ≥ 3.12** | Garmin's official client is Python; MCP Python SDK is mature; FIT parsers exist; runs identically on Windows/macOS. |
| Package/env manager | **uv** (`uv init`, `uv run`, `uv tool`) | Already installed here; produces a lockfile; `uvx` is how Claude Desktop/Code will launch the MCP server. |
| HTTP/auth | `garmin-connect-personal-api-python` (official) | Token exchange, JWT `exp` handling, 1-hour refresh window, cache file with `0600`, retries not included (we add them). |
| Cache | **SQLite** (stdlib `sqlite3`, WAL mode), single file | Zero ops, atomic, portable, easy to query; raw JSON kept verbatim so the schema can evolve without re-polling. |
| Models/validation | `pydantic` v2 | Typed normalized records; tolerant parsing (`extra="allow"`) because Garmin may change fields. |
| CLI | `typer` | Commands: `sync`, `backfill`, `context`, `mcp`, `status`, `doctor`. |
| Refresh trigger | **On-demand, staleness-driven ("freshness gate", §6.5)** inside the MCP server and the CLI, plus a **startup check (§6.6)** that launches one background job when the cache is empty or stale | No OS scheduler, no daemon, nothing to install on either machine. The cache is refreshed when someone actually asks for data and it is older than its TTL; when the MCP server boots it pre-warms the cache in the background so the first question is already fast. An empty cache triggers the initial backfill automatically. |
| Agent interface | **MCP server over stdio** (Python `mcp` SDK, `FastMCP`) | Native for Claude Desktop and Claude Code; local, no network. Optional HTTP mode bound to localhost only. |
| Context format | Markdown "context packs" + JSON | Markdown is what you paste/upload into ChatGPT Projects; JSON is what MCP tools return. |
| FIT parsing (optional, Phase 5) | `garmin-fit-sdk` (official) or `fitdecode` | Lap/record-level data for interval analysis. |
| Multi-machine | Private git repo for code; **each machine keeps its own cache and refreshes independently** | Avoids syncing a SQLite file through a cloud drive; refreshes are idempotent and cheap, and a machine that is not being used makes zero calls. |

### 1.4 Architecture decision: "local context engine + thin MCP"
Why not "just an MCP server that calls Garmin live"? Because:
- Every chat question would hit Garmin (rate limits unknown, latency, 24h token churn).
- The agent needs *trends* (7/28/90 days), which means dozens of calls per question without a cache.
- ChatGPT cannot reach a local MCP server at all; it needs either a public HTTPS endpoint or a pasted/uploaded document.

And why not a background scheduler that polls every hour? Because it needs installing on every machine, runs while
nobody is asking anything, and still serves data that can be up to an hour old. Instead the cache is **refreshed
lazily**: every consumer request passes through a *freshness gate* that compares the age of the relevant cache slice
with its TTL and refreshes only what is stale, then serves the request. Same end result, zero idle traffic, nothing to
install.

So the system has three decoupled layers:

```
 Claude Desktop / Claude Code ──► [MCP server] ──┐
 CLI `context show|export`  ──────────────────────┤
                                                  ▼
                                        [freshness gate]  "is this slice older than its TTL?"
                                           │ yes                       │ no
                                           ▼                           │
 Garmin Personal API ◄──GET──► [sync: fetch+normalize] ──► SQLite cache ◄┘
                                                                │
                                                                ▼
                                                 [context builder] ──► packs (md/json) ──► ChatGPT Project (upload)
```

---

## 2. Garmin Personal API — facts verified on 2026-10-28

| Item | Value |
|---|---|
| Base URL | `https://personal.connectapi.garmin.com` (a `.cn` host exists; ignore it) |
| Token endpoint | `POST /personalapitoken-service/personal-api/personal-api-key/token` with header `Authorization: ApiKey <key>` |
| Token response | **Plain text body containing a JWT** (not JSON). Lifetime 24 h or until the key expires. No refresh token: re-exchange the key. |
| Data calls | `Authorization: Bearer <jwt>`; all 26 endpoints are `GET` |
| Key lifetime | 30/90/180/365 days chosen at creation; Garmin emails ~1 week before expiry; the API does not expose the expiry date |
| Key revocation | Deleting the key in Garmin Connect Web immediately invalidates all tokens derived from it |
| Scopes seen in JWT (community report) | `CONNECT_READ`, `PERSONAL_API_FITNESS_READ`, `PERSONAL_API_NUTRITION_READ`, `PERSONAL_API_USER_READ`, `PERSONAL_API_WELLNESS_READ` |
| Rate limits | **Not published.** Treat `429` as authoritative, honour `Retry-After`. |
| Terms | Personal use only, single user, not for commercial multi-user apps. Credit Garmin if publishing charts. |
| Not available on this tier | Training Status, Training Readiness, Training Load Balance/Focus, race predictor, Garmin Coach plans, saved workouts, devices, gear, goals (except weight goal), personal records, blood pressure, any write/push (Courses/Training APIs are business tiers), TCX/GPX/CSV exports (only original-format download, usually a ZIP containing a `.fit`). |
| Important nuance | Per-activity **`vO2MaxValue`, `aerobicTrainingEffect`, `anaerobicTrainingEffect`, `activityTrainingLoad`, `trainingEffectLabel`, `lactateThresholdBpm`, `maxFtp`, power curve `maxAvgPower_*`** ARE present in the activity list schema. So training load and VO2max trends can be **derived locally** even though the "training status" endpoints are missing. |

Sources: Garmin developer pages (Personal API, FAQ, Swagger), the official client source (`constants.py`, `auth.py`),
and community implementations (garmin-mcp issue #42, personal-data-for-garmin-connect, garmin-personal-mcp).
The complete OpenAPI 3.0.1 document (26 paths, 109 schemas) was extracted from Garmin's Swagger page and saved as
`docs/garmin-personal-api-openapi.json`. **Treat that file as the source of truth for field names.**

---

## 3. Endpoint catalogue and refresh policy

Date format for all `*Date` parameters: `YYYY-MM-DD` (local calendar date). Window caps come from the spec descriptions;
where the spec says nothing, use the conservative value and shrink on `400`.

| # | Endpoint (GET) | Params | Window cap | Official wrapper | Use in this project | Refresh tier |
|---|---|---|---|---|---|---|
| 1 | `/activitylist-service/activities/search/activities` | `start`, `limit`, `startDate`, `endDate`, `sortBy`, `sortOrder`, `activityType`, … | paginated (`limit` ≤ 100 safe) | `search_activities` | Discover new/changed activities; main source of training load, TE, VO2max | **A** TTL 60 min |
| 2 | `/activity-service/activity/{activityId}` | path | n/a | `get_activity` | Full summary + split summaries for new activities | A (only new IDs) |
| 3 | `/activity-service/activity/{activityId}/workouts` | path | n/a | `get_activity_workouts` | Structured workout followed (if any) | A (only new IDs) |
| 4 | `/download-service/files/activity/{activityId}` | path | n/a | `download_activity` (bytes, ZIP) | Optional FIT archive (Phase 5) | A (only new IDs, if enabled) |
| 5 | `/usersummary-service/usersummary/daily` | `calendarDate` | 1 day | none → `client.get("/usersummary-service/usersummary/daily", params={"calendarDate": d})` | RHR, 7-day RHR avg, stress durations, Body Battery high/low/wake, steps, intensity minutes for **today & yesterday** | **B** TTL 60 min (2 calls) |
| 6 | `/usersummary-service/v2/dailysummaries` | `startDate`, `endDate` | 1 year | `get_daily_summaries` | Range backfill and refresh of last 7 days | **C** TTL 24 h + backfill |
| 7 | `/sleep-service/sleep/dailySleep` | `date` | 1 day | `get_daily_sleep` | Last night in full (stages, score, SpO2, respiration, HRV-ish avg HR, sleep need) | B (today) |
| 8 | `/sleep-service/sleep/dailySleepSummariesByDate` | `startDate`, `endDate` | **31 days** | `get_daily_sleep_summaries` | Backfill + weekly refresh | C |
| 9 | `/hrv-service/hrv/{calendarDate}` | path | 1 day | `get_daily_hrv` | Last night HRV readings + summary (`lastNightAvg`, `weeklyAvg`, `baseline`, `status`) | B (today) |
| 10 | `/hrv-service/hrv/daily/{fromDate}/{toDate}` | path | assume **28 days** | `get_daily_hrv_summaries` | Backfill + weekly refresh | C |
| 11 | `/wellness-service/wellness/dailyHeartRate` | `date` | 1 day | `get_daily_heart_rate` | RHR, min/max, intraday HR (sample arrays can be large; store raw, don't normalize samples) | B (today) |
| 12 | `/wellness-service/wellness/bodyBattery/reports/daily` | `startDate`, `endDate`, `chunks` | **31 days** | `get_daily_body_battery` | Body Battery curve + charged/drained events | B (today, yesterday) / C |
| 13 | `/usersummary-service/stats/stress/daily/{s}/{e}` | path | **28 days** | `get_daily_stress` | Stress daily values | C |
| 14 | `/usersummary-service/stats/steps/daily/{s}/{e}` | path | **28 days** | `get_daily_steps` | Steps/distance | C |
| 15 | `/usersummary-service/stats/hydration/daily/{s}/{e}` | path | **31 days** | `get_daily_hydration` | Hydration (only if the owner logs it) | C |
| 16 | `/wellness-service/wellness/daily/spo2acclimation/{calendarDate}` | path | 1 day | `get_daily_spo2` | SpO2/acclimation (useful for altitude events) | **D** TTL 24 h (today) |
| 17 | `/wellnessactivity-service/activity/summary/range/{from}/{until}` | path | assume 31 days | `get_wellness_activity_summaries` | Breathwork/meditation etc. | C |
| 18 | `/weight-service/weight/range/{s}/{e}` | path + `includeAll` | assume 31 days | `get_weight_measurements` | Daily weight summaries + range average | C |
| 19 | `/weight-service/weight/all/{s}/{e}` | path | assume 31 days | `get("/weight-service/weight/all/...")` | Every weigh-in (body fat, muscle mass if scale supports) | C |
| 20 | `/goal-service/goal/user/effective/weightgoal/{s}/{e}` | path | n/a | `get_effective_weight_goals` | Weight goal | **E** TTL 7 d |
| 21 | `/biometric-service/heartRateZones/v2` | `generateDefaults` | n/a | `get_heart_rate_zones` | Zone floors per sport, LTHR, max HR, RHR used | E |
| 22 | `/fitnessage-service/fitnessage/v2/{calendarDate}` | path + `fullHistoryScan` | n/a | `get_fitness_age` | Fitness age + components | E |
| 23 | `/userprofile-service/userprofile/settings` | none | n/a | `get_user_profile_settings` | Units, time zone, preferences (also the "is my token valid" probe) | E |
| 24 | `/nutrition-service/food/logs/{date}` | path + `includeGoals`, `includeNutrition`, `includeMedia`, `mealId` | 1 day | `get_daily_food_logs` | Only if the owner logs food | D (opt-in) |
| 25 | `/periodichealth-service/menstrualcycle/summary/{calendarDate}` | path | n/a | `get_menstrual_cycle_summary` | Opt-in | D (opt-in) |
| 26 | `/periodichealth-service/menstrualcycle/dayview/{calendarDate}` | path + flags | n/a | `get_menstrual_cycle_day_view` | Opt-in | D (opt-in) |

Refresh tiers → TTL (how old a slice may be before a request triggers a refresh) and refresh mode (§6.5):
- **A — TTL 60 min, synchronous**: 1 search call (`limit=20`, sorted newest first) plus per-new-activity detail calls.
- **B — TTL 60 min, synchronous**: today + yesterday daily summary, today's sleep/HRV/HR/Body Battery.
  Garmin finalizes sleep/HRV after wake-up and may revise during the morning; a 60-minute TTL covers that naturally.
- **C — TTL 24 h, background**: refresh last 7 days of everything range-based (catches late watch syncs and Garmin recalculations).
- **D — TTL 24 h, background**: single-day opt-in endpoints.
- **E — TTL 7 d, background**: static-ish profile data.
- **H — history / backfill (one-off, auto-started when the cache is empty, §6.6; or `garmin-coach backfill`)**: 365 days
  for tiers C/E plus the full activity **list** paginated with `startDate` = today-365, respecting window caps by chunking.
  Activity **detail and workouts** (endpoints 2–3) are fetched during backfill only for the last 28 days; older ones are
  fetched **lazily** the first time a tool asks for that activity (the list already carries load, TE, VO2max, power).
  This keeps the backfill at ~130–200 calls instead of 2 extra calls per activity.

TTLs live in `settings.yaml` (§11) and can be overridden per request with `max_age_minutes` (§9). A tier is "stale" when
its **oldest** required slice is older than the TTL (e.g. tier B is stale if yesterday's summary is fresh but today's is not).

Request budget per on-demand refresh of A+B: ~5–10 calls (≈5–10 s with the 500 ms gap). Background C/D/E refresh:
~25–40 calls. Backfill: ~130–200 calls once, spread with a 0.5–1 s sleep between calls (≈2–4 min in the background).
A machine where no `garmin-coach` process is running makes **zero** calls.

---

## 4. Repository layout

```
garmin-coach/
├── pyproject.toml              # uv-managed; console script `garmin-coach`
├── uv.lock
├── README.md                   # setup for Windows + macOS, security notes
├── .gitignore                  # data/, *.db, *.sqlite*, .env, *.key, context/out/, fit/
├── config/
│   ├── athlete.example.yaml    # template; real athlete.yaml lives in the data dir
│   ├── events.example.yaml
│   ├── history.example.md      # template for the athlete's narrative history (§11.2)
│   ├── coach-notes.example.md  # template for coaching rules learned so far (§11.2)
│   └── settings.example.yaml
├── .mcp.json                   # project-scoped MCP registration for Claude Code (uses `uv run --project .`)
├── .claude/skills/coach/SKILL.md   # Claude Code skill: "/coach" → daily check-in workflow using the MCP tools
├── mcpb/
│   └── manifest.json           # Claude Desktop one-click bundle (.mcpb) definition, built by `garmin-coach build-mcpb`
├── docs/
│   ├── garmin-personal-api-openapi.json   # source of truth for fields (already provided)
│   ├── PLAN.md                            # this file
│   └── DECISIONS.md                       # ADR-style log the implementer appends to
├── src/garmin_coach/
│   ├── __init__.py
│   ├── cli.py                  # typer app
│   ├── install.py              # `garmin-coach install claude-desktop|claude-code`: writes the client config with absolute paths
│   ├── settings.py             # paths (platformdirs), config loading, env overrides
│   ├── secrets.py              # key resolution (official resolve_api_key) + redaction helpers
│   ├── garmin/
│   │   ├── client.py           # thin wrapper around official client: retries, backoff, budget, chunking
│   │   ├── endpoints.py        # the 26 endpoints as enum/dataclass with caps and tiers (from §3)
│   │   └── chunking.py         # date range splitter honoring caps
│   ├── store/
│   │   ├── db.py               # sqlite connection, migrations, WAL
│   │   ├── schema.sql          # tables in §5.2
│   │   └── repo.py             # upsert/read functions
│   ├── sync/
│   │   ├── freshness.py        # the freshness gate: ensure_fresh(tiers, max_age) → sync or background refresh (§6.5)
│   │   ├── startup.py          # startup_check(): empty cache → backfill job; stale tiers → refresh job (§6.6)
│   │   ├── jobs.py             # background job runner: one worker thread, `job` table, progress, cancel (§6.7)
│   │   ├── planner.py          # decides which calls are due for the requested tiers (TTLs, sync_state)
│   │   ├── runner.py           # executes plan, writes raw + normalized; lock file; budgets; progress callback
│   │   └── normalize/          # one module per domain: activities.py, daily.py, sleep.py, hrv.py, weight.py, profile.py
│   ├── metrics/
│   │   ├── load.py             # daily load, ATL/CTL/TSB, ACWR, monotony/strain
│   │   ├── recovery.py         # HRV vs baseline, RHR delta, sleep, Body Battery → readiness heuristic
│   │   ├── fitness.py          # VO2max trend, threshold HR/pace, FTP, power curve bests
│   │   └── events.py           # days-to-event, phase (base/build/peak/taper), weekly volume targets
│   ├── context/
│   │   ├── builder.py          # assembles ContextPack (pydantic) from store + metrics + yaml inputs
│   │   ├── render_md.py        # Jinja2 templates → markdown packs
│   │   └── templates/*.md.j2
│   └── mcp/
│       ├── server.py           # FastMCP app: tools + resources, stdio default, --http localhost optional
│       └── tools.py            # every tool calls ensure_fresh() first, then reads SQLite
└── tests/
    ├── fixtures/               # sanitized JSON samples per endpoint (no PII beyond the owner's own choice)
    ├── test_chunking.py
    ├── test_freshness.py       # TTL logic, lock, stale-but-served behaviour, background refresh
    ├── test_startup_jobs.py    # empty cache → backfill job; stale → refresh job; progress rows; resume; cancel
    ├── test_normalize_*.py
    ├── test_metrics_*.py
    ├── test_context.py
    └── test_mcp_tools.py
```

---

## 5. Data design

### 5.1 Locations (via `platformdirs`)
| Thing | Windows | macOS |
|---|---|---|
| API key file | `%USERPROFILE%\.config\garmin\connect-personal-api-key` | `~/.config/garmin/connect-personal-api-key` (chmod 600) |
| Token cache (official client) | `%USERPROFILE%\.config\garmin\connect-personal-api-bearer-token.json` | same path |
| App data dir | `%LOCALAPPDATA%\garmin-coach\` | `~/Library/Application Support/garmin-coach/` |
| SQLite DB | `<app data>/garmin.db` | same |
| FIT archive (optional) | `<app data>/fit/<activityId>.zip` | same |
| Context output | `<app data>/context/` | same |
| User config (`athlete.yaml`, `events.yaml`, `settings.yaml`) and knowledge (`history.md`, `coach-notes.md`) | `<app data>/config/` — the owner may keep this folder in a **private** git repo or copy it between machines by hand; it is the only thing that must be kept in sync | same |
| Logs | `<app data>/logs/garmin-coach.log` (rotating, redacted) | same |

`GARMIN_COACH_HOME` env var overrides the app data dir. **None of these paths are inside OneDrive/iCloud/Desktop/Documents.**
Windows note: "Known Folder Move" syncs Desktop/Documents/Pictures; `%LOCALAPPDATA%` and `%USERPROFILE%\.config` are not synced.

### 5.2 SQLite schema (minimum)
```sql
-- raw layer: every successful response, verbatim. Never deleted. Lets us re-normalize without re-polling.
CREATE TABLE raw_payload (
  id INTEGER PRIMARY KEY,
  endpoint TEXT NOT NULL,            -- e.g. 'sleep.daily'
  key TEXT NOT NULL,                 -- natural key: date, date range, or activityId
  fetched_at TEXT NOT NULL,          -- ISO8601 UTC
  content_hash TEXT NOT NULL,        -- sha256 of body; skip write if unchanged
  body TEXT NOT NULL,                -- JSON text
  UNIQUE(endpoint, key, content_hash)
);
CREATE TABLE sync_state (            -- when each (endpoint,key) was last fetched; drives the planner and the freshness gate
  endpoint TEXT NOT NULL, key TEXT NOT NULL, last_fetched_at TEXT NOT NULL, last_status INTEGER,
  PRIMARY KEY(endpoint, key)
);
CREATE TABLE tier_state (            -- one row per tier: makes the freshness check a single cheap read
  tier TEXT PRIMARY KEY,             -- 'A' | 'B' | 'C' | 'D' | 'E' | 'H' (H = history/backfill, completed once)
  last_completed_at TEXT,            -- ISO8601 UTC of the last *successful* refresh of the whole tier
  last_attempt_at TEXT, last_error TEXT, cooldown_until TEXT   -- circuit breaker state
);
CREATE TABLE job (                   -- background/foreground sync jobs; the thing `status`, `jobs` and get_sync_status read
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,                -- 'backfill' | 'refresh'
  trigger TEXT NOT NULL,             -- 'startup' | 'mcp' | 'cli' | 'gate'
  tiers TEXT NOT NULL,               -- e.g. 'A,B' or 'H'
  status TEXT NOT NULL,              -- 'queued' | 'running' | 'done' | 'partial' | 'failed' | 'cancelled'
  pid INTEGER, host TEXT,            -- which process owns it (to detect orphans after a crash)
  queued_at TEXT, started_at TEXT, finished_at TEXT, heartbeat_at TEXT,   -- heartbeat updated every call
  calls_planned INTEGER, calls_done INTEGER, calls_failed INTEGER,
  current_step TEXT,                 -- human-readable: "sleep summaries 2026-03-01..2026-03-31 (7/12)"
  error TEXT, notes TEXT
);
CREATE TABLE sync_run (id INTEGER PRIMARY KEY, job_id INTEGER, started_at TEXT, finished_at TEXT, trigger TEXT,
  tiers TEXT, calls INTEGER, errors INTEGER, notes TEXT);

-- normalized layer (one row per day / per activity). All upserts keyed by date or activity_id.
CREATE TABLE activity (
  activity_id INTEGER PRIMARY KEY, start_local TEXT, start_gmt TEXT, type_key TEXT, name TEXT,
  distance_m REAL, duration_s REAL, moving_s REAL, elev_gain_m REAL, avg_hr INTEGER, max_hr INTEGER,
  avg_power REAL, norm_power REAL, aerobic_te REAL, anaerobic_te REAL, te_label TEXT,
  training_load REAL, vo2max REAL, lthr INTEGER, ftp INTEGER, avg_speed_mps REAL,
  calories REAL, body_battery_diff INTEGER, has_detail INTEGER DEFAULT 0, has_fit INTEGER DEFAULT 0,
  updated_at TEXT
);
CREATE TABLE daily (
  date TEXT PRIMARY KEY,
  steps INTEGER, distance_m REAL, active_kcal REAL, total_kcal REAL,
  rhr INTEGER, rhr_7d_avg INTEGER, hr_min INTEGER, hr_max INTEGER,
  stress_avg INTEGER, stress_high_s INTEGER, stress_med_s INTEGER, stress_low_s INTEGER, stress_rest_s INTEGER,
  bb_high INTEGER, bb_low INTEGER, bb_at_wake INTEGER, bb_charged INTEGER, bb_drained INTEGER,
  intensity_min_moderate INTEGER, intensity_min_vigorous INTEGER,
  spo2_avg REAL, respiration_avg REAL, hydration_ml REAL, updated_at TEXT
);
CREATE TABLE sleep (
  date TEXT PRIMARY KEY, start_local TEXT, end_local TEXT, total_s INTEGER, deep_s INTEGER, light_s INTEGER, rem_s INTEGER,
  awake_s INTEGER, awake_count INTEGER, score INTEGER, score_quality TEXT, avg_hr INTEGER, avg_spo2 REAL, lowest_spo2 REAL,
  avg_respiration REAL, avg_stress REAL, nap_s INTEGER, sleep_need_min INTEGER, feedback TEXT, updated_at TEXT
);
CREATE TABLE hrv (
  date TEXT PRIMARY KEY, last_night_avg INTEGER, last_night_5min_high INTEGER, weekly_avg INTEGER,
  baseline_low INTEGER, baseline_upper INTEGER, baseline_balanced_low INTEGER, baseline_balanced_upper INTEGER,
  status TEXT, feedback TEXT, updated_at TEXT
);
CREATE TABLE weight (
  ts_gmt TEXT PRIMARY KEY, date TEXT, weight_g REAL, bmi REAL, body_fat_pct REAL, muscle_mass_g REAL, body_water_pct REAL, source TEXT
);
CREATE TABLE profile_snapshot (
  taken_at TEXT PRIMARY KEY, kind TEXT, body TEXT   -- 'hr_zones' | 'settings' | 'fitness_age' | 'weight_goal'
);
-- derived layer, recomputed by `context build`; cheap to drop and rebuild.
CREATE TABLE metrics_daily (
  date TEXT PRIMARY KEY, load REAL, atl REAL, ctl REAL, tsb REAL, acwr REAL, monotony REAL, strain REAL,
  readiness_score INTEGER, readiness_reasons TEXT, vo2max_run REAL, vo2max_bike REAL
);
```
Exact HRV baseline field names must be taken from `HrvSummaryDTO.baseline` in the OpenAPI file during Phase 1; the
columns above are a placeholder for "low/upper/balanced" style bounds.

### 5.3 Normalization rules
- Normalize from `raw_payload` in a separate step (`sync` fetches; `normalize` transforms). Re-running `normalize --all`
  must rebuild the normalized tables from raw without network access.
- Unknown/missing fields → `NULL`, never crash. Pydantic models with `extra="allow"` and all fields optional.
- Units: keep Garmin's native units (meters, seconds, grams, bpm, ms). Convert only in rendering.
- Time: store both GMT and local timestamps when provided. "Day" = Garmin `calendarDate`.
- Activity detail (`/activity-service/activity/{id}`) and workouts are stored raw; only summary fields go to `activity`.
- Intraday arrays (HR samples, Body Battery curve, stress curve, HRV readings) stay in raw only; expose via MCP on demand.

---

## 6. Sync engine

### 6.1 Commands
```
garmin-coach doctor            # checks key file perms, token exchange works (calls profile settings), db ok; tells you if the cache is empty
garmin-coach backfill [--days 365] [--fit] [--background]   # history load; idempotent; resumable. Foreground with a progress bar by default.
                                                            #   Auto-started in the background by the startup check when the cache is empty (§6.6).
garmin-coach sync [--tier A,B,...] [--force] [--background] # force a refresh now: plan → fetch → normalize → context build.
                                                            #   Without --force it applies the same TTLs as the freshness gate, so it is a no-op when fresh.
garmin-coach normalize --all   # rebuild normalized tables from raw
garmin-coach context build [--offline]             # recompute metrics_daily and render context packs (freshness gate first unless --offline)
garmin-coach context show [daily|weekly|events] [--offline] [--max-age 60]   # print a pack (for copy/paste into ChatGPT)
garmin-coach context export --zip [--offline]      # bundle packs for a ChatGPT Project
garmin-coach mcp [--http --port 8765]              # start MCP server (stdio default); runs the startup check first
garmin-coach status [--watch] [--json]             # cache age per tier, backfill done?, running job with progress, cooldowns, key age
garmin-coach jobs [list|show <id>|cancel <id>|prune]   # job history and control (§6.7)
```
There is **no** `schedule` or `daemon` command. Garmin is contacted only while a `garmin-coach` process is alive and only
when the startup check, the freshness gate, or an explicit `sync`/`backfill` decides it is needed.

### 6.2 Planner logic
- Input: the set of tiers requested by the caller (freshness gate or CLI) and `max_age` per tier. Load `tier_state` /
  `sync_state`; a tier is due when `now - last_completed_at > TTL` (or `--force`). Within a due tier, individual
  `(endpoint, key)` slices that are already fresh (e.g. yesterday's summary fetched 20 min ago) are skipped.
- Activity discovery: `search_activities(start=0, limit=20, sortBy="startLocal", sortOrder="desc")`. Any `activityId` not in `activity`, or whose summary hash changed (edited name, corrected elevation), is upserted from the list row; detail + workouts are fetched for activities newer than 28 days (`has_detail=1`). Older activities get detail **lazily** on the first `get_activity` call. Stop paging when a full page contains only known, unchanged IDs. During backfill, page with `limit=100` and `startDate`.
- Always compute the list of calls first, then execute; the plan size becomes `job.calls_planned`. Hard caps: 30 calls per synchronous on-demand refresh, 60 per background refresh, 400 per backfill job (a job that hits the cap ends as `partial`; the next startup check or `backfill` resumes it from `sync_state`).
- After a successful run: normalize the touched domains, recompute `metrics_daily`, re-render the packs, and update `tier_state.last_completed_at`. A tier is marked complete only if **all** its planned calls succeeded or were tombstoned (404 = no data); otherwise it stays stale and will be retried on the next request after the cooldown.

### 6.3 Client wrapper (`garmin/client.py`)
- Wrap `GarminPersonalApiClient` + `PersonalApiAuthenticator` from the official package.
- On `401`: `obtain_token(force_refresh=True)` once, update bearer, retry once. Then fail the run.
- On `429` or `5xx`: exponential backoff 2 s → 4 → 8 → 16 → 32 (jitter), max 5 tries, honour `Retry-After`.
- On `400` for a range call: halve the window and retry (learn the cap; persist the learned cap in `sync_state` notes).
- On `404` for a date-based single-day call: treat as "no data for that day" (store a tombstone in `sync_state`, do not retry for 24 h).
- Minimum 500 ms between calls (configurable). All calls go through one function that enforces the budget.
- Redaction filter on the logging root logger: masks `GCDC_…`, `Bearer …`, and JWT-looking strings.

### 6.4 Circuit breaker
After 3 consecutive `429` responses in one run, abort the run and set `tier_state.cooldown_until = now + 2 h` for the
affected tiers. While a cooldown is active the freshness gate **does not call Garmin** and serves cached data flagged as
stale. Any network/auth failure sets a shorter cooldown (10 min) so a broken connection does not add latency to every
chat message. `status` shows it. This is what protects the owner's key from being throttled or flagged while rate
limits are unknown.

### 6.5 Freshness gate (on-demand refresh) — the core of the update model
`sync/freshness.py` exposes one function used by every consumer:

```python
def ensure_fresh(tiers: set[Tier], max_age: dict[Tier, timedelta] | None = None, *, trigger: str) -> Freshness
```

Behaviour, in order:
1. **Cheap check.** Read `tier_state` (one query). For each requested tier compare `now - last_completed_at` with
   `max_age[tier]` (default = TTL from `settings.yaml`; `0` = force). If nothing is stale → return immediately with
   `refreshed_now=False`. This path must cost < 5 ms; it runs on every MCP call.
2. **Cooldown check.** Tiers in cooldown (§6.4) are removed from the refresh set and reported as `stale_reason="cooldown"`.
3. **Lock.** Acquire the cross-process file lock in the app data dir (`refresh.lock`, non-blocking). If another process
   holds it (e.g. Claude Desktop and Claude Code both have a server running, or a CLI sync is in progress), **wait up to
   `sync.lock_wait_seconds` (default 15)** for it to finish, then re-read `tier_state` (double-checked: the other process
   probably refreshed what we need) and continue with whatever is still stale.
4. **Synchronous tiers (A, B).** Run the planner + runner inline for stale A/B tiers with the synchronous budget
   (30 calls, `sync.sync_timeout_seconds` default 25 s wall clock). Typical cost: 5–10 calls, 5–10 s. If the budget is
   exhausted, keep what was fetched, leave the tier stale, and continue — the tool must still answer.
5. **Background tiers (C, D, E).** If stale, start **one** daemon thread (per process, guarded by an in-process flag)
   that runs the planner + runner for them with the background budget, then normalizes and re-renders. The current
   request is answered from the cache immediately with `background_refresh_started=True`; the next request sees the
   fresh data. In the CLI (`context show/export` without `--offline`) these tiers are refreshed synchronously instead,
   because the process would otherwise exit before the thread finishes.
6. **Never fail the consumer because the refresh failed.** Any exception in steps 3–5 is logged (redacted), recorded in
   `tier_state.last_error`, and the request is served from the cache with `stale=True, stale_reason=...`.
7. Return a `Freshness` object that every MCP tool/resource embeds in its response and every pack prints in its header:

```json
{"data_through": "2026-10-28", "tiers": {"A": {"age_minutes": 3, "refreshed_now": true}, "B": {"age_minutes": 3, "refreshed_now": true},
 "C": {"age_minutes": 540, "background_refresh_started": true}}, "stale": false, "stale_reason": null, "circuit_open": false}
```

Which tiers each consumer requests (so a question about HR zones does not trigger an activity refresh):

| Consumer | Tiers |
|---|---|
| `get_daily_brief`, `daily_checkin` prompt, `context show daily`, resources `daily-brief` | A, B (+ C, D in background) |
| `list_activities`, `get_activity`, `get_training_load`, `activities-recent` | A |
| `get_sleep`, `get_hrv`, `get_body_battery`, `get_heart_rate` for **today/yesterday** | B |
| same tools for older dates, `get_range_summary`, `last-7d`, `last-28d`, `weekly_review` | C (synchronous if the requested range is not in the cache at all, otherwise background) |
| `get_weight`, `get_hr_zones`, `get_athlete_profile`, `athlete` resource | E (+ C for weight) |
| `get_events`, `events` resource | none (YAML only) |
| `get_sync_status` | none (reports ages, never refreshes) |
| `refresh(tiers?, force?, scope?)` tool | enqueues a job for the given tiers (or `scope="history"` → backfill); `force=True` sets `max_age=0`; rate-limited to once per 5 min per tier even when forced; returns the job id |
| `get_activity(id)` for an activity with `has_detail=0` | lazy fetch of endpoints 2–3 for that single id (2 calls), synchronous |
| `context export --zip` | A, B, C, D, E synchronously (it is a deliberate "give me everything" action) |

Operational notes:
- A stdio MCP server is spawned by Claude Desktop/Code at startup and stays alive for the session. Thanks to the startup
  check (§6.6) the cache is usually already warm when the first question arrives; otherwise the first question pays the
  A+B refresh once (a few seconds); later questions are instant until the TTL expires again.
- If a background job is already refreshing the tiers a tool needs, the gate waits up to `lock_wait_seconds` for it and
  then serves whatever is there, with `freshness.job_running=true` so the model can say "history is still loading".
- Garmin's side still depends on the watch having synced to Garmin Connect. If the user asks before the watch synced,
  the brief says "no data yet for today" and the next question after the TTL will pick it up. `refresh(force=True)` is
  the escape hatch ("I just synced my watch, reload").
- The lock file plus `tier_state`/`job` are the only coordination between processes; SQLite in WAL mode handles concurrent
  readers while one writer refreshes.

### 6.6 Startup check (runs once per process start)
`sync/startup.py: startup_check(trigger) -> Job | None` is called by `garmin-coach mcp` right after the DB is opened and
before the server starts accepting requests (non-blocking: it only *enqueues*), and by every CLI command except
`doctor`, `status`, `jobs`, `normalize` and anything with `--offline`.

Decision, in order:
1. **Orphan cleanup.** Any `job` row with `status='running'` whose `heartbeat_at` is older than 2 min (or whose `pid` is
   dead on this host) is marked `failed` with `error='orphaned'`. Its progress is not lost: `sync_state` already records
   what was fetched.
2. **Cache completely empty** (no `tier_state` row for `H` with `last_completed_at`, i.e. backfill never finished) and no
   `backfill` job currently running on any process → enqueue `Job(kind='backfill', tiers='H', trigger='startup')`.
   A `partial` or `failed` previous backfill is resumed, not restarted (same job kind, new row, planner skips fetched slices).
   This is the "the command starts by itself when the cache is empty" rule. It also covers the second machine: clone,
   put the key in place, start Claude Desktop, and the history loads on its own.
3. **Cache present but stale.** Compute the set of tiers whose age exceeds the TTL. If non-empty and no job is running →
   enqueue `Job(kind='refresh', tiers=<stale tiers>, trigger='startup')`. A/B are included so the first question is fast.
4. **Cooldown active** (§6.4) → enqueue nothing; `status` shows the cooldown.
5. Log the decision (redacted) at INFO level. The MCP server also exposes it through `get_sync_status` and the
   `garmin-coach://status` resource.

While a startup backfill is running, every tool answer carries `freshness.history_loaded=false` and the packs print
`⚠ Historical data still loading (job #12, 48/160 calls)` at the top, so the assistant can caveat trend-based advice.

### 6.7 Background jobs and monitoring
`sync/jobs.py` is a tiny in-process job runner:
- **One worker thread per process**, started lazily on first enqueue, daemon=True. Jobs run strictly one at a time
  (FIFO). The cross-process file lock (§6.5 step 3) guarantees that two processes never fetch simultaneously; a job that
  cannot get the lock within `lock_wait_seconds` is re-queued once, then marked `failed` with `error='locked'`.
- The runner updates the `job` row **after every API call**: `calls_done`, `calls_failed`, `current_step`,
  `heartbeat_at`. This is what makes progress visible from other processes (the CLI `status --watch` while Claude
  Desktop's server is backfilling, for example).
- Terminal states: `done` (all planned calls ok or tombstoned), `partial` (budget/timeout hit, resumable), `failed`
  (unrecoverable error or circuit breaker), `cancelled` (via `jobs cancel`/`cancel_job`, checked between calls).
- On `done` or `partial` the runner normalizes the touched domains, recomputes metrics, re-renders packs and updates
  `tier_state`.
- `jobs prune` keeps the last 50 rows.

How to monitor:
| Where | What you see |
|---|---|
| `garmin-coach status` | per tier: age, TTL, fresh/stale; `history: loaded 2026-10-28 09:12` or `loading 48/160`; running job line with ETA (`remaining calls × min gap`); cooldowns; key age; last error (redacted) |
| `garmin-coach status --watch` | same, refreshed every 2 s until the job ends (Ctrl-C to stop watching, job keeps running) |
| `garmin-coach status --json` | machine-readable, same object as `get_sync_status` |
| `garmin-coach jobs list|show <id>` | job history, per-job counts, duration, error |
| MCP tool `get_sync_status()` and resource `garmin-coach://status` | same object; lets the model answer "is my data up to date?" |
| `backfill` / `sync` in the foreground | `rich` progress bar; exit code 0 on `done`, 2 on `partial`, 1 on `failed` |
| Log file `<app data>/logs/garmin-coach.log` | one line per call (endpoint, key, status, ms), redacted |

`get_sync_status` / `status --json` shape:
```json
{"history_loaded": false, "tiers": {"A": {"age_minutes": 2, "ttl_minutes": 60, "stale": false}, "...": {}},
 "job": {"id": 12, "kind": "backfill", "status": "running", "trigger": "startup", "calls_done": 48, "calls_planned": 160,
         "current_step": "sleep summaries 2026-03-01..2026-03-31 (7/12)", "started_at": "...", "eta_seconds": 70},
 "last_jobs": [{"id": 11, "kind": "refresh", "status": "done", "calls_done": 7, "finished_at": "..."}],
 "cooldown_until": null, "last_error": null, "key_age_days": 3}
```

How to force:
- CLI: `garmin-coach sync --force [--tier A,B]` (foreground, progress bar) or `--background` (enqueues and returns; watch with `status --watch`).
- CLI: `garmin-coach backfill` to (re)load history explicitly, e.g. with `--days 730`.
- MCP: `refresh(tiers?, force?)` and `refresh(scope="history")` both enqueue a job and return its id immediately; the model
  can then call `get_sync_status` to report progress. Still rate-limited to once per 5 min per tier when `force=true`.

---

## 7. Metrics (derived locally, because Garmin's Training Status is not exposed)

All formulas must be documented in `docs/METRICS.md` with the caveat that they approximate, not reproduce, Garmin's proprietary metrics.

| Metric | Definition | Source fields |
|---|---|---|
| Daily load | Σ `activityTrainingLoad` of the day's activities; fallback Banister TRIMP from duration × HR-reserve fraction when load is null (uses HR zones + RHR) | `activity.training_load`, `avg_hr`, `duration_s`, `hr_zones`, `daily.rhr` |
| ATL / CTL / TSB | EWMA with τ = 7 d (ATL) and 42 d (CTL); TSB = CTL − ATL (yesterday's) | `metrics_daily.load` |
| ACWR | 7-day sum / (28-day sum / 4); flag > 1.5 and < 0.8 | load |
| Monotony / strain | Foster: mean/std of last 7 days; strain = weekly load × monotony | load |
| HRV status | Garmin's own `status` + `lastNightAvg` vs `baseline` bounds; 7-day trend slope | `hrv` |
| RHR delta | today vs `rhr_7d_avg`; flag ≥ +5 bpm | `daily` |
| Sleep | score, duration vs `sleep_need_min`, deep+REM share, 7-day average | `sleep` |
| Body Battery | at wake, lowest, charged overnight | `daily` |
| Readiness (heuristic) | 0–100 rule-based score: HRV status (30), sleep score (25), BB at wake (20), RHR delta (15), TSB (10). Return reasons as text. Label clearly "heuristic". | above |
| Fitness | latest `vo2max` per sport (run/bike), 90-day trend; LTHR/FTP from latest activities and zones; best `maxAvgPower_*` over 90 days | `activity`, `profile_snapshot` |
| Event readiness | days to each event; phase by weeks-out (≥12 base, 12–4 build, 4–2 peak, <2 taper); weekly volume/ load last 4 weeks vs plan targets in `events.yaml` | `events.yaml`, load |

---

## 8. Context packs (what the agent actually reads)

Rendered by `context build` into `<app data>/context/`; also served by MCP as resources. Keep each pack small
(target ≤ 2–3k tokens for daily, ≤ 6k for weekly) so they fit in any chat.

1. **`athlete.md`** — from `athlete.yaml` (manual): age, sex, height, weight (auto from DB), sports, max HR, LTHR, FTP,
   HR zones (auto from API), injuries/constraints, weekly availability, coaching preferences, units, language of advice;
   followed verbatim by **`history.md`** (athletic history before Garmin, past races and results, injuries, what has
   worked and what has not) and **`coach-notes.md`** (coaching rules and agreements accumulated so far). These two files
   are the knowledge transferred from the owner's previous ChatGPT/Copilot conversations (§11.2) and are the
   single source of truth for "who is this athlete" across every Claude surface.
2. **`events.md`** — from `events.yaml` (manual): upcoming events with date, sport, distance/elevation, goal time, priority
   A/B/C; computed: days-to-go, phase, suggested focus.
3. **`daily-brief.md`** — *today*: last night's sleep (score, duration, stages), HRV (last night vs baseline, status),
   RHR and delta, Body Battery at wake, stress yesterday, yesterday's activities (type, duration, load, TE, avg HR/power,
   pace), ATL/CTL/TSB/ACWR, readiness heuristic with reasons, one-line "flags" (e.g. "HRV below baseline 3 days", "ACWR 1.6").
4. **`last-7d.md`** and **`last-28d.md`** — tables: per-day load/sleep/HRV/RHR/BB; per-week totals by sport (time, km, load,
   elevation); zone distribution; VO2max/threshold trend; weight trend.
5. **`activities-recent.md`** — last 10 activities, one row each, with the key numbers and split highlights.
6. **`coach-instructions.md`** — a short, stable preamble (≤ 400 tokens) the owner pastes once as the instructions of a
   Claude Project (or as `CLAUDE.md` in Claude Code): *always start by calling `get_daily_brief` and `get_athlete_profile`;
   use `get_events` for planning; readiness is heuristic; when `freshness.stale` or `history_loaded=false` say so; answer
   in `advice_language`*. It deliberately contains **no personal data**: everything personal is served by the MCP tools,
   so the instructions never go out of date and the same text works in every Claude client.
7. **`context.json`** — the same content as structured JSON (the `ContextPack` pydantic model) for programmatic use.

Every pack starts with `Generated: <local datetime>`, `Data through: <date>`, `Cache age: A 3 min · B 3 min · C 9 h`,
and, when applicable, `⚠ Served stale: <reason>` so the assistant can tell the owner when numbers may be outdated.

---

## 9. MCP server (Claude Desktop / Claude Code)

- Framework: `mcp` Python SDK (`FastMCP`). Transport: stdio by default. Optional `--http` → streamable HTTP bound to `127.0.0.1` only,
  with a random bearer token printed at start (for a local reverse-proxy/tunnel scenario, Phase 6). Never `0.0.0.0`.
- **Every tool and resource calls `ensure_fresh()` first** (§6.5) with the tiers listed in the table there, then reads
  SQLite / the rendered packs. The MCP server is therefore the thing that keeps the cache alive: no scheduler is involved.
  Garmin is called only through the freshness gate, never directly from a tool.
- Every tool accepts an optional `max_age_minutes: int | None` (None = TTL defaults, `0` = force refresh of the tiers
  it needs) and every response embeds the `freshness` object so the model can say "data is 3 minutes old" or "served
  from a 9-hour-old cache because Garmin is unreachable".
- Tools (all return JSON; keep payloads small, paginate):
  - `get_daily_brief(date?, max_age_minutes?)`, `get_context_pack(name, max_age_minutes?)`, `get_events()`
  - `get_athlete_profile(sections?: ["profile","zones","history","coach_notes"])` → `athlete.yaml` + auto fields + `history.md` + `coach-notes.md` (all by default)
  - `append_coach_note(text)` → the **only** write tool; appends a dated line to `coach-notes.md` so the coach can record an agreement ("from now on long run on Saturdays") that persists across chats. Local file only, never touches Garmin.
  - `get_range_summary(start, end, granularity=day|week, max_age_minutes?)`
  - `list_activities(start?, end?, type?, limit=20, max_age_minutes?)`, `get_activity(activity_id, include_splits=false)`
  - `get_training_load(start, end, max_age_minutes?)` → load/ATL/CTL/TSB/ACWR series
  - `get_sleep(date, max_age_minutes?)`, `get_hrv(date, …)`, `get_body_battery(date, include_curve=false, …)`, `get_heart_rate(date, include_samples=false, …)`
  - `get_weight(start, end, …)`, `get_hr_zones()`
  - `get_sync_status()` → the §6.7 object: history loaded?, cache age per tier, running job with progress and ETA, last jobs, cooldowns, key age; **never refreshes**
  - `refresh(tiers?: list, force?: bool, scope?: "tiers"|"history")` → explicit reload ("I just synced my watch") or history backfill; enqueues a background job and returns `{job_id}`; rate-limited to once per 5 min per tier
  - `cancel_job(job_id)`
- On server start the §6.6 startup check runs **before** the first request is served; it never blocks the server, it only enqueues.
- Resources: `garmin-coach://context/daily-brief`, `.../last-7d`, `.../last-28d`, `.../athlete`, `.../events`,
  `garmin-coach://knowledge/history`, `garmin-coach://knowledge/coach-notes`, `garmin-coach://status`.
- Prompts (these are the "skills" the owner will invoke from Claude): `daily_checkin` (brief + profile + events →
  today's session), `weekly_review` (last-7d + last-28d → what went well, what to change next week), `plan_week`
  (events + load + availability → next 7 days), `race_readiness(event)` (phase, taper, predicted form), `explain_metric(name)`.
  Each prompt embeds the needed packs so the model does not have to guess which tools to call.

### 9.1 Quick integration with Claude (the primary and only planned consumer)
Target: from a fresh clone to "Claude answers the daily check-in" in **under 10 minutes**, on either machine.

| Client | How | Done by |
|---|---|---|
| Claude Desktop | `garmin-coach install claude-desktop` writes the `garmin-coach` entry into `claude_desktop_config.json` (Windows `%APPDATA%\Claude\`, macOS `~/Library/Application Support/Claude/`) with absolute paths to `uv` and the repo; `--dry-run` prints the JSON instead. Restart Claude Desktop. | Phase 3 |
| Claude Desktop, one click | `garmin-coach build-mcpb` produces `garmin-coach.mcpb` (Desktop Extension bundle: `manifest.json` + the `uv`-based launcher); double-click installs it, with the app-data dir as a user-configurable setting. Nice-to-have for the Mac. | Phase 4 |
| Claude Code, this repo | `.mcp.json` committed in the repo → the server is available automatically when Claude Code opens the repo folder. | Phase 3 |
| Claude Code, anywhere | `garmin-coach install claude-code --scope user` runs `claude mcp add --scope user garmin-coach -- <abs uv> run --project <abs repo> garmin-coach mcp`. | Phase 3 |
| Claude Code skill | `.claude/skills/coach/SKILL.md`: `/coach` runs the `daily_checkin` flow; `/coach week` the weekly review. | Phase 3 |
| Claude Project (claude.ai / Desktop) | Create a project "Garmin coach", paste `coach-instructions.md` as instructions, nothing else. All personal knowledge arrives via MCP. | Owner, 2 min |
| `claude.ai` web | Cannot reach a local stdio server; use Claude Desktop (same account, same Projects) instead. | — |

Rules for the implementer so the integration stays "quick":
- Absolute paths only in generated configs; escape backslashes on Windows; never put secrets in `args` or `env`.
- The server must start in < 2 s and must not fail to start when the cache is empty, the key is missing, or Garmin is
  down: it starts, and the tools return a clear `error` field ("API key not found at …, run `garmin-coach doctor`").
  A server that crashes at boot is invisible in Claude Desktop and very hard to debug.
- Tool and prompt descriptions are written for the model: say what the data is, its units, and when to call it.
- `garmin-coach doctor` ends by printing the exact next command for the client it detects installed.

---

## 10. Other assistants (decided 2026-10-28: Claude only)

**Microsoft 365 Copilot is out of scope**: publishing an agent there needs the tenant administrator's approval and would
store personal health data in an organisation's tenant. The knowledge accumulated in earlier chat conversations is
transferred once into the service (§11.2)
and Claude becomes the single coaching front-end through the local MCP server (§9.1).

Kept as low-cost fallbacks, not as goals:
- `garmin-coach context export --zip` still exists (it is just the rendered packs) in case the owner ever wants a second
  opinion from another assistant by uploading files. Any data an assistant reads is, by definition, sent to that vendor.
- Exposing the HTTP MCP endpoint through a tunnel for ChatGPT or Copilot Studio stays an optional Phase 6 item and is
  **not** to be built unless the owner explicitly asks.

---

## 11. Manual inputs the owner maintains

### 11.1 Structured (YAML)

`athlete.yaml`
```yaml
name: ""
birth_year: 19xx
sex: M|F
height_cm: 0
sports: [running, cycling]
max_hr: null          # null = take from Garmin zones
lthr: null
ftp: null
constraints: ["left knee: avoid >25 km long runs", "no training Tue evenings"]
availability: {mon: 60, tue: 0, wed: 90, thu: 60, fri: 0, sat: 180, sun: 120}   # minutes
advice_language: it
units: metric
```
`events.yaml`
```yaml
events:
  - name: "Half marathon X"
    date: 2027-03-14
    sport: running
    distance_km: 21.1
    elevation_m: 150
    goal: "sub 1:45"
    priority: A
```
`settings.yaml`
```yaml
sync:
  ttl_minutes: {A: 60, B: 60, C: 1440, D: 1440, E: 10080}   # staleness thresholds per tier (§3, §6.5)
  min_call_gap_ms: 500
  sync_budget_calls: 30          # max calls in a synchronous on-demand refresh
  sync_timeout_seconds: 25       # wall clock for a synchronous refresh; then serve what we have
  background_budget_calls: 60
  backfill_budget_calls: 400     # per backfill job; a job over budget ends 'partial' and resumes next start
  backfill_days: 365
  detail_days: 28                # activity detail/workouts fetched eagerly only for activities newer than this; older = lazy
  startup_check: true            # set false to disable auto backfill/refresh at process start
  lock_wait_seconds: 15
  heartbeat_stale_seconds: 120   # a 'running' job without heartbeat for this long is treated as orphaned
  cooldown_minutes: {rate_limit: 120, network_error: 10}
  download_fit: false
optional_endpoints: {nutrition: false, womens_health: false, hydration: true, spo2: true}
context: {daily_token_budget: 2500, weekly_token_budget: 6000, recent_activities: 10}
```

### 11.2 Narrative knowledge (Markdown) and the one-time knowledge transfer

Two Markdown files in `<app data>/config/`, served by `get_athlete_profile` and as MCP resources, and included in
`athlete.md`. Keep each under ~1 500 tokens; the implementer must warn in `doctor` when they grow past 3 000.

`history.md` — facts the Garmin data cannot tell:
```markdown
# Athletic history
## Background
- Running since 2018; cycling since 2022. Pre-Garmin PRs: 10 km 44:30 (2019), half 1:41 (2021).
## Injuries and limits
- 2024-03 left Achilles tendinopathy, 8 weeks off; now fine if long runs ≤ 25 km and no hills two days in a row.
## What worked / what did not
- Responds well to 2 quality sessions/week; 3 led to fatigue (2025 spring block).
## Past events
| Date | Event | Result | Notes |
```

`coach-notes.md` — rules and agreements, one dated line per entry, appended by the owner or by the `append_coach_note` tool:
```markdown
- 2026-10-28 Long run on Saturdays, recovery ride on Sundays.
- 2026-10-28 Prefer advice as: today's session, why, one thing to watch.
```

**Knowledge transfer from the old chats (owner task, Phase 0, ~30 min).** Paste this into the existing ChatGPT/Copilot
conversation and save the answer into the two files above and into `athlete.yaml` / `events.yaml`:

> You have been my training advisor in this conversation. Produce a hand-over document for a new coach, with no
> screenshots and no day-by-day data, structured exactly as: (1) athlete profile: age, sex, height, weight, sports,
> known max HR / LTHR / FTP, HR zones if you know them; (2) upcoming events with date, distance, goal, priority;
> (3) injuries and constraints with dates; (4) training history: typical weekly structure, what worked, what did not,
> with periods; (5) past events and results; (6) the rules and preferences we agreed on about how you advise me;
> (7) open questions you still had about me. Be factual, keep it under 1 500 words, use bullet points and tables.

The implementer ships `config/history.example.md` and `config/coach-notes.example.md` matching the structure above, and
`doctor` reports whether the real files exist.

---

## 12. Phased work plan with acceptance criteria

### Phase 0 — Security hygiene and scaffold (½ day)
1. In Garmin Connect Web (`connect.garmin.com/app/settings/personalApiToken`): delete the current key, create a new 1-year key.
2. Store it at `~/.config/garmin/connect-personal-api-key` (Windows: `%USERPROFILE%\.config\garmin\`). On macOS `chmod 600`. Delete `api-key.txt` from the OneDrive folder.
3. Create the repo **outside OneDrive** with `uv init --package garmin-coach`, Python ≥ 3.12, deps: `garmin-connect-personal-api-python`, `pydantic`, `typer`, `platformdirs`, `pyyaml`, `jinja2`, `mcp`; dev: `pytest`, `ruff`, `mypy`. Copy `docs/garmin-personal-api-openapi.json` and this plan into `docs/`.
4. `.gitignore` covers data, db, keys, context output, fit files. Pre-commit hook (or a test) that greps for `GCDC_` and JWT patterns.
5. `garmin-coach doctor` implemented: checks key file exists and (POSIX) permissions, exchanges token via the official authenticator, calls profile settings, checks config/knowledge files exist, prints OK/failure without secrets and the next command to run.
6. Owner task: run the knowledge-transfer prompt (§11.2) in the old ChatGPT/Copilot chat and fill `athlete.yaml`, `events.yaml`, `history.md`, `coach-notes.md`.

**Accept:** `doctor` passes on this Windows machine; `git grep GCDC_` returns nothing; repo path is not under OneDrive; the four config/knowledge files exist.

### Phase 1 — Sync + cache (1–2 days)
1. `endpoints.py` with the 26 endpoints, caps, tiers; `chunking.py` with tests (31/28/365-day windows, inclusive dates).
2. `client.py` wrapper with retries, backoff, 401 refresh, 400 shrink, 404 tombstone, circuit breaker, redaction logging.
3. `db.py` + `schema.sql` + migrations; `raw_payload` dedupe by hash.
4. `backfill` and `sync` with planner; `tier_state`; lock file; `status` (plain, `--watch`, `--json`); lazy activity detail (`detail_days`).
5. `freshness.py` per §6.5: TTL check, cooldowns, lock + double-check, synchronous A/B, background C/D/E, stale-but-served; `Freshness` model. Tests with a fake clock and a fake client (no network).
6. `jobs.py` + `job` table per §6.7: worker thread, per-call progress/heartbeat, terminal states, cancel, orphan detection; `jobs` CLI.
7. `startup.py` per §6.6: empty cache → backfill job, stale → refresh job, cooldown → nothing; wired into `mcp` and the CLI commands.
8. Normalizers for activities, daily, sleep, hrv, weight, profile → tables in §5.2. Field names verified against the OpenAPI JSON. Record **sanitized** fixtures for tests (owner decides what to keep; strip `userProfilePk`, names, GPS boxes).

**Accept:** on an empty DB, `garmin-coach mcp` (or any CLI command) starts a backfill job by itself and `status --watch` in a second terminal shows calls progressing to `done` with ≤ 200 calls and no unhandled exceptions; killing the process mid-backfill and restarting resumes it (`partial` → new job → `done`) without re-fetching slices already in `raw_payload`; `sync` twice in a row makes **0** calls the second time (everything within TTL) and `sync --force --tier A,B` makes ≤ 10; `ensure_fresh({A,B})` on a fresh cache returns in < 5 ms; with a fake client that raises, it still returns cached data flagged stale; `normalize --all` offline rebuilds identical tables; tests green.

### Phase 2 — Metrics + context packs (1 day)
1. `metrics/*` per §7 with unit tests on synthetic series (known EWMA results, ACWR edge cases, missing days).
2. `context/builder.py` + Jinja templates per §8; `context build|show|export`.
3. `coach-instructions.md` template written in the owner's `advice_language`.

**Accept:** `context show daily --offline` renders in < 1 s from the cache, ≤ 2.5k tokens, with no empty sections when data exists and explicit "no data" lines when it does not; without `--offline` on a stale cache it refreshes A/B first and the header shows the new cache age.

### Phase 3 — MCP server (1 day)
1. FastMCP server with tools/resources/prompts per §9; every tool goes through `ensure_fresh()` with the tiers from the §6.5 table; input validation; payload size limits.
2. Startup check wired into server boot; `get_sync_status`, `refresh`, `cancel_job`, `get_athlete_profile`, `append_coach_note` tools; knowledge and status resources; the five prompts of §9.
2b. `garmin-coach install claude-desktop|claude-code`, `.mcp.json`, `.claude/skills/coach/SKILL.md` (§9.1). Server boots in < 2 s even with empty cache / missing key.
3. Register in Claude Code and Claude Desktop on this machine; run `daily_checkin` prompt end-to-end twice: once with a stale cache (observe the refresh in the log and the `refreshed_now=true` flag), once immediately after (0 calls). Then wipe the DB, restart Claude Desktop, and ask "is my data loaded?" while the auto-backfill runs: the answer must quote the job progress.
4. Tests call tools in-process against a fixture DB with a fake client: stale → refresh → served; fresh → no calls; client error → stale-but-served; two concurrent calls → one refresh; empty DB → startup enqueues backfill and tools report `history_loaded=false`.

**Accept:** from a fresh clone, `uv sync` + key file + `garmin-coach install claude-desktop` + restart Claude Desktop takes < 10 min and "How should I train today?" produces an answer grounded in today's brief, the athlete history and the events list without any manual paste; the second question within the TTL makes no Garmin call (`get_sync_status` confirms); `refresh(force=true)` reloads on demand and the model can report its progress; on a fresh install the history loads with no command typed by the owner; in Claude Code `/coach` works from the repo folder.

### Phase 4 — Second machine + hardening (½ day)
1. README section for the Mac: clone private repo, `uv sync`, key file, copy the `config/` knowledge folder, `doctor`, `install claude-desktop`. Nothing to schedule and nothing to backfill by hand: the first server start loads the history; `status --watch` shows it.
1b. `garmin-coach build-mcpb` → `garmin-coach.mcpb` for one-click install in Claude Desktop (§9.1).
2. Key-expiry reminder: `status`, `get_sync_status` and the daily brief warn when key age (stored at `doctor` time in the app data dir) > 350 days.
3. Latency check: measure the synchronous A+B refresh on both machines; if it regularly exceeds ~10 s, lower `limit` on the activity search or move the per-activity detail calls to the background tier.

**Accept:** both machines answer the daily check-in from Claude Desktop for 48 h with no manual sync; `status` shows refreshes only at times the owner actually asked something; no file under OneDrive/iCloud changed.

### Phase 5 — Optional depth
- FIT download/parse for new activities: laps, intervals, HR/power time-in-zone computed locally; `get_activity(include_laps=true)`.
- Nutrition / hydration / SpO2 opt-ins in context.
- `context export --zip` + `--clipboard` for the ChatGPT Project workflow.

### Phase 6 — Optional exposure / UI
- Localhost HTTP MCP + Cloudflare Tunnel + Access for ChatGPT custom connector (explicit owner opt-in only).
- Small local dashboard (Streamlit or a static HTML page generated from `context.json`) — matches the original folder name "custom-garmin-dashboard" but is not needed for the coaching goal.

---

## 13. Testing strategy
- **Offline by default.** All unit tests run against JSON fixtures and an in-memory SQLite DB. No network in CI.
- **One live smoke test** behind `GARMIN_COACH_LIVE=1`: token exchange + profile settings + one activity search with `limit=1`.
- Golden tests for context rendering (snapshot markdown from fixture DB).
- Property test for chunking: union of chunks == original range, every chunk ≤ cap, chunks contiguous and non-overlapping.
- Freshness tests with an injectable clock: exact-TTL boundary, `max_age=0` forces, cooldown suppresses, lock contention
  yields one refresh, synchronous budget exhaustion still returns, background thread updates `tier_state` on completion.
- Startup/job tests: empty DB enqueues exactly one backfill; a second process start while it runs enqueues nothing;
  a `running` row with a stale heartbeat is marked orphaned and resumed; budget hit → `partial` → resume fetches only the
  missing slices; `cancel` stops between calls; progress fields advance monotonically.
- Redaction test: feed the logger a fake `GCDC_…` string and assert the handler output masks it.

---

## 14. Open questions to resolve during implementation (log answers in `docs/DECISIONS.md`)
1. Exact shape of `HrvSummaryDTO.baseline` (check the OpenAPI file; map to columns).
2. Real caps of `/hrv-service/hrv/daily`, weight range/all, wellness activity range (start at 28/31 days; shrink on `400`).
3. Does `activityTrainingLoad` populate for all activity types (strength, swim)? If null, the TRIMP fallback is used.
4. Behaviour of single-day endpoints for days with no data: `404`, `204`, or `200` with nulls?
5. Does the ZIP from the download endpoint contain the full FIT (VO2max/recovery fields reported stripped on other tiers)?
6. Rate limits: record every `429` with timestamp to infer the real budget over the first weeks.
7. Time zone: `calendarDate` is the user's local day; confirm midnight behaviour for late-night activities and for travel.

---

## 15. Definition of done (for the whole project)
- No scheduler, no daemon: the cache refreshes itself on demand through the freshness gate and pre-warms at process start; an empty cache loads its history by itself; data is never older than the TTL when served (or is explicitly flagged stale); every sync is observable via `status --watch` and `get_sync_status`; data and secrets never under a cloud-synced folder.
- Claude Desktop/Code answer daily training questions from the local MCP server with zero manual data entry, knowing the athlete's full history and the agreed coaching rules without the owner re-explaining them in any chat.
- Setting up a new machine or a new Claude client takes under 10 minutes with `install`.
- The owner receives an in-tool warning before the Garmin key expires and can rotate it with `doctor` in under 5 minutes.
