-- garmin-coach cache schema v1 (PLAN §5.2). Deviations from the plan are logged in docs/DECISIONS.md.

-- raw layer: every successful response, verbatim. Never deleted. Lets us re-normalize without re-polling.
CREATE TABLE IF NOT EXISTS raw_payload (
  id INTEGER PRIMARY KEY,
  endpoint TEXT NOT NULL,            -- e.g. 'sleep.daily'
  key TEXT NOT NULL,                 -- natural key: date, date range, or activityId
  fetched_at TEXT NOT NULL,          -- ISO8601 UTC, first time this exact body was received
  last_seen_at TEXT NOT NULL,        -- ISO8601 UTC, last time this exact body was received (normalization order)
  content_hash TEXT NOT NULL,        -- sha256 of body; identical body => no new row
  body TEXT NOT NULL,                -- JSON text
  UNIQUE(endpoint, key, content_hash)
);
CREATE INDEX IF NOT EXISTS ix_raw_endpoint_key ON raw_payload(endpoint, key);
CREATE INDEX IF NOT EXISTS ix_raw_seen ON raw_payload(last_seen_at, id);

CREATE TABLE IF NOT EXISTS sync_state (
  endpoint TEXT NOT NULL, key TEXT NOT NULL, last_fetched_at TEXT NOT NULL, last_status INTEGER, note TEXT,
  PRIMARY KEY(endpoint, key)
);

CREATE TABLE IF NOT EXISTS endpoint_cap (     -- window caps learned from HTTP 400 (PLAN §6.3)
  endpoint TEXT PRIMARY KEY, cap_days INTEGER NOT NULL, learned_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tier_state (
  tier TEXT PRIMARY KEY,
  last_completed_at TEXT,
  last_attempt_at TEXT, last_error TEXT, cooldown_until TEXT
);

CREATE TABLE IF NOT EXISTS job (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,                -- 'backfill' | 'refresh'
  trigger TEXT NOT NULL,             -- 'startup' | 'mcp' | 'cli' | 'gate'
  tiers TEXT NOT NULL,
  status TEXT NOT NULL,              -- 'queued' | 'running' | 'done' | 'partial' | 'failed' | 'cancelled'
  pid INTEGER, host TEXT,
  queued_at TEXT, started_at TEXT, finished_at TEXT, heartbeat_at TEXT,
  calls_planned INTEGER DEFAULT 0, calls_done INTEGER DEFAULT 0, calls_failed INTEGER DEFAULT 0,
  current_step TEXT,
  error TEXT, notes TEXT,
  force INTEGER DEFAULT 0,
  cancel_requested INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_job_status ON job(status);

CREATE TABLE IF NOT EXISTS sync_run (id INTEGER PRIMARY KEY, job_id INTEGER, started_at TEXT, finished_at TEXT,
  trigger TEXT, tiers TEXT, calls INTEGER, errors INTEGER, notes TEXT);

-- normalized layer
CREATE TABLE IF NOT EXISTS activity (
  activity_id INTEGER PRIMARY KEY, start_local TEXT, start_gmt TEXT, type_key TEXT, name TEXT,
  distance_m REAL, duration_s REAL, moving_s REAL, elev_gain_m REAL, avg_hr INTEGER, max_hr INTEGER,
  avg_power REAL, norm_power REAL, aerobic_te REAL, anaerobic_te REAL, te_label TEXT,
  training_load REAL, vo2max REAL, lthr INTEGER, ftp INTEGER, avg_speed_mps REAL,
  calories REAL, body_battery_diff INTEGER, has_detail INTEGER DEFAULT 0, has_fit INTEGER DEFAULT 0,
  summary_hash TEXT,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_activity_start ON activity(start_local);

CREATE TABLE IF NOT EXISTS daily (
  date TEXT PRIMARY KEY,
  steps INTEGER, distance_m REAL, active_kcal REAL, total_kcal REAL,
  rhr INTEGER, rhr_7d_avg INTEGER, hr_min INTEGER, hr_max INTEGER,
  stress_avg INTEGER, stress_high_s INTEGER, stress_med_s INTEGER, stress_low_s INTEGER, stress_rest_s INTEGER,
  bb_high INTEGER, bb_low INTEGER, bb_at_wake INTEGER, bb_charged INTEGER, bb_drained INTEGER,
  intensity_min_moderate INTEGER, intensity_min_vigorous INTEGER,
  spo2_avg REAL, respiration_avg REAL, hydration_ml REAL, updated_at TEXT
);

CREATE TABLE IF NOT EXISTS sleep (
  date TEXT PRIMARY KEY, start_local TEXT, end_local TEXT, total_s INTEGER, deep_s INTEGER, light_s INTEGER,
  rem_s INTEGER, awake_s INTEGER, awake_count INTEGER, score INTEGER, score_quality TEXT, avg_hr INTEGER,
  avg_spo2 REAL, lowest_spo2 REAL, avg_respiration REAL, avg_stress REAL, nap_s INTEGER, sleep_need_min INTEGER,
  feedback TEXT, updated_at TEXT
);

-- Baseline columns follow HrvSummaryDTO.baseline (BaselineScoreDTO): lowUpper, balancedLow, balancedUpper, markerValue.
CREATE TABLE IF NOT EXISTS hrv (
  date TEXT PRIMARY KEY, last_night_avg INTEGER, last_night_5min_high INTEGER, weekly_avg INTEGER,
  baseline_low_upper INTEGER, baseline_balanced_low INTEGER, baseline_balanced_upper INTEGER,
  baseline_marker REAL, status TEXT, feedback TEXT, updated_at TEXT
);

CREATE TABLE IF NOT EXISTS weight (
  ts_gmt TEXT PRIMARY KEY, date TEXT, weight_g REAL, bmi REAL, body_fat_pct REAL, muscle_mass_g REAL,
  body_water_pct REAL, source TEXT
);

CREATE TABLE IF NOT EXISTS profile_snapshot (
  taken_at TEXT NOT NULL, kind TEXT NOT NULL, body TEXT,  -- 'hr_zones' | 'settings' | 'fitness_age' | 'weight_goal'
  PRIMARY KEY(taken_at, kind)
);

-- derived layer (Phase 2), cheap to drop and rebuild.
CREATE TABLE IF NOT EXISTS metrics_daily (
  date TEXT PRIMARY KEY, load REAL, atl REAL, ctl REAL, tsb REAL, acwr REAL, monotony REAL, strain REAL,
  readiness_score INTEGER, readiness_reasons TEXT, vo2max_run REAL, vo2max_bike REAL
);
