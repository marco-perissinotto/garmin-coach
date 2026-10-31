# FIT summaries

Every activity that enters the cache also gets its original FIT file: `GET /download-service/files/activity/{id}`
(endpoint 4 of PLAN §3) returns a ZIP, which is kept under `<app data>/fit/<activityId>.zip` and reduced by
`garmin_coach.fit.parse` to a compact JSON summary stored in `activity_fit.data`. The MCP tool `get_activity` serves it
as `fit` (laps and series on request). Decisions: D-070 onwards in
[DECISIONS.md](DECISIONS.md).

## How files arrive

| When | What | Calls |
|---|---|---|
| Tier A refresh / history backfill | every activity inside `sync.detail_days` (28 days) without a download attempt | 1 per activity, after the calls that complete tier A |
| `get_activity` on an older activity | detail + workouts when missing, plus the FIT when never tried | up to 3, synchronous |
| `garmin-coach fit backfill`, MCP `refresh(scope="fit")` | every activity in the cache without an attempt, newest first (`fit.backfill_days` limits the window) | chained jobs of `fit.backfill_budget_calls` (100) at `fit.call_gap_ms` (1.5 s) |

A file is fetched **once**. HTTP 404 or an empty body = no original file (`not_found`, e.g. a manual entry); a ZIP
without a `.fit` member = `no_fit` (GPX/TCX upload); an unreadable file = `parse_error` (the ZIP is kept). HTTP 429
and 5xx are retried later; a 400 is not. A failed download never makes tier A stale (downloads run as pseudo-tier F).
The FIT backfill pauses within one call when an on-demand refresh needs Garmin (flag `sync_wanted_at`) and resumes
in a new job.

## Table `activity_fit`

One row per download attempt (schema v4): `status` (`ok | parse_error | no_fit | not_found`), `fetched_at`,
`parsed_at`, `parser_version`, `file_name`, `zip_sha256`, `zip_bytes`, `fit_bytes`, `sport`, `sub_sport`, `device`,
`n_sessions`, `n_laps`, `n_records`, `data` (the JSON below, only when `ok`), `error` (redacted, ≤ 500 characters).
`activity.has_fit = 1` exactly when `status = 'ok'`. The table is not part of the normalized layer: `normalize --all`
keeps it and restores `has_fit`. Cross-activity questions work with `json_extract`, e.g.
`SELECT activity_id, json_extract(data, '$.records.decoupling_pct') FROM activity_fit WHERE status='ok'`.

## The FitSummary JSON

Conventions: every key is optional; a value the file does not carry is **left out**, never `null`. Units are SI as in
the rest of the project (m, s, m/s, bpm, W, °C); datetimes are ISO 8601 UTC (`...Z`); positions are degrees with 4
decimals (~10 m). Names in the right column are FIT profile fields as decoded by `garmin-fit-sdk` with scale and
offset applied.

```jsonc
{
  "meta": {"parser_version": 1, "sdk_version": "21.218.0", "profile_version": "21.218", "file_type": "activity",
           "time_created": "2026-10-30T06:01:12Z", "n_sessions": 1, "n_laps": 8, "n_records": 3612,
           "laps_truncated": false, "truncated": false, "errors": []},
  "session": {...},              // the single session, or the first leg
  "sessions": [{...}],           // only when n_sessions > 1 (multisport): one object per leg, same keys as session
  "zones": {...},
  "laps": [{...}],
  "intervals": {...},            // only with >= 2 work laps and >= 1 rest lap
  "dynamics": {"run" | "bike" | "swim": {...}},
  "records": {...},
  "series": [{...}],
  "route": {"start": [lat, lon], "end": [lat, lon]},
  "device": {...}
}
```

### `meta`

| Key | Source |
|---|---|
| `parser_version` | `fit.parse.PARSER_VERSION` |
| `sdk_version` | `garmin_fit_sdk.__version__` |
| `profile_version` | FIT file header, bytes 2-3 (÷1000 from 22.0, ÷100 before, the SDK's rule) |
| `file_type`, `time_created` | `file_id.type`, `file_id.time_created` |
| `n_sessions`, `n_laps`, `n_records` | counts of `session`, `lap` (with timer time or distance), `record` messages |
| `laps_truncated` | more than `fit.max_laps` (300) laps: only the first ones are kept |
| `truncated` | the size cap removed `series` and/or laps (see Caps) |
| `errors` | decoder errors that still left a session (≤ 5, redacted), e.g. a truncated file |

### `session` (and each item of `sessions`)

| Key | FIT field (`session`) |
|---|---|
| `sport`, `sub_sport`, `profile_name` | `sport`, `sub_sport`, `sport_profile_name` (FIT names such as `cycling`/`road`, not Garmin Connect typeKeys) |
| `start_time`, `end_time` | `start_time`; `start_time + total_elapsed_time` |
| `elapsed_s`, `timer_s`, `moving_s`, `paused_s` | `total_elapsed_time`, `total_timer_time`, `total_moving_time`; `max(elapsed − timer, 0)` |
| `distance_m`, `ascent_m`, `descent_m`, `calories_kcal` | `total_distance`, `total_ascent`, `total_descent`, `total_calories` |
| `avg_speed_mps`, `max_speed_mps` | `enhanced_avg_speed` / `avg_speed`, `enhanced_max_speed` / `max_speed` |
| `avg_gap_mps` | `avg_grade_adjusted_speed` (running) |
| `avg_hr`, `max_hr`, `min_hr` | `avg_heart_rate`, `max_heart_rate`, `min_heart_rate` |
| `avg_cadence`, `max_cadence` | `avg_cadence`, `max_cadence` (rpm; strides/min when running) |
| `avg_cadence_spm` | running only: `(avg_cadence + avg_fractional_cadence) × 2` |
| `avg_power`, `max_power`, `normalized_power`, `ftp` | `avg_power`, `max_power`, `normalized_power`, `threshold_power` |
| `tss`, `intensity_factor` | `training_stress_score`, `intensity_factor` |
| `work_kj` | `total_work` ÷ 1000 |
| `training_load_peak`, `aerobic_te`, `anaerobic_te` | `training_load_peak`, `total_training_effect`, `total_anaerobic_training_effect` |
| `avg_temperature_c`, `avg_respiration_rate`, `avg_spo2`, `avg_stress` | `avg_temperature`, `enhanced_avg_respiration_rate` / `avg_respiration_rate`, `avg_spo2`, `avg_stress` |
| `rmssd_hrv_ms`, `sdrr_hrv_ms` | `rmssd_hrv`, `sdrr_hrv` |
| `rpe` | `workout_rpe` ÷ 10 (stored 10-100 for RPE 1-10, D-078) |
| `feel` | `workout_feel` as stored (0-100: 0 very weak … 100 very strong) |
| `pool_length_m`, `num_lengths`, `num_active_lengths`, `avg_swolf`, `stroke` | `pool_length`, `num_lengths`, `num_active_lengths`, `avg_swolf`, `swim_stroke` |
| `num_laps` | `num_laps` |

### `zones`

```jsonc
"zones": {"source": "time_in_zone" | "session" | "computed",
          "hr":    {"time_s": [0, 610, 1820, 940, 230, 0], "boundaries_bpm": [113, 131, 148, 165, 182],
                    "calc_type": "percent_max_hr", "max_hr": 185, "resting_hr": 48, "threshold_hr": 166},
          "power": {"time_s": [...], "boundaries_w": [...], "ftp": 250, "calc_type": "percent_ftp"}}
```

1. `time_in_zone` message with `reference_mesg = session`: `time_in_hr_zone`, `hr_zone_high_boundary`, `hr_calc_type`,
   `max_heart_rate`, `resting_heart_rate`, `threshold_heart_rate`; `time_in_power_zone`, `power_zone_high_boundary`,
   `functional_threshold_power`, `pwr_calc_type`.
2. Else `session.time_in_hr_zone` / `time_in_power_zone` (`source: "session"`, only `time_s`).
3. Else, when the athlete's Garmin HR zone floors are known (latest `hr_zones` snapshot, sport-specific when
   available), the records' heart rate × recording interval bucketed into the floors (`source: "computed"`, 6 entries:
   index 0 = below zone 1, then zones 1-5; key `floors_bpm` instead of `boundaries_bpm`, D-077).

Times are whole seconds, the array exactly as the device wrote it. **Index 0**: Garmin devices write one more entry
than zones (time below zone 1 first); whether index 1-5 equal the activity list's `hrTimeInZone_1..5` is checked live
(`tests/test_live.py`, D-083) and recorded in DECISIONS.md.

### `laps`

`lap` messages in file order, `i` from 1; a lap with neither timer time nor distance is dropped. Keys: `i`,
`start_time`, `elapsed_s`, `timer_s`, `distance_m`, `avg_speed_mps`, `max_speed_mps`, `avg_hr`, `max_hr`, `avg_power`,
`max_power`, `normalized_power`, `avg_cadence`, `avg_cadence_spm` (running), `ascent_m`, `descent_m`, `intensity`
(`active`, `rest`, `warmup`, `cooldown`, `recovery`, `interval`, `other`), `trigger` (`lap_trigger`), `wkt_step_index`,
`balance_right_pct`, `step_length_m` (`avg_step_length` mm ÷ 1000), `vertical_oscillation_mm`, `stance_time_ms`,
`stance_time_balance_pct`, `vertical_ratio_pct`, `avg_gap_mps`, `num_lengths`, `stroke`, `avg_swolf`.

**Pedal balance.** `left_right_balance` on laps and sessions is the `left_right_balance_100` type: bit 15 set = the
value refers to the right leg, low 14 bits = percent × 100. `garmin-fit-sdk` 21.218.0 does not expand it, so
`balance_right_pct = (value & 0x3FFF) / 100` when bit 15 is set; without that bit the side is unknown and the key is
omitted. A float already between 0 and 100 (a future SDK expanding the type) is taken as the right percentage.

### `intervals`

Work laps = `intensity == "active"`, rest laps = `rest` or `recovery`; present only with ≥ 2 work and ≥ 1 rest lap
(computed over every lap, also when `laps` was cut).

```jsonc
"intervals": {"work": {"n": 6, "avg_duration_s": 240.1, "avg_distance_m": 1001.3, "avg_hr": 169, "max_hr": 178,
                       "avg_power": 318, "avg_speed_mps": 4.17, "first_to_last_speed_pct": -2.1, "first_to_last_hr": 6},
              "rest": {"n": 5, "avg_duration_s": 120.4, "avg_hr": 141, "avg_speed_mps": 2.5},
              "lap_indexes": {"work": [2, 4, 6, 8, 10, 12], "rest": [3, 5, 7, 9, 11]}}
```

Averages are plain means of the lap values; `first_to_last_speed_pct = (last − first) / first × 100` on the work laps'
average speed, `first_to_last_hr = last − first` on their average HR.

### `dynamics`

One sub-object for the session's FIT sport (`running` → `run`, `cycling` → `bike`, `swimming` → `swim`; other sports:
omitted). Left/right pairs are `[left, right]` and appear only when both sides are present.

| Sub-object | Keys (FIT `session` fields) |
|---|---|
| `run` | `cadence_spm`, `step_length_m` (`avg_step_length` ÷ 1000), `vertical_oscillation_mm`, `vertical_ratio_pct`, `stance_time_ms`, `stance_time_balance_pct`, `gap_mps` |
| `bike` | `balance_right_pct` (`left_right_balance`), `torque_effectiveness_pct`, `pedal_smoothness_pct`, `time_standing_s`, `stand_count`, `pco_mm`, `power_phase_deg` and `power_phase_peak_deg` (`[[left start, left end], [right start, right end]]`) |
| `swim` | `swolf`, `stroke_distance_m`, `strokes_per_length`, `stroke`, `pool_length_m` |

### `records`

Aggregates of one pass over the `record` messages; the records themselves are not stored.

| Key | Meaning |
|---|---|
| `n`, `interval_s` | record count; median gap between timestamps (whole seconds) |
| `coverage_pct` | share of records having `hr`, `power`, `cadence`, `position` |
| `decoupling_pct` | Pw:HR (cycling with power) or Pa:HR (running, speed): `(first-half output:HR − second-half output:HR) / first half × 100`, halves split by timer time; only with timer ≥ 30 min and HR and output coverage ≥ 80 % |
| `power_zero_pct` | cycling: share of power records equal to 0 (coasting) |
| `power_source` | `record.power`, or `developer:<name>` when only a developer field named `Power` / `Running Power` carries it |

Pauses: records strictly inside a timer stop (`event = timer`, `stop*` … `start`) are excluded and the stop time is not
counted; without timer events a gap above twice the recording interval counts as a pause.

### `series`

The session's shape for "how did it unfold" questions: buckets of `bucket_s = max(60, ceil(timer_s / series_points))`
over `t_s = timestamp − session start` (bucket start, whole seconds); empty buckets (pauses) are skipped; at most
`fit.series_points` (48) buckets, widened when long pauses with records would exceed it. Each item: `t_s`, `hr`,
`power`, `speed_mps`, `cadence` (bucket means) and `alt_m`, `dist_m` (last value in the bucket).

### `route` and `device`

`route`: `session.start_position_*` / `end_position_*`, falling back to the first / last record with a position.
Never a track (D-075).

`device`: the `device_info` message with `device_index = creator` (else the first one, else `file_id`):
`manufacturer`, `product` (`garmin_product` name when known, else the number), `software_version`; `sensors` = the
distinct `antplus_device_type` / `ble_device_type` names of the other devices (D-077). Serial numbers and the
`user_profile` message (weight, height) are never copied.

## Caps

| What | Cap |
|---|---|
| ZIP member | 50 MB, else `parse_error` "member too large" |
| Laps | `fit.max_laps` (300), `meta.laps_truncated` |
| Series | `fit.series_points` (48) buckets |
| Summary | target ≤ 20 KB; hard cap 120 000 characters: drop `series`, then keep 100 laps, then drop `laps` (`meta.truncated`) |
| `get_activity` | laps only with `include_laps` (≤ 80, `laps_more: true` when cut), series only with `include_series`, the usual 60 000-character answer guard |

## Re-parse and re-download

The ZIP is the raw layer: a parser improvement never needs Garmin. After bumping `PARSER_VERSION`:

```sh
garmin-coach fit reparse            # rows of an older parser version or with parse_error
garmin-coach fit reparse --all      # every kept ZIP
garmin-coach fit reparse 1234 5678  # these activities
garmin-coach fit show 1234 [--json] # inspect one summary
```

There is no re-download command. To fetch one file again, delete its two rows by hand and run `fit backfill`:

```sql
DELETE FROM activity_fit WHERE activity_id = 1234;
DELETE FROM sync_state WHERE endpoint = 'activity.download' AND key = '1234';
```

## Disk use

A ZIP is ~0.05-1 MB (a short run is small, a 4-hour ride with 1 s recording near the top). For ~1 300 activities
expect 0.3-1 GB under `<app data>/fit/`; `status` and `doctor` show the folder size. `fit.keep_files: false` keeps
only the summaries (then `fit reparse` cannot help, and a parser fix needs a re-download).
