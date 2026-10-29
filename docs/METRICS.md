# Metrics

How `garmin-coach` derives its training metrics from the cached Garmin data (PLAN §7). Everything is computed
locally, deterministically, with no LLM involved. These metrics **approximate, they do not reproduce** Garmin's
proprietary Training Status, Training Readiness or Load Focus, which the Personal API does not expose.

The derived layer lives in the `metrics_daily` table. It is recomputed after every sync that stored new data, and
on demand when `context` finds that the underlying tables changed (a fingerprint of row counts and `updated_at`).

## Daily load

- **Per activity:** Garmin's `activityTrainingLoad` (EPOC-based) when present.
- **Fallback when it is missing** (older activities, some swims and walks): Banister TRIMP
  `TRIMP = minutes × HRr × a × e^(b × HRr)`, with `HRr = (avgHR − restHR) / (maxHR − restHR)` clipped to 0–1,
  `a = 0.64, b = 1.92` for men and `a = 0.86, b = 1.67` for women.
  - `restHR` is that day's resting HR (else the one Garmin uses for the zones, else 50).
  - `maxHR` is `athlete.yaml max_hr`, else Garmin's zone max.
  - TRIMP is multiplied by a **calibration factor**: the median of `load / TRIMP` over activities that have both
    (at least 10, clamped to 0.3–3), so the fallback speaks Garmin's units. On the owner's data: 1.09.
- **Per day:** sum over the day's activities (local start date). A day without activities is a real 0 (rest),
  not missing data. Multisport activities are a single row in the API (no children), so there is no double counting.
- An activity without load and without heart rate (for example a strength session without a strap) counts 0.

## ATL, CTL, TSB

Exponentially weighted moving averages of the daily load, one value per calendar day, starting at 0 on the first
day with an activity:

- `x_t = x_{t−1} + (load_t − x_{t−1}) × (1 − e^(−1/τ))`
- **ATL** (acute, "fatigue"): τ = 7 days. **CTL** (chronic, "fitness"): τ = 42 days.
- **TSB** (form) for day t = `CTL_{t−1} − ATL_{t−1}`: the form going *into* the day, so today's session does not
  change today's TSB.
- **Relative form** = `TSB / CTL_{t−1}`. It is comparable whatever the absolute load scale and is what the
  readiness score and the flags use (flag below −30%).

## ACWR

`acute:chronic workload ratio = sum of the last 7 days / (sum of the last 28 days / 4)` (rolling, uncoupled).
It is undefined with less than 28 days of history or when the 28-day load is 0. Flags: **> 1.5** (spike: injury risk
rises) and **< 0.8** (detraining, taper or a break).

## Monotony and strain (Foster)

Over the last 7 days: `monotony = mean / population standard deviation`, `strain = weekly load × monotony`.
Monotony is undefined when every day has the same load (standard deviation 0). Flag at **≥ 2.0**.

## Recovery

- **HRV:** Garmin's overnight average, its status and the baseline bounds of `HrvSummaryDTO.baseline`
  (`balancedLow`–`balancedUpper` = "normal range"). Derived: the number of *consecutive* most recent nights below
  the normal range, and the 7-night least-squares slope (ms/day).
- **Resting HR delta:** today's resting HR minus the 7-day average (Garmin's `lastSevenDaysAvgRestingHeartRate`,
  else the mean of the previous 7 days when at least 4 exist). Flag at **≥ +5 bpm**.
- **Sleep:** score, duration against Garmin's sleep need, stages, 7-night averages. Flags: score < 60, or more
  than 60 minutes below the need.
- **Body Battery:** value at wake time (only present in the single-day summary, so only for recent days), daily
  high/low. Flag below 50 at wake.

## Readiness (heuristic)

A 0–100 rule-based score, labelled everywhere as a heuristic. Component weights:

| Component | Weight | Points (fraction of the weight) |
|---|---:|---|
| HRV | 30 | status BALANCED 1.0, UNBALANCED 0.6, LOW 0.35, POOR 0.15; minus 0.25 after 1 night below the normal range, minus 0.5 after 3+ nights (Garmin's status follows the weekly average and can stay BALANCED while the last nights drop) |
| Sleep score | 25 | score / 100 |
| Body Battery at wake | 20 | value / 100 |
| Resting HR delta | 15 | ≤ 0: 1.0 · +1…+2: 0.8 · +3…+4: 0.5 · ≥ +5: 0.2 |
| Form (relative TSB) | 10 | ≥ −10%: 1.0 · ≥ −30%: 0.6 · ≥ −50%: 0.3 · below: 0 |

Missing components are dropped and the remaining weights rescaled; with less than 50 of the 100 weight points
available the score is not computed. The reasons (one line per component) and the missing components are shown
with the score. Labels: ≥ 80 high, ≥ 60 good, ≥ 40 reduced, below low.

**Known limit:** with a high chronic load, a single hard day moves ATL and TSB only a little, so the day after a race
can still score high. The daily brief therefore adds a separate flag when **yesterday's load is ≥ 2× the chronic
daily load** (CTL). Local or muscular pain reported by the athlete always outweighs the score (coaching rule).

## Fitness indicators (instead of VO2max)

The Personal API does not provide VO2max: `vO2MaxValue` is empty on every activity, and neither the activity detail
nor fitness age exposes it. Fitness trends therefore use **efficiency on steady aerobic sessions** (≥ 30 minutes,
average HR ≤ 90% of the sport's threshold HR, or ≤ 150 bpm if unknown):

- **Running:** metres per heartbeat = grade-adjusted speed (or average speed) × 60 / average HR.
- **Cycling:** watts per bpm = normalized power (or average power) / average HR.
- Indoor and outdoor are kept apart (treadmill and trainer numbers are not comparable with outdoor ones). Shown as
  the mean of the last 4 weeks against weeks 5–13, with the number of sessions.

Other indicators:
- **Mean-maximal power** (5 s, 1', 5', 20', 60'): best values over 90 days from Garmin's `maxAvgPower_*`, outdoor
  and indoor separately.
- **HR zone distribution:** share of time in Garmin's zones 1–5 from `hrTimeInZone_*`, over 7 and 28 days.
- **Thresholds:** LTHR per sport from Garmin's HR zones, the latest per-activity `lactateThresholdBpm` with its date,
  FTP from `athlete.yaml` and from Garmin's activity detail (`functionalThresholdPower`).

## Event phases

Days to each future event and a phase by weeks out: **base** ≥ 12 weeks, **build** 4–12, **peak** 2–4,
**taper** < 2, **race week** ≤ 7 days, **race day** 0. Each phase comes with a one-line generic focus; the coach adapts
it to the athlete's rules and constraints.
