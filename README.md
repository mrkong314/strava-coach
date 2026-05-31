# strava-coach
Personal AI training pipeline. Pulls training data from Intervals.icu, asks
Claude for analysis, and writes reports into a Craft document.
## Schedules
- **Daily readiness check** - every morning (~6am Melbourne). `coach.py --mode daily`
- **Weekly training review** - Sunday evening (~7pm Melbourne). `coach.py --mode weekly`
Both also run on demand from the Actions tab (the `workflow_dispatch` trigger).
## Required secrets
Set under Settings -> Secrets and variables -> Actions:
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
