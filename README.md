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
