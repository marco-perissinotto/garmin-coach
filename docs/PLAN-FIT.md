# garmin-coach — FIT ingestion plan (Phase 5a)

> **Audience:** the implementing AI model (Claude Opus) and the project owner. This is a *specification*, written
> after reading the code as of commit `6532b39` (schema v3, coaching state merged). It extends `docs/PLAN.md`; the
> hard rules of PLAN §0 apply unchanged. Where this document and PLAN.md disagree, this document wins for the FIT
> feature only. Log every deviation and every live finding in `docs/DECISIONS.md` (next free numbers: D-070+).
>
> **Goal in one sentence:** every activity that enters the cache also gets its original FIT file downloaded,
> reduced to a compact JSON summary (laps, intervals, HR/power time in zones, running/cycling dynamics, aerobic
> decoupling, a coarse intensity profile, start/end point) stored in SQLite, so the coach can analyse a session in
> depth; a separate command fills the FIT summaries of the past gradually, within Garmin's unpublished rate limits.

---

## 0. Scope

In scope:
1. Download + parse the FIT of **new** activities during the normal sync (tier A follow-ups and the history
   backfill), on by default.
2. A **gradual historical FIT backfill** (`garmin-coach fit backfill`, MCP `refresh(scope="fit")`) that chains
   budget-limited jobs and yields to on-demand refreshes.
3. Storage: ZIP kept on disk (raw layer), summary JSON in a new `activity_fit` table (normalized layer).
4. Exposure: `get_activity` returns the summary (laps and series on request), `list_activities` flags `has_fit`,
   `get_sync_status`/`status` report coverage, the `coach` skill knows how to use it.

Out of scope (later phases): full GPS tracks, per-record storage, FIT-derived aggregates in the context packs or in
`metrics_daily`, HRV (RR interval) analysis, pictures, Garmin "Training Status" emulation.

---

## 1. Facts verified on 2026-10-31

| Item | Value |
|---|---|
| Endpoint | PLAN §3 #4: `GET /download-service/files/activity/{activityId}` → `application/x-zip-compressed`. It is already in `garmin/endpoints.py` as `activity.download` (kind `activity`, domain `raw`) and is the only endpoint whose body is **bytes, not JSON**. |
| Official client | `GarminPersonalApiClient.download_activity(id) -> bytes` (ZIP, not extracted). Raises `GarminPersonalApiError("The original activity file response was empty")` on an empty 200 body; that exception is **not** an `HttpError`, so today it would escape `GarminClient.fetch` uncaught. `HttpError` is a subclass of `GarminPersonalApiError`. The request timeout is the client's `DEFAULT_TIMEOUT_SECONDS`. |
| ZIP content | One member `<activityId>_ACTIVITY.fit` for watch recordings. Activities uploaded as GPX/TCX or entered by hand may answer 404 or a ZIP without a `.fit` member (to verify live, §12). |
| FIT SDK | `garmin-fit-sdk` 21.218.0 on PyPI (official, Apache-2.0, Python ≥ 3.6, pure Python). `Stream.from_byte_array`, `Decoder(stream).read(...) -> (messages, errors)`; options `apply_scale_and_offset`, `convert_datetimes_to_dates`, `convert_types_to_strings`, `enable_crc_check`, `expand_sub_fields`, `expand_components`, `merge_heart_rates`, `mesg_listener`. `Decoder.is_fit(stream)`, `Decoder.check_integrity(stream)`. Messages come back keyed by `<name>_mesgs` (`Profile['messages'][num]['messages_key']`), e.g. `file_id_mesgs`, `session_mesgs`, `lap_mesgs`, `record_mesgs`, `time_in_zone_mesgs`, `device_info_mesgs`, `event_mesgs`, `sport_mesgs`, `split_summary_mesgs`, `hrv_mesgs`, `field_description_mesgs` (developer fields). An `Encoder` exists (used to build the test fixture). |
| Field names (Profile) | **session (18):** `sport, sub_sport, sport_profile_name, start_time, total_elapsed_time, total_timer_time, total_moving_time, total_distance, total_ascent, total_descent, total_calories, enhanced_avg_speed, enhanced_max_speed, avg_heart_rate, max_heart_rate, min_heart_rate, avg_cadence, max_cadence, avg_fractional_cadence, avg_power, max_power, normalized_power, threshold_power, training_stress_score, intensity_factor, total_work, left_right_balance, total_training_effect, total_anaerobic_training_effect, training_load_peak, avg_temperature, avg_respiration_rate, avg_spo2, avg_stress, sdrr_hrv, rmssd_hrv, workout_rpe, workout_feel, time_in_hr_zone, time_in_power_zone, time_in_speed_zone, time_in_cadence_zone, avg_vertical_oscillation, avg_vertical_ratio, avg_stance_time, avg_stance_time_balance, avg_step_length, avg_grade_adjusted_speed, avg_left_torque_effectiveness, avg_right_torque_effectiveness, avg_left_pedal_smoothness, avg_right_pedal_smoothness, time_standing, stand_count, avg_left_pco, avg_right_pco, avg_left_power_phase, avg_left_power_phase_peak, avg_right_power_phase, avg_right_power_phase_peak, pool_length, pool_length_unit, num_lengths, num_active_lengths, avg_swolf, avg_stroke_distance, avg_strokes_per_length, swim_stroke, num_laps, start_position_lat/long, end_position_lat/long, nec_lat/long, swc_lat/long`. **lap (19):** same aggregates plus `intensity, lap_trigger, wkt_step_index, repetition_num, message_index`. **record (20):** `timestamp, position_lat, position_long, enhanced_altitude/altitude, heart_rate, cadence, fractional_cadence, distance, enhanced_speed/speed, power, left_right_balance, temperature, vertical_oscillation, stance_time, stance_time_balance, step_length, vertical_ratio, respiration_rate, …`. **time_in_zone (216):** `reference_mesg, reference_index, time_in_hr_zone, time_in_power_zone, time_in_speed_zone, time_in_cadence_zone, hr_zone_high_boundary, power_zone_high_boundary, hr_calc_type, max_heart_rate, resting_heart_rate, threshold_heart_rate, pwr_calc_type, functional_threshold_power`. |
| Owner's cache today | 1 314 activities (2016-05-30 → 2026-10-31; 27 with detail), ~55 % cycling (road/indoor/virtual/MTB/gravel), ~31 % running, ~15 % swimming, plus walking/hiking/multisport/strength. |
| Rate-limit history | D-031: 429s after ~195 calls in ~2 min (per-endpoint abuse protection suspected). D-036: ~1 050 calls at 1.5 s, no 429. No data yet for ZIP downloads. |
| Existing hooks | `activity.has_fit` column (always 0), `Paths.fit_dir` (`<app data>/fit/`, never created), `.gitignore` already ignores `fit/` and `*.fit`, `settings.sync.download_fit: false`, CLI `backfill --fit` prints "Phase 5, ignored", PLAN §5.1 row "FIT archive `<app data>/fit/<activityId>.zip`". |

---

## 2. Decisions (record them as D-070 … in DECISIONS.md with any amendment)

**D-070 Storage: ZIP on disk + one JSON column.** The ZIP is the raw layer (`<app data>/fit/<activityId>.zip`,
kept by default: disk is cheap, Garmin calls are the scarce resource, and a parser improvement must never require a
re-download). The parsed summary goes to `activity_fit.data` (JSON text) with a few scalar columns for status and
counts. No per-lap or per-record tables: every read is per activity, SQLite `json_extract` covers the rare
cross-activity query, and new fields need no migration. `raw_payload` stays JSON-only (bytes never go there).

**D-071 Download is a tier A follow-up, failures never stale tier A.** Download calls carry a pseudo-tier
`Tier.F` so that a 404, a parse error or a budget stop affects only `failed_tiers={F}`; tier A completes on
search+detail+workouts as before. `F` is not in `REFRESH_TIERS`, has no TTL and is never requested by the gate.

**D-072 Fetched once.** Like detail/workouts, `activity.download` is "fresh forever" once `sync_state` holds a
200/204/404 for that id. A parse failure keeps the ZIP, stores `status='parse_error'` and never refetches;
`fit reparse` fixes it offline after a parser fix.

**D-073 Historical FIT backfill = job kind `fit`, planned whole, budget-limited, chained, yielding.** The planner
lists every eligible activity without a download attempt (newest first); the job's fetcher budget stops it
`partial`, and like D-033 the job enqueues its continuation. Between calls it checks a `sync_wanted_at` flag set
by the on-demand gate and by the lazy single-activity fetch; when set it ends `partial` ("yielded") so the daily
brief never waits behind an hour-long download chain. Not auto-started at process start: explicit command or MCP
`refresh(scope="fit")` only; an interrupted chain resumes on the next explicit request (fetched ids are skipped).

**D-074 Parser is pure and versioned.** `fit/parse.py` turns bytes into a dict without DB, paths or logging of
content; `PARSER_VERSION` is stored per row; bumping it + `fit reparse` re-derives everything from the kept ZIPs.
`normalize --all` does **not** re-parse FIT files (pure-Python decode is seconds per file) but restores
`activity.has_fit` from `activity_fit`.

**D-075 Privacy and size.** No full track: only the start and end point rounded to 4 decimals (~10 m). No serial
numbers, no `user_profile` message (weight/height), no pictures. Target ≤ 20 KB of JSON per activity, hard cap
120 KB (drop `series`, then cut laps to 100, flag `meta.truncated`).

**D-076 Settings move.** `sync.download_fit` is replaced by a `fit:` block (`fit.download: true` by default).
`doctor` warns when the old key is still present in `settings.yaml`.

---

## 3. Settings (`settings.py`, `config/settings.example.yaml`)

```yaml
fit:
  download: true               # download + parse the FIT of every new activity during sync (tier A, history)
  keep_files: true             # keep <app data>/fit/<activityId>.zip for offline re-parsing (~0.1–1 MB each)
  skip_types: []               # Garmin typeKeys never downloaded, e.g. [walking, hiking]
  backfill_days: 0             # `fit backfill` window in days; 0 = the whole activity history in the cache
  backfill_budget_calls: 100   # per fit job; jobs chain until every eligible activity has been tried
  call_gap_ms: 1500            # pacing of fit jobs (ZIP responses are heavier than JSON)
  max_laps: 300                # laps kept per activity (older ones dropped, meta.laps_truncated=true)
  series_points: 48            # points of the coarse intensity profile (bucket = max(60 s, timer/points))
```

`class FitSettings(_Model)` with these defaults; `Settings.fit: FitSettings`. Remove `SyncSettings.download_fit`.
Dependency: add `garmin-fit-sdk>=21.218.0` to `pyproject.toml` (`uv add`), imported lazily inside `fit/parse.py`
so `context show --offline` and the MCP boot stay fast.

---

## 4. Storage

### 4.1 Schema v4 (`store/db.py` MIGRATIONS[2], `SCHEMA_VERSION = 4`, no rebuild; also add to `schema.sql`)

```sql
CREATE TABLE IF NOT EXISTS activity_fit (
  activity_id INTEGER PRIMARY KEY,
  status TEXT NOT NULL,              -- 'ok' | 'parse_error' | 'no_fit' | 'not_found'
  fetched_at TEXT NOT NULL,          -- ISO8601 UTC of the download (or of the 404)
  parsed_at TEXT,                    -- ISO8601 UTC of the last parse
  parser_version INTEGER,            -- fit.parse.PARSER_VERSION used; NULL when nothing was parsed
  file_name TEXT,                    -- '<activityId>.zip' under Paths.fit_dir; NULL when not kept / not_found
  zip_sha256 TEXT, zip_bytes INTEGER, fit_bytes INTEGER,
  sport TEXT, sub_sport TEXT, device TEXT,
  n_sessions INTEGER, n_laps INTEGER, n_records INTEGER,
  data TEXT,                         -- JSON FitSummary (§5), only when status='ok'
  error TEXT                         -- redacted, ≤ 500 chars, for parse_error / no_fit
);
CREATE TABLE IF NOT EXISTS app_flag (key TEXT PRIMARY KEY, value TEXT NOT NULL);  -- 'sync_wanted_at' (§6.6)
```

`activity.has_fit = 1` iff `activity_fit.status = 'ok'`; maintained by `fit/store.py` and restored at the end of
`normalize.rebuild_all` (`UPDATE activity SET has_fit=1 WHERE activity_id IN (SELECT activity_id FROM activity_fit
WHERE status='ok')`). `activity_fit` is **not** in `NORMALIZED_TABLES`.

### 4.2 Files

`Paths.fit_dir` (`<app data>/fit/`), created on first write (`Paths.ensure()` may create it too). File name
`<activityId>.zip`, written atomically (temp file + `os.replace`). When `keep_files` is false nothing is written
and `file_name` is NULL. Status shows the folder size. The ZIP is overwritten only by an explicit re-download
(there is none in this phase: deleting the `activity_fit` row and `sync_state` row by hand is the escape hatch;
document it in `fit reparse --help`).

### 4.3 `fit/store.py` API (DB side, used by the runner, the CLI and the tools)

```python
PARSER_VERSION  # re-exported from fit.parse
def ingest(conn, paths, settings, activity_id: int, zip_bytes: bytes, now: datetime, *, hr_zones=None) -> str
    # write file (if keep_files) -> extract -> parse -> upsert activity_fit + activity.has_fit; returns status
def mark(conn, activity_id: int, status: str, now: datetime, error: str | None = None) -> None   # 'not_found'
def reparse(conn, paths, settings, activity_id: int, now: datetime, *, hr_zones=None) -> str      # from the kept ZIP
def pending_ids(conn, settings, today: date) -> list[int]   # eligible activities without a download attempt, newest first
def coverage(conn, settings, paths) -> dict                 # the status block of §8.3
def summary(conn, activity_id: int) -> dict | None          # json.loads(data) when status='ok'
```

`ingest` is called inside the runner's `BEGIN IMMEDIATE … COMMIT` so that `activity_fit`, `activity.has_fit` and
`sync_state` change atomically; the file write happens just before `BEGIN` (an orphan file after a crash is
harmless and overwritten next time). `hr_zones` are the athlete's Garmin HR zone floors for the sport (from the
latest `profile_snapshot` kind `hr_zones`, same lookup `metrics` already does), used only by the computed-zones
fallback (§5.2).

---

## 5. The parser (`fit/parse.py`, pure)

```python
PARSER_VERSION = 1

@dataclass
class FitResult:
    status: Literal["ok", "parse_error", "no_fit"]
    summary: dict[str, Any] | None
    fit_bytes: int
    error: str | None
    sport: str | None; sub_sport: str | None; device: str | None
    n_sessions: int; n_laps: int; n_records: int

def extract_fit(zip_bytes: bytes) -> bytes | None
    # bytes already FIT (Decoder.is_fit) -> returned as is; else first ZIP member whose name ends with .fit
    # (case-insensitive); None when no such member or ZIP invalid. Refuse members > 50 MB (zip bomb) -> treated as
    # parse_error "member too large".
def parse_fit(fit_bytes: bytes, *, hr_zone_floors: list[int] | None = None, max_laps: int = 300,
              series_points: int = 48) -> FitResult
```

Decoder options: `apply_scale_and_offset=True, convert_datetimes_to_dates=True, convert_types_to_strings=True,
enable_crc_check=False` (the ZIP already carries a CRC32), `expand_sub_fields=True, expand_components=True,
merge_heart_rates=True`. Decoder `errors` that do not prevent a session message are kept in `meta.errors` (≤ 5
strings, redacted) and the status stays `ok`; no session message, or an exception, → `parse_error`. Every field is
optional: missing → key omitted (never `null` noise). Units stay SI as in the rest of the project (m, s, m/s,
bpm, W, °C); datetimes → ISO 8601 UTC strings; semicircles → degrees (`× 180 / 2**31`), 4 decimals.

### 5.1 FitSummary JSON (document it verbatim in `docs/FIT.md`)

```jsonc
{
  "meta": {"parser_version": 1, "sdk_version": "21.218.0", "profile_version": "...", "file_type": "activity",
           "time_created": "2026-10-30T06:01:12Z", "n_sessions": 1, "n_laps": 8, "n_records": 3612,
           "laps_truncated": false, "truncated": false, "errors": []},
  "session": {                       // the single session, or the first one (then "sessions" lists them all)
    "sport": "cycling", "sub_sport": "road", "profile_name": "Road",
    "start_time": "...Z", "end_time": "...Z",
    "elapsed_s": 3725.4, "timer_s": 3600.0, "moving_s": 3580.0, "paused_s": 125.4,
    "distance_m": 30210.5, "ascent_m": 412, "descent_m": 408, "calories_kcal": 812,
    "avg_speed_mps": 8.39, "max_speed_mps": 15.2, "avg_gap_mps": 3.1,          // gap: grade-adjusted (running)
    "avg_hr": 142, "max_hr": 171, "min_hr": 88,
    "avg_cadence": 84, "max_cadence": 112, "avg_cadence_spm": 172,           // spm only for running: (avg_cadence + avg_fractional_cadence) × 2
    "avg_power": 201, "max_power": 612, "normalized_power": 221, "ftp": 250, "tss": 61.2, "intensity_factor": 0.88,
    "work_kj": 724, "training_load_peak": 61,
    "aerobic_te": 3.4, "anaerobic_te": 0.8,
    "avg_temperature_c": 18, "avg_respiration_rate": 28.1, "avg_spo2": 97, "avg_stress": 42,
    "rmssd_hrv_ms": 31.2, "sdrr_hrv_ms": 44.0, "rpe": 7.0, "feel": 3,
    "pool_length_m": 25, "num_lengths": 80, "num_active_lengths": 76, "avg_swolf": 38, "stroke": "freestyle",
    "num_laps": 8
  },
  "sessions": [ { ...same keys per leg... } ],   // only when n_sessions > 1 (multisport)
  "zones": {
    "source": "time_in_zone" | "session" | "computed",
    "hr":    {"time_s": [0, 610, 1820, 940, 230, 0], "boundaries_bpm": [113, 131, 148, 165, 182],
              "calc_type": "percent_max_hr", "max_hr": 185, "resting_hr": 48, "threshold_hr": 166},
    "power": {"time_s": [...], "boundaries_w": [...], "ftp": 250, "calc_type": "percent_ftp"}
  },
  "laps": [
    {"i": 1, "start_time": "...Z", "elapsed_s": 600.2, "timer_s": 600.0, "distance_m": 2650.0,
     "avg_speed_mps": 4.42, "max_speed_mps": 4.9, "avg_hr": 168, "max_hr": 176, "avg_power": 312, "max_power": 480,
     "normalized_power": 318, "avg_cadence": 91, "avg_cadence_spm": 182, "ascent_m": 12, "descent_m": 10,
     "intensity": "active", "trigger": "manual", "wkt_step_index": 3, "balance_right_pct": 50.8,
     "step_length_m": 1.32, "vertical_oscillation_mm": 84.0, "stance_time_ms": 228, "stance_time_balance_pct": 49.6,
     "vertical_ratio_pct": 7.9, "avg_gap_mps": 4.5, "num_lengths": 4, "stroke": "freestyle", "avg_swolf": 36}
  ],
  "intervals": {                     // derived from lap.intensity; present only with ≥ 2 'active' laps and ≥ 1 'rest'/'recovery' lap
    "work": {"n": 6, "avg_duration_s": 240.1, "avg_distance_m": 1001.3, "avg_hr": 169, "max_hr": 178,
             "avg_power": 318, "avg_speed_mps": 4.17, "first_to_last_speed_pct": -2.1, "first_to_last_hr": 6},
    "rest": {"n": 5, "avg_duration_s": 120.4, "avg_hr": 141, "avg_speed_mps": 2.5},
    "lap_indexes": {"work": [2,4,6,8,10,12], "rest": [3,5,7,9,11]}
  },
  "dynamics": {                      // sport-specific session fields, not repeated in "session"
    "run":  {"cadence_spm": 172, "step_length_m": 1.21, "vertical_oscillation_mm": 88.2, "vertical_ratio_pct": 8.4,
             "stance_time_ms": 241, "stance_time_balance_pct": 49.8, "gap_mps": 3.1},
    "bike": {"balance_right_pct": 51.2, "torque_effectiveness_pct": [78.1, 79.4], "pedal_smoothness_pct": [21.0, 22.3],
             "time_standing_s": 310, "stand_count": 14, "pco_mm": [2, -1], "power_phase_deg": [[12, 190], [14, 193]],
             "power_phase_peak_deg": [[60, 120], [62, 123]]},
    "swim": {"swolf": 38, "stroke_distance_m": 2.1, "strokes_per_length": 18.5, "stroke": "freestyle", "pool_length_m": 25}
  },
  "records": {                       // aggregates derived from record messages; the records themselves are discarded
    "n": 3612, "interval_s": 1, "coverage_pct": {"hr": 99.8, "power": 97.2, "cadence": 96.0, "position": 100.0},
    "decoupling_pct": 3.4,           // Pw:HR (bike with power) or Pa:HR (run, speed): (first half ratio − second half ratio) / first half × 100, only when timer ≥ 30 min and hr+power/speed coverage ≥ 80 %
    "power_zero_pct": 11.3,          // cycling: share of records with power == 0 (coasting)
    "power_source": "record.power" | "developer:Power" | null
  },
  "series": [                        // ≤ series_points buckets over timer time, bucket_s = max(60, ceil(timer_s / series_points)); empty buckets (pauses) skipped
    {"t_s": 0, "hr": 112, "power": 150, "speed_mps": 7.9, "cadence": 82, "alt_m": 211, "dist_m": 480},
    ...
  ],
  "route": {"start": [45.4379, 12.3351], "end": [45.4381, 12.3349]},   // omitted when no position
  "device": {"manufacturer": "garmin", "product": "fenix7", "software_version": 16.22,
             "sensors": ["heart_rate", "bike_power", "bike_speed_cadence"]}   // distinct antplus/ble device types of non-creator device_info messages
}
```

### 5.2 Extraction rules

- **session**: `session_mesgs[0]` (all of them into `sessions` when > 1; `session` then is the first leg — the
  tools present `sessions` when present). `end_time = start_time + total_elapsed_time`; `paused_s = max(elapsed −
  timer, 0)`. Prefer `enhanced_*` over plain speed/altitude fields. `workout_rpe` is already scaled by the SDK
  (check: FIT scale 10 → a value like 7.0).
- **zones**: first choice `time_in_zone_mesgs` whose `reference_mesg == "session"` (arrays and boundaries come from
  the same message); second choice `session.time_in_hr_zone` / `time_in_power_zone` (`source: "session"`); last
  resort when both are absent and `hr_zone_floors` were passed: bucket record `heart_rate` × `interval_s` into the
  floors (`source: "computed"`, 5 entries plus a leading "below zone 1" entry so the shape matches Garmin's). Keep
  Garmin's array as the device wrote it; the index-0 meaning (time below zone 1) must be confirmed against the
  list's `hrTimeInZone_1..5` on a real file (§12) and documented in `docs/FIT.md`.
- **laps**: `lap_mesgs` in order, `i` from 1; keep the first `max_laps`, flag `laps_truncated`. Drop a lap only when
  it has neither `total_timer_time` nor `total_distance`. `balance_right_pct` decodes `left_right_balance`
  (`left_right_balance_100` type: bit 15 = "value refers to the right side", low 15 bits = percent × 100; if the
  SDK already expanded it into a numeric right-percent after `expand_components`, use that — verify live).
- **intervals**: as in the JSON above. `first_to_last_speed_pct` = (last work lap speed − first) / first × 100;
  `first_to_last_hr` = last work lap avg HR − first. Omit the whole block when the rule does not match.
- **dynamics**: one sub-object for the session's sport family (`sport` ∈ running → `run`, cycling → `bike`,
  swimming → `swim`; others: omit). Left/right pairs are `[left, right]` arrays.
- **records**: single pass over `record_mesgs`, nothing stored. `interval_s` = median of timestamp deltas
  (rounded to int). Coverage = share of records having the field. Decoupling as defined above (halves split by
  timer time, not by index; records during pauses are excluded by using `timer` events if present, otherwise by
  timestamp gaps > 2 × interval). Developer field named `Power`/`Running Power` (via `field_description_mesgs`)
  is used when `record.power` is absent (`power_source`).
- **series**: buckets of `bucket_s` over `t_s = timestamp − session.start_time` in whole seconds; `hr`, `power`,
  `speed_mps`, `cadence` averaged, `alt_m`, `dist_m` last value; values rounded (hr/power/cadence int, speed 2
  decimals). `t_s` is the bucket start.
- **route**: session `start_position_*` / `end_position_*`; fallback first/last record with a position.
- **device**: `device_info_mesgs` with `device_index == "creator"` (or the first message) → manufacturer, product
  (`garmin_product` string when expanded, else the product number as string), `software_version`. Sensors: distinct
  `antplus_device_type` / `ble_device_type` / `source_type` strings of the other device_info messages, excluding
  the creator; serial numbers are never copied.
- **sport names** come from `convert_types_to_strings` (e.g. `"cycling"`, `"road"`); they are *not* the Garmin
  Connect `typeKey` — the row's `sport`/`sub_sport` columns store the FIT names, `activity.type_key` stays the source
  for sport family logic (`metrics.sport.family`).

### 5.3 Performance

Pure-Python decoding of a 1 s-recorded 4 h ride is several seconds. Acceptable in jobs and in the synchronous lazy
fetch of one activity (inside `sync_timeout_seconds`). If a live measurement shows > 5 s for typical files, switch
records to a `mesg_listener` that aggregates on the fly and does not retain the message, and disable
`expand_components` for record messages; log the timing in DECISIONS.

---

## 6. Sync integration

### 6.1 `garmin/endpoints.py`
- `Tier.F = "F"` (comment: FIT downloads; pseudo-tier, no TTL, never requested by the gate). Not added to
  `REFRESH_TIERS`, `SYNC_TIERS`, `BACKGROUND_TIERS`. `TIER_ENDPOINTS[Tier.F] = ("activity.download",)`.
- Endpoint 4: domain `"fit"`; the 26-endpoint assertion stays.

### 6.2 `garmin/client.py`
In `fetch`, catch `GarminPersonalApiError` that is not an `HttpError` and raise `NotFoundError(f"{endpoint.name}:
empty response", 404)` — the only known case is the empty download body ("no file"). `last_status` 404. Everything
else (budget, gap, backoff, 429 circuit) applies unchanged to downloads.

### 6.3 `sync/planner.py`
- `is_fresh`: `activity.download` joins `activity.detail`/`activity.workouts` in the "fetched once" rule.
- `expand(search page)`: for each activity inside the detail window (`detail_days`), when `settings.fit.download`
  and `typeKey` not in `fit.skip_types` and no `sync_state` row for (`activity.download`, id): append
  `Call("activity.download", str(aid), {"activity_id": aid}, Tier.F, f"activity.download {aid} ({start_local})")`.
  Applies to both tier A refreshes and the history backfill (same detail window).
- `plan({Tier.F})`: one `Call` per id from `fit.store.pending_ids` (activities with no `sync_state` row for the
  download, `type_key` not skipped, `start_local` within `fit.backfill_days` when > 0, newest first). No limit: the
  fetcher budget bounds the job and the chain continues (D-073).
- `lazy_detail_calls` → rename `lazy_activity_calls(activity_id, *, want_fit: bool)`: detail + workouts when
  `has_detail = 0`, plus the download when `want_fit` and eligible and unattempted (2–3 calls).

### 6.4 `sync/runner.py`
- Constructor: `paths: Paths | None = None` (needed for `fit_dir`; `None` ⇒ files not kept, used by a few tests)
  and `yield_check: Callable[[], bool] | None = None`.
- Queue discipline: download calls go to a second deque `deferred`; the main loop pops from `queue` first and from
  `deferred` only when `queue` is empty, so paging and detail calls (which complete tier A) always run before the
  heavier downloads. `calls_planned` counts both.
- Before each call: `if self.yield_check and self.yield_check(): res.status, stop_reason = "partial",
  "yielded: on-demand refresh waiting"; requeue the call; break`.
- `_store`: when `call.endpoint == "activity.download"`, the body is `bytes` → `fit.store.ingest(...)` inside the
  same `BEGIN IMMEDIATE` transaction (no `raw_payload`, no `raw_ids` → `on_finished` is not triggered by FIT alone).
  `hr_zones` for the computed fallback come from a small helper reading the latest `hr_zones` snapshot.
- `NotFoundError` branch: for `activity.download` also `fit.store.mark(conn, aid, "not_found", now)`.
- Any other failure of a download is recorded in `sync_state` with its status (so a 5xx is retried on the next
  plan, a 400 is not) and adds `Tier.F` to `failed_tiers` only. The run status rules are unchanged.
- Logging: `GET activity.download key=<id> status=200 ms=… bytes=<n>` — never the content.

### 6.5 `sync/jobs.py`
- New kind `"fit"`. `App.job_fetcher("fit")` → budget `fit.backfill_budget_calls`, gap
  `max(fit.call_gap_ms, sync.min_call_gap_ms)`.
- `_execute`: for kind `fit`, `Runner(..., paths=self.paths, yield_check=lambda: repo.flag_age_seconds(conn,
  "sync_wanted_at", now) is not None and < 30)`.
- Chaining (D-033 generalised): `kind in ("backfill", "fit") and status == "partial" and calls_done > 0 and error
  contains "budget" or "yielded" and not cancelled` → enqueue continuation with the same trigger. A `fit`
  continuation after a yield must not fail `locked`: fit jobs retry the lock up to 10 times (5 s apart) instead of
  once (`_requeued` becomes a counter).
- `EXIT_CODES`, `TERMINAL`, orphan cleanup, prune: unchanged.

### 6.6 The `sync_wanted_at` flag (`store/repo.py`)
`set_flag(conn, key, value)`, `get_flag`, `clear_flag`, `flag_age_seconds(conn, key, now)`. The freshness gate sets
`sync_wanted_at = now` right before `RefreshLock.acquire` in the synchronous path (step 3-4) and clears it in the
`finally` after release; `CoachService._lazy_fetch` does the same. Background refresh jobs do not set it (they are
FIFO behind nothing). A stale flag (> 30 s) is ignored, so a crashed process cannot stall fit jobs forever.

### 6.7 Freshness gate, startup, status
- `freshness.py`: only the flag (6.6). Tier F never appears in `Freshness.tiers`.
- `startup.py`: unchanged (no automatic fit backfill, D-073).
- `status.py`: `tiers` keeps A–E + H; add the `fit` block (§8.3). Cooldowns on tier F (a 429 storm during a fit
  job opens the circuit on F for `cooldown_minutes.rate_limit`) are shown in `fit.cooldown_until` and block
  `fit backfill` / `refresh(scope="fit")` like the history cooldown does.

### 6.8 `normalize/__init__.py`
`rebuild_all`: after replaying raw payloads, restore `activity.has_fit` from `activity_fit` (§4.1).

---

## 7. CLI (`cli.py`, new `fit` sub-typer)

| Command | Behaviour |
|---|---|
| `garmin-coach fit backfill [--days N] [--background] [--quiet]` | Startup check (refresh_stale=False), cooldown check on F, then enqueue kind `fit` (or follow the active one) and follow the chain like `backfill` does; exit codes 0/2/1. `--days` overrides `fit.backfill_days` for this run. `--background` spawns a detached child as `sync --background` does. |
| `garmin-coach fit reparse [--all] [ACTIVITY_ID ...]` | Offline. Re-parses kept ZIPs whose `parser_version < PARSER_VERSION` or status `parse_error` (`--all`: every kept ZIP; ids: those only). Prints counts; rows without a kept file are listed as "no file kept: delete the `activity_fit` and `sync_state` rows to re-download". |
| `garmin-coach fit show ACTIVITY_ID [--json]` | Offline. Prints the FitSummary (JSON) or a short Rich table (session, zones, laps). Debug aid. |
| `garmin-coach backfill --fit` | After the history chain ends `done`, enqueue a `fit` job and follow it (replaces the "Phase 5, ignored" message). |
| `garmin-coach status` | Caption gains `FIT: 412 ok · 890 pending · 7 no file · 1 parse error · 310 MB` and the running/last fit job when any. `--json` carries the `fit` block. |
| `garmin-coach doctor` | Checks: `garmin-fit-sdk` importable (version), `fit_dir` creatable/writable, legacy `sync.download_fit` key warning, FIT coverage line. |

---

## 8. MCP

### 8.1 `get_activity(activity_id, include_splits=False, include_laps=False, include_series=False, max_age_minutes=None)`
- Lazy fetch (`_lazy_fetch`) now plans detail + workouts (when `has_detail = 0`) **and** the download (when
  `fit.download`, type not skipped, no attempt yet) — 2–3 synchronous calls under the refresh lock, with the
  `sync_wanted_at` flag set. Parsing happens inside the call.
- Answer gains:
  - `fit_status`: `"ok" | "not_downloaded" | "no_file" | "parse_error" | "disabled" | "skipped_type"`
    (`no_file` covers `not_found` and `no_fit`).
  - `fit` (only when `ok`): `meta` (parser_version, n_laps, n_records, laps_truncated, truncated), `session`,
    `sessions` (if any), `zones`, `intervals`, `dynamics`, `records`, `route`, `device`; plus `laps` when
    `include_laps` (at most `MAX_LAPS_OUT = 80`, `laps_more: true` when cut) and `series` when `include_series`.
  - `fit_problem` when the lazy download failed (same wording style as `detail_problem`).
- Payload guard unchanged (`MAX_PAYLOAD_CHARS`); a typical answer with laps is ≤ 25 KB.
- Tool docstring (server.py) rewritten to advertise the FIT sections and the two new flags.

### 8.2 `list_activities`
Each item gains `has_fit: bool`. `_ACTIVITY_HIDDEN` no longer hides `has_fit` in `get_activity.activity`.

### 8.3 `get_sync_status` / `status --json`
```json
"fit": {"enabled": true, "ok": 412, "parse_error": 1, "no_file": 7, "pending": 890, "files_mb": 310,
        "cooldown_until": null, "job": {"id": 31, "status": "running", "calls_done": 40, "calls_planned": 890,
        "current_step": "activity.download 1234 (2025-03-02)", "eta_seconds": 1300},
        "last_job": {"id": 30, "status": "partial", "calls_done": 100, "error": "…budget…", "finished_at": "…"}}
```
`job` is present only when the running job is of kind `fit` (the generic `job` key keeps showing whatever runs).

### 8.4 `refresh(tiers?, force?, scope="tiers"|"history"|"fit")`
`scope="fit"`: same guards as `history` (active fit job → return it; requested < 5 min ago → error; F cooldown →
error), then `enqueue("fit", {Tier.F}, "mcp")`. `force` is ignored for fit (never re-downloads). Note text: "FIT
backfill started in the background: it runs in chained jobs and pauses for on-demand refreshes; follow it with
get_sync_status".

### 8.5 Server instructions and prompts
Add one sentence to the MCP instructions: "Per-activity FIT data (laps, intervals, HR/power zones, dynamics,
decoupling) is in `get_activity.fit` when `fit_status` is `ok`; `refresh(scope="fit")` loads the past." Packs,
resources and prompts are unchanged in this phase.

---

## 9. Skill (`.claude/skills/coach/SKILL.md`)

- **By argument**, add `fit`: "`refresh(scope="fit")`, then `get_sync_status`; report coverage (`fit` block),
  explain it runs in the background in chained jobs and that `/coach status` shows progress."
- **`status`**: also report the `fit` block (ok / pending / no file / parse errors, running job).
- New section **Session analysis**: when the check-in assesses yesterday's key session, when the user asks about a
  specific workout, or during `week` for the key sessions: call `get_activity(id, include_laps=true)` and read
  `fit`: `zones` (time in HR and power zones), `intervals` + `laps` (reps: pace/power/HR per rep, fade across reps
  via `first_to_last_*`), `dynamics` (balance, cadence, ground contact), `records.decoupling_pct` (aerobic
  durability: < 5 % solid, 5–10 % acceptable, > 10 % drift worth a comment), `series` only when the user asks about
  the shape of the session. Prefer FIT for laps/zones and the activity list for load/TE (Garmin's recomputed
  values). If `fit_status` is not `ok`, say that the FIT is not available for that activity (and why) and, when it
  is `not_downloaded`, offer `/coach fit`.
- **Data honesty**: a FIT value never overrides a reported symptom; never infer intervals when `intervals` is
  absent.

---

## 10. Documentation to update

- `docs/FIT.md` (new): the JSON schema of §5.1 with provenance per key, the caps, the zone-array index note, the
  balance decoding note, the re-parse procedure, disk usage expectations.
- `docs/PLAN.md`: §3 row 4 ("Optional FIT archive (Phase 5)" → "FIT download, see PLAN-FIT.md; tier A follow-up /
  F"), §5.1 FIT archive row, §6.1 commands, §9 tool list (`get_activity` flags, `refresh` scope), Phase 5 bullet →
  "done (PLAN-FIT.md)", §11.1 settings block.
- `docs/DECISIONS.md`: D-070 … plus the live findings of §12.
- `README.md` (Italian): "Stato del progetto" row 5, "Comandi" table (fit commands), "Cosa legge il coach" (FIT
  sections), "Dove stanno i dati" (fit folder and size), settings snippet.
- `config/settings.example.yaml`: `fit:` block, remove `sync.download_fit`.
- `CONTRIBUTING.md`: nothing unless a new dev dependency appears.

---

## 11. Tests (offline, in-memory DB, fake clock; `tests/fakes.py` extended)

- `tests/fitgen.py`: builds a deterministic synthetic FIT with `garmin_fit_sdk.Encoder` (file_id, device_info
  creator + HR sensor, sport, event start/stop, 1 session, 11 laps alternating `active`/`rest` with
  `wkt_step_index`, `time_in_zone` for the session, ~1 800 records at 1 Hz with timestamp/heart_rate/power/
  enhanced_speed/distance/position/altitude/cadence) and a ZIP around it. Reused by `FakeGarmin._activity_download`
  (cached bytes; `fake.no_fit_ids` → `NotFoundError`; `fake.bad_zip_ids` → random bytes; `fake.gpx_ids` → ZIP with
  a `.gpx` member). If `Encoder` turns out unusable, commit a ≤ 100 KB synthetic `.fit` produced once by a script
  kept in `tests/` (never a real recording: real FIT files carry GPS and device serials).
- `test_fit_parse.py`: every §5.1 section against the synthetic file (exact numbers), laps cap + flag, intervals
  rule (present/absent), decoupling (construct halves with known ratios), series bucket count and `t_s`, route
  rounding, computed-zones fallback when `time_in_zone` is removed, `no_fit` for a GPX ZIP, `parse_error` for
  garbage and for an oversized member, no `null` values in the output, JSON size under the cap.
- `test_fit_store.py`: ingest writes file + row + `has_fit`, `keep_files=false` writes no file, `mark not_found`,
  `reparse` bumps `parser_version`, `pending_ids` ordering/filters (`skip_types`, `backfill_days`, attempted ids
  excluded), `rebuild_all` restores `has_fit`.
- `test_sync_runner.py` additions: a new activity inside the detail window costs search + 3 calls and stores FIT;
  download 404 / parse error / 5xx leave tier A complete and `sync_state` tombstoned; budget exhausted with
  downloads deferred → A complete, job `partial`, the next A refresh picks the downloads up; `fit.download=false`
  plans no download; `skip_types` honoured; `Tier.F` plan is newest first and whole; `yield_check` ends the run
  `partial` with the call requeued; deferred queue order (details before downloads across pages).
- `test_startup_jobs.py` additions: fit chain on budget partial; fit chain on yield; continuation retries the
  lock; cancel stops the chain; startup never enqueues a fit job; `sync_wanted_at` set/cleared by the gate and
  ignored when older than 30 s.
- `test_mcp_tools.py` additions: `get_activity` answers with `fit` sections, `include_laps` cap and `laps_more`,
  `fit_status` values for each row state, lazy fetch makes 3 calls (2 when FIT disabled) and never touches tier A
  freshness, `list_activities.has_fit`, `get_sync_status.fit`, `refresh(scope="fit")` guards (active, 5-min,
  cooldown), payload guard still enforced.
- `test_context.py`: golden packs unchanged (FIT is tool-only in this phase).
- `test_live.py` (behind `GARMIN_COACH_LIVE=1`): download one recent activity, parse it, assert `status == "ok"`
  and that `zones.hr.time_s` is consistent with the list's `hrTimeInZone_*` (within rounding).
- `ruff`, `mypy` clean; `tests/test_redaction.py` keeps scanning the new files.

---

## 12. To verify live on the owner's machine (log the answers in DECISIONS.md)

1. ZIP member naming and whether child activities of a multisport, manual entries and GPX/TCX uploads answer 404,
   an empty body or a ZIP without `.fit`.
2. PLAN §14 Q5: is the FIT complete on this tier (session `training_stress_score`, `total_training_effect`,
   `training_load_peak` present)? Compare with the list's `activityTrainingLoad` / `aerobicTrainingEffect`.
3. Meaning of index 0 in `time_in_hr_zone` versus the list's `hrTimeInZone_1..5`.
4. `left_right_balance` shape after `expand_components` (number or dict/flags).
5. Decode time per file (short run, 4 h ride); apply §5.3 if slow.
6. 429 behaviour of ZIP downloads at 1.5 s gap over a 100-call fit job; adjust `fit.call_gap_ms` default if needed.
7. Disk use after the full backfill (expected 0.3–1 GB for 1 314 activities).

---

## 13. Acceptance criteria

- `uv sync` installs `garmin-fit-sdk`; `uv run pytest`, `ruff check`, `mypy` pass; no network in tests.
- Opening the owner's v3 database migrates to v4 without touching existing rows.
- After a new activity syncs, `get_activity` returns `fit_status: "ok"` with `session`, `zones`, `intervals` (when
  applicable), `dynamics`, `records`, `route`, `device`; `include_laps` adds the laps.
- A download that fails for any reason never leaves tier A stale and never breaks the daily brief.
- `garmin-coach fit backfill --background` on the real cache chains jobs until every eligible activity has been
  tried, pauses within one call gap when the daily brief needs Garmin, and `status --watch` shows the progress and
  the ETA.
- `normalize --all` leaves `activity_fit` and `has_fit` intact; `fit reparse --all` rebuilds every summary from
  the kept ZIPs without network.
- `/coach` can answer "how did my intervals go yesterday" from the FIT data, and says plainly when a FIT is
  missing.

---

## 14. Suggested work order

1. Settings (`FitSettings`), dependency, schema v4 + migration, `fit/parse.py` + `tests/fitgen.py` +
   `test_fit_parse.py` (the parser is the riskiest part: do it first, against the synthetic file).
2. `fit/store.py`, planner/runner/client/endpoints changes, `FakeGarmin._activity_download`, runner tests.
3. Jobs (`fit` kind, chaining, yield, lock retries), `app_flag` helpers, gate + lazy fetch flag, job tests.
4. CLI `fit` commands, `backfill --fit`, `status`/`doctor` additions, MCP tools + server descriptions, tool tests.
5. Skill, docs (FIT.md, PLAN.md, DECISIONS.md, README.md, settings example).
6. Hand over to the owner for the live steps of §12 (do not run them unasked); record the answers.
