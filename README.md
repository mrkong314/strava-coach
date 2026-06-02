# strava-coach
Personal AI training pipeline. Pulls training data from Intervals.icu, asks
Claude for analysis, and writes reports into a Craft document.

## Schedules
- **Daily readiness check** - every morning (~6am Melbourne). `coach.py --mode daily`
- **Weekly training review** - Sunday evening (~7pm Melbourne). `coach.py --mode weekly`

Both also run on demand from the Actions tab (the `workflow_dispatch` trigger).

## Required secrets
Set under Settings -\> Secrets and variables -\> Actions:
- `INTERVALS_ATHLETE_ID` - e.g. `i588094`
- `INTERVALS_API_KEY` - Intervals.icu API key
- `ANTHROPIC_API_KEY` - Anthropic API key
- `CRAFT_API_BASE` - Craft API base URL, e.g. `https://connect.craft.do/links/XXXX/api/v1`

## Editing the coaching context
Athlete goals live in the `ATHLETE_CONTEXT` string near the top of `coach.py`.
Edit it freely as goals change - Claude reads it verbatim.

## Hevy gym sync (`hevy_sync.py`)
Separate pipeline for strength training, independent of `coach.py`. Two modes:

- **pull** - reads completed workouts from Hevy and writes them into the Craft
  Training Log as a `detail_gym` JSON section (same chunked-codeblock mechanism
  `coach.py` uses). Dedup by workout id; only writes on new/changed data, so it
  is safe on a 30-min cron. `hevy_sync.py --mode pull`
- **push** - reads gym events on the Workout Planning calendar whose description
  holds a `[HEVY-ROUTINE]` block and creates/updates those routines in Hevy.
  Run once per block from the Actions tab. `hevy_sync.py --mode push`

Runs from the Actions tab (the `Hevy Sync` workflow, `workflow_dispatch` with a
`mode` input) and via cron-job.org for the 30-min pull (POST to the workflow
dispatch endpoint with body `{"ref":"main","inputs":{"mode":"pull"}}`).

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
- A `title=...` line sets the routine title (used as the find-or-update key).

### Hevy create vs update asymmetry (important)
The Hevy routine `POST` (create) and `PUT` (update) endpoints do **not** accept
the same body. Fields that are fine on create are rejected on update. Known so
far, both handled in the code:

- `routine.notes` must be non-empty on `PUT` ("not allowed to be empty").
  `parse_hevy_block` defaults `notes` to the routine title.
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
~80% of the top weight, rounded to 2.5 kg, typed as Hevy `"warmup"` sets so they
do not count as working volume. The prescribed working sets are unchanged.
Accessories, dumbbell/machine variants and reps-only lifts do not ramp. Adjust
which lifts ramp via `RAMP_IDS`; adjust the ladder via `RAMP_FRACTIONS`. Note:
Hevy rest is per-exercise, so a ramp lift that also leads a superset shares the
short intra-pair rest on its warmups - harmless as warmups are light.
