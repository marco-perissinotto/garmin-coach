# Dashboard

A single local HTML page that shows the athlete's form and tells them something they would not see in Garmin
Connect: where they are improving, where they are slipping, what to focus on before the target event, what to keep an
eye on, and patterns from their own history. Built from the cache with the same metrics as the packs
(`docs/METRICS.md`), with no network and no LLM. Decisions: D-093.

## Use

| How | What happens |
|---|---|
| `garmin-coach dashboard --open` | Rebuilds the page from the cache and opens it in the default browser. `--refresh` first refreshes today's data from Garmin (tiers A-D, the daily brief's tiers). |
| `/coach dashboard` (Claude Code) or "aprimi la dashboard" (Claude Desktop) | The `open_dashboard` MCP tool: refreshes like the daily brief, rebuilds, opens the browser and returns the page's findings so the coach can discuss them. |
| After every sync | `App.post_run` rewrites the page when it already exists (`dashboard.auto_update`, default true). It is never created by a sync: the first `dashboard` call creates it. |

The page lives in `<app data>/dashboard/index.html`, per machine like the database: it holds health data and never
goes to the shared profile folder. It is self-contained (inline CSS, JS and data; no CDN, no fonts, no requests), so
it opens offline. The data is embedded as JSON with `<`, `>` and `&` escaped, so no text from Garmin (activity names)
can close the script block; the page inserts every text with `textContent`.

The page and the chat work together: each finding and each recent activity has a «Chiedi al coach» button that copies
a ready question to paste into the chat; `open_dashboard` returns the same findings to the coach.

## Page

1. **Header**: date, data age, period selector (3 months, 6 months, 1 year, 3 years: scopes every time chart and the
   weekly bars), theme (auto, light, dark; remembered in the browser).
2. **Hero**: chronic load (CTL) with its rank among all days of the history, the peaks of the last year and of all
   time; tiles for form (TSB / CTL), readiness, ACWR, days to the target event. A one-paragraph summary.
3. **Il punto**: four columns of findings (below) and the "Lo sapevi?" patterns.
4. **Target event**: CTL in the days before each past edition of the target event and the current season, aligned on
   race day; a table of the same point of each preparation (CTL; weekly hours per sport and run km over the previous 4
   weeks), CTL and TSB on race day, result.
5. **Load**: CTL and ATL; TSB (diverging); CTL per sport stacked (the four add up to the total CTL); weekly CTL over
   the whole history; training days, streak, monotony.
6. **Volume and intensity**: weekly hours per sport; weekly hours per intensity domain; the domain split of the last
   4 weeks against the 4 before; cumulative hours of this year against the previous two, and year-to-date totals.
7. **Disciplines**: run, bike and swim cards with the efficiency trend (each comparable session as a point, the
   42-day rolling median as a line), volume, decoupling, running dynamics, best times per year, best powers, FTP check,
   swim pace and swolf, strength sessions.
8. **Health and recovery**: ten wellness metrics (7-day mean against the 90 days before), HRV with Garmin's normal
   range, resting HR, sleep against need, bedtime regularity, sleep by night of the week.
9. **Plan and events**: the coaching state (plan, conditions, open decisions, symptoms) and the events with phase.
10. **Recent activities**: the last 14, with load, decoupling and the «Chiedi al coach» button.

Every chart has a table view, a legend when it has two series or more, a tooltip on hover and on keyboard focus.

## Numbers (`dashboard/data.py`)

| Item | Definition |
|---|---|
| Comparable session | `fitness.is_steady`: at least 30 min, average HR at most 90 % of the sport's threshold HR (Garmin's sport-specific LTHR, else `athlete.lthr`, else 90 % of max HR). Run efficiency uses outdoor runs only; bike efficiency keeps outdoor and indoor apart. |
| Efficiency | Run: metres per heartbeat, `(grade-adjusted or plain speed) × 60 / avg HR`. Bike: `(NP or avg power) / avg HR`. Swim: seconds per 100 m in pool sessions of at least 1 km. |
| Trend | Median of the last 42 days against the median of the 90 days before (and of the same weeks a year ago). A verdict needs at least 3 sessions in each window; within ±1.5 % it is "stable". |
| Intensity domains | Low below 89 % of the threshold HR (Friel's top of zone 2), high above the threshold HR, medium between. From the FIT `time_in_zone` with the threshold HR the device used that day; else from Garmin's five zone times with today's zone floors. A bin crossed by a threshold is split in proportion (`split_domains`). |
| CTL per sport | The usual 42-day EWMA over each sport's daily load (`compute.compute_loads`, Garmin load or calibrated TRIMP). |
| Target event | The next A-priority event, else B, else the next one. Past editions: activities of the same family within ±4 % of the distance (±15 % for multisport), more than 120 days old, and for single sports run at more than 80 % of max HR (a race, not a training run of the same length). The last three. The window is `days to go + 126` days, at most a year. |
| Best times | For each standard distance, the fastest average pace of an outdoor run at least that long, as time over that distance. For a shorter distance inside a longer race this is a lower bound; the "record" finding only uses runs within 110 % of the distance. |
| FTP check | Best 20-minute power of the last 90 days × 0.95, against `athlete.ftp`. |
| Health metrics | 7-day mean against the mean of the 90 days before; out of the usual when the difference exceeds 0.75 daily standard deviations; whether that is good or bad depends on the metric (HRV up good, resting HR up bad, steps neither). Daily totals (stress, Body Battery high, steps) leave out today, which is still partial. |
| Illness signal | Resting HR at least 4 bpm above its 60-day mean and HRV below Garmin's normal range, both today and yesterday. |
| Patterns | HRV the night after the hardest 20 % of training days and after rest days, against the 7 nights before (last 365 days, at least 8 cases each); sleep after sessions ending after 19:30 against before 17:00; sleep by night of the week (last 182 days). Associations with their counts, not causes. |

## Findings (`dashboard/insights.py`)

Rule-based sentences in Italian (the owner's `advice_language`), each quoting its numbers and the number of sessions
or nights behind it; with fewer than 4 sessions a trend says "da confermare" and weighs half. Each finding has a
weight; the page shows the four heaviest per group.

| Group | Findings |
|---|---|
| Dove vai forte | an efficiency trend up (run, bike outdoor/indoor, swim); a personal record at a race distance in the last 120 days; running dynamics better (cadence +3, ground contact −4 ms, vertical ratio −0.2); bike decoupling down by 2 points; CTL up 6 % in 4 weeks or 10 % over a year; CTL above every past edition at the same point; a health metric better than usual; at least 22 training days in 28. |
| Dove stai calando | the same trends down; best 20 and 60 min power below 92 % of the year's best; CTL down 6 % in 4 weeks (with a note after a race) or 10 % over a year; CTL below every past edition; run decoupling above 5 % and up 2 points. |
| Su cosa concentrarti | the discipline furthest below the past seasons at the same point (weekly hours below 85 %, weighted by its share of a triathlon); FTP estimate below 93 % of the set FTP; medium intensity above 20 % in base or build; no strength session in 4 weeks for a multisport target; bedtime varying by ±45 min or more. |
| Da tenere d'occhio | the illness signal; ACWR above 1.5; monotony 2 or more; run volume up 30 % in a week (from 20 km); a health metric worse than usual; sleep debt of 4 h or more over 14 nights; every symptom in the coaching state. |

The summary names the CTL and its rank, how fresh the athlete is, the area of the heaviest strength, the area of the
heaviest focus (else decline) and the first "watch" item.

## Tests

`tests/test_dashboard.py`: domain split, trend, number formatting, the findings on synthetic payloads, the payload and
the page against the fake account (escaping of untrusted text, no external resources), the page's script parsed by
`node --check` when Node is installed, the after-sync rule, the coaching state on the page and the MCP tool.
