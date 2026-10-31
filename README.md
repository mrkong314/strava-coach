# strava-coach
Personal training-data pipeline. Pulls activities, wellness, laps and
per-second stream metrics from Intervals.icu, and completed gym workouts
from Hevy, and writes them into a Craft "Training Log" document as managed
JSON blocks. Pushes planned workouts from a Google Calendar out to
Intervals.icu and Hevy.

No LLM runs in the pipeline. All analysis happens in chat via the Coach
Claude skills, which read the Training Log through the `log_index` block.

## Scripts and workflows

| Script | Workflow | Trigger | Writes |
| --- | --- | --- | --- |
| `coach.py --mode sync` | Sync Training Data | `workflow_dispatch`, hit by cron-job.org (30 min) | `detail_activity`, `detail_wellness`, `detail_laps` |
| `coach.py --mode rollup` | Weekly Rollup | cron 20:00 UTC Sunday (Monday morning Melbourne) and manual | `weekly`, `events` |
| `metrics.py` | Stream metrics | `workflow_dispatch`, hit by cron-job.org | `detail_metrics` |
| `hevy_sync.py --mode pull` | Hevy Sync | `workflow_dispatch`, hit by cron-job.org (30 min) | `detail_gym` |
| `hevy_sync.py --mode push` | Hevy Sync | manual, once per block | Hevy routines |
| `scripts/push_workouts.py` | Push planned workouts to intervals.icu | `workflow_dispatch`, hit by cron-job.org (hourly) and manual | Intervals.icu planned workouts |
| `scripts/cleanup_workouts.py` | Cleanup intervals.icu planned workouts | manual, dry run by default | deletes owned Intervals.icu workouts |
| `seed_pbs.py` | Seed PBs | manual, one-time | `pb_seed` |

Every workflow can be run from the Actions tab. The ones marked cron-job.org
have no `schedule:` of their own; cron-job.org POSTs to the workflow
dispatch endpoint, for example with body
`{"ref":"main","inputs":{"mode":"pull"}}` for the Hevy pull. The intervals
quoted are the intended cadence; the actual schedule lives in cron-job.org.

## Required secrets
Set under Settings -> Secrets and variables -> Actions:

| Secret | Used by |
| --- | --- |
| `INTERVALS_ATHLETE_ID` (e.g. `i588094`) | coach, metrics, push, cleanup, seed |
| `INTERVALS_API_KEY` | coach, metrics, push, cleanup, seed |
| `CRAFT_API_BASE` (e.g. `https://connect.craft.do/links/XXXX/api/v1`) | coach, metrics, hevy pull, seed |
| `HEVY_API_KEY` | hevy pull and push |
| `GOOGLE_CREDENTIALS_JSON` (service-account JSON, calendar read) | hevy push, push_workouts |
| `GCAL_CALENDAR_ID` (the Workout Planning calendar) | hevy push, push_workouts |
| `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, `STRAVA_REFRESH_TOKEN` | seed_pbs only (optional) |

The Craft connect link reaches only the Training Log document; the scripts
take the first live document on the connection.

## Training Log structure (`coach.py`)
Craft caps a block at 10,000 characters, so each logical section is stored
across as many fenced-JSON sub-blocks as needed. Every managed block carries
`_section`, `_part` (0-based), `_updated` and `items`.

| Section | Lifecycle | Content |
| --- | --- | --- |
| `detail_activity` | rolling 90 days, rewritten each sync | trimmed Intervals.icu activity summaries |
| `detail_wellness` | rolling 90 days, rewritten each sync | CTL/ATL/form, HR, HRV, sleep, weight, eFTP |
| `detail_laps` | 90 days, fetched once per activity | lap rows tagged `aid` and `n`, for runs/rides/swims of 20 min or more |
| `detail_metrics` | 90 days, computed once per activity | stream metrics (see below) |
| `detail_gym` | all history, merged each pull | Hevy workouts (see Hevy section) |
| `weekly` | append-only, one item per completed week | per-sport sessions, minutes, load, km, avg HR, pace |
| `events` | append-only | races, FTP tests, 40 km+ runs, 200 km+ rides, 4 km+ swims |
| `records` | seeded once from `history_seed.json` | PB and FTP progression |
| `athlete_constants` | maintained by Claude in chat | LTHR, fallback FTP, W' read by `metrics.py` |
| `pb_seed` | written by `seed_pbs.py` | all-time bars for the Current PBs collection |
| `log_index` | rewritten after every write | part-block ids per section with date ranges, plus a hash of today's content |

On first run, if the document has no managed blocks, `sync` seeds `events`
and `records` from `history_seed.json`. Activities the Intervals.icu API
returns as stubs (id and start time only, typically Strava-sourced indoor
rides) are dropped everywhere.

### Write safety
Sections are rewritten post-then-delete: the new part-blocks are posted
first and the old ids deleted afterwards. A failed post leaves the previous
blocks intact (this matters for the append-only `weekly` and `events`
sections). A failed delete leaves old and new parts side by side until the
next write, which captures and removes both. Writes are not retried, so a
retry after a gateway error cannot duplicate blocks.

### Timezone
All scripts use `zoneinfo.ZoneInfo("Australia/Melbourne")`, so "today",
week boundaries and `_updated` stamps follow AEST/AEDT. `push_workouts.py`
takes the zone from `TZID` (default `Australia/Melbourne`) for both its
notion of today and the calendar window; GitHub runners are UTC, so without
this the window started a day early for half of each Melbourne day.

## Stream metrics (`metrics.py`, `stream_core.py`)
For every Run, Ride or VirtualRide of 15 min or more in the 90-day window
with no `detail_metrics` entry yet, fetch the per-second streams and compute
power and HR histograms, surges, EF drift (lag-corrected), half-split
decoupling, HRR60, W'bal minimum, climb/flat NP and uphill/downhill run
splits. `stream_core.py` is pure stdlib and is shared verbatim with the
today-brief skill. Constants come from the Log's `athlete_constants` block;
`LTHR_BIKE`, `LTHR_RUN` and `WPRIME_J` in the workflow env are fallbacks
only. `METRICS_MAX_NEW` (default 60) bounds new activities per run so the
initial backfill spreads over a few cron cycles.

## Planned workouts to Intervals.icu (`scripts/push_workouts.py`)
Windowed upsert from the Workout Planning calendar into Intervals.icu over
today to today + `WINDOW_DAYS` (default 14). An event is pushed when it is
future-dated and its description is in planned format: a `gym` event with
an exercise list, or an endurance description whose first line starts with
a discipline or step header (`Ride`, `Run`, `Swim`, `Warmup`, `Threshold`,
...). Reconciled prose (two or more of `ACTUAL`, `PLANNED`, `WELLNESS` in
the head) is skipped.

Each Intervals.icu workout is keyed by `external_id = wkplan-<sha1 of the
Google event id>`, so the script only ever creates, updates, dedupes or
deletes events it owns. Workout type comes from the first discipline word in
the description's `Name:` line, then the calendar summary. Duration is
summed from step lines (`- 8m 95%`, with `3x` repeat headers).
`DRY_RUN=1` logs without writing.

`scripts/cleanup_workouts.py` deletes every owned workout in a forward
window so a fixed push can recreate them. It defaults to dry run; the
workflow shares the `push-workouts` concurrency group so it never runs
alongside a push.

## PB seeding (`seed_pbs.py`)
One-time, re-runnable scan of full history. Longest run/ride/swim and most
climbing come from Intervals.icu summaries, power PBs from the Intervals
power curve when available, and run distance-split PBs from Strava
`best_efforts` when Strava credentials are set. Writes a `pb_seed` section;
the final upsert into the Current PBs collection is done in a Coach Claude
chat.

## Hevy gym sync (`hevy_sync.py`)
Separate pipeline for strength training, independent of `coach.py`. Two modes:

- **pull** - reads completed workouts from Hevy and writes them into the Craft
  Training Log as a `detail_gym` JSON section (same chunked-codeblock mechanism
  `coach.py` uses). `date` is the Melbourne local date of the start time (Hevy
  reports UTC). Each item carries `routine_id` when the workout was started
  from a routine. Dedup by workout id; only writes on new/changed data, so it
  is safe on a 30-min cron. `hevy_sync.py --mode pull`
- **push** - reads gym events on the Workout Planning calendar (today to 28
  days ahead) whose description holds a `[HEVY-ROUTINE]` block and keeps one
  Hevy routine per gym day up to date with the next uncompleted session (see
  Routine slots below). Runs on the cron; `--dry-run` logs the slot decisions
  and writes nothing. `hevy_sync.py --mode push [--dry-run]`

Runs from the Actions tab (the `Hevy Sync` workflow, `workflow_dispatch` with
`mode` and `dry_run` inputs) and via cron-job.org (POST to the workflow
dispatch endpoint with body `{"ref":"main","inputs":{"mode":"pull"}}` or
`{"mode":"push"}`).

### Routine slots (why old routines no longer pile up)
Hevy's public API has no DELETE for routines or routine folders, and
`folder_id` is rejected on update, so a routine can never be removed or moved
once created. Creating one routine per dated session therefore left every past
session in the app forever. The push instead keeps **one routine object per gym
day**, a slot (`day1`, `day2`, ...), identified by `Day N` in its title (a
`[wkplan-slot day1] <title>` marker is also written to the routine notes, but
Hevy's routine GET does not return routine-level notes, so the title is what
identifies a slot; keep hand-made routine titles free of `Day N`). If several
routines claim one slot the oldest is used and the others are logged for manual
deletion. Each run points every slot at the earliest
planned session for that day, dated today or later, that has not been logged.
"Logged" means a Hevy workout in the last 21 days started from that slot's
routine (`routine_id`) on the session's date, or, for the transition from the
old scheme, a workout whose title equals the session title on that date.

The slot's title and exercises are rewritten in place, so the app still shows
the full dated title (`26.11.03 Gym Lower - Day 1 (W4 deload)`) and the workout
started from it carries that title into `detail_gym`; only the routine count
stays fixed. The slot is derived from `Day N` in the `title=` line; a block
whose title has no `Day N` is skipped and logged. When every planned session in
the window is logged the slot is left as is. Routines are only written when
their content differs from what Hevy holds. Routines whose title has no `Day N`
are never touched; stale ones are deleted by hand in the app, and the push never
recreates them.

### Additional secrets for Hevy sync
- `HEVY_API_KEY` - Hevy Pro API key (hevy.com/settings?developer)
- `GOOGLE_CREDENTIALS_JSON` - service-account JSON for calendar read (push)
- `GCAL_CALENDAR_ID` - the Workout Planning calendar id (push)

`CRAFT_API_BASE` (above) is reused by the pull. Dependencies are in
`requirements-hevy.txt`. Exercise-id reference: the Craft doc "Hevy Exercise
IDs"; the script's `ID_TO_NAME` / `REPS_ONLY_IDS` hold the same library.

### `[HEVY-ROUTINE]` line format
One exercise per line, pipe-delimited:

	template_id | name | sets | reps | weight_kg | rest_s | note

- `sets`, `reps`, `rest_s` are integers; lines that fail to parse those are
  skipped. `weight_kg` is a float, or blank for bodyweight lifts (and is omitted
  for any `template_id` in `REPS_ONLY_IDS`). `note` is optional; a literal `@`
  in a note is stripped because it 400s in Hevy notes.
- A `title=...` line sets the routine title. `Day N` in it selects the slot.

### Hevy create vs update asymmetry (important)
The Hevy routine `POST` (create) and `PUT` (update) endpoints do **not** accept
the same body. Fields that are fine on create are rejected on update. Known so
far, both handled in the code:

- `routine.notes` must be non-empty on `PUT` ("not allowed to be empty").
  The push sets `notes` to the slot marker plus the title.
- `routine.folder_id` is rejected entirely on `PUT` ("not allowed"). `run_push`
  strips `folder_id` from the body before the `PUT`; the `POST` path keeps it.

Because a 400 only reports the **first** offending field, a re-push can surface
a new `"<field> is not allowed"` after an earlier one is fixed. The fix is the
same shape each time: strip that field on the `PUT` path in `run_push`.
`hevy_put` prints the response body on a non-2xx (\`PUT <path> -\> <status>:
<body>\`), so the rejected field shows up in the Actions log - keep that line.

### Supersets
Supersets are encoded in the `[HEVY-ROUTINE]` block by **rest pattern**, not a
dedicated field: short `rest_s` (~15) on the first lift of a pair, full recovery
`rest_s` on the pair-closing lift. This makes the session run to its intended
duration. It does **not** group the lifts in the Hevy app - the push does not
set Hevy's `superset_id`, so paired lifts render as a flat ordered list. To get
true grouped supersets would require adding `superset_id` support: an extra
field (or group tag) in the line format plus mapping in `parse_hevy_block`. Not
implemented; rest-pattern encoding is the current approach.

### Warmup ramps
Heavy barbell compounds (`RAMP_IDS`: squat/deadlift variants, barbell bench,
incline bench, OHP, barbell RDL) get a warmup ramp on push: 2 sets at ~60% and
~80% of the top weight, rounded to 2.5 kg, typed as Hevy `"warmup"`. The warmups
come **out of** the prescribed set count, not on top: a lift written as N sets
becomes 2 warmup + (N-2) working, clamped to at least 1 working set (so a 2-set
lift gives 1 warmup + 1 working). Prescribe set counts inclusive of the warmups.
Accessories, dumbbell/machine variants and reps-only lifts do not ramp. Adjust
which lifts ramp via `RAMP_IDS`, the ladder via `RAMP_FRACTIONS`, rounding via
`_round_2p5`. Note: Hevy rest is per-exercise, so a ramp lift that also leads a
superset shares the short intra-pair rest on its warmups - harmless as warmups
are light.
