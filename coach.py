#!/usr/bin/env python3
"""
coach.py - Intervals.icu + Claude personal coaching pipeline.

Modes:
  python coach.py --mode daily    Short morning readiness check.
  python coach.py --mode weekly   Full weekly training review.

Reads training data from Intervals.icu, asks Claude for analysis, and appends
the result to a Craft document.

Credentials come from environment variables (set as GitHub Actions secrets):
  INTERVALS_ATHLETE_ID   e.g. i588094
  INTERVALS_API_KEY      Intervals.icu API key
  ANTHROPIC_API_KEY      Anthropic API key
  CRAFT_API_BASE         e.g. https://connect.craft.do/links/XXXX/api/v1
"""

import os
import json
import argparse
import datetime as dt

import requests
import anthropic

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

INTERVALS_ATHLETE_ID = os.environ["INTERVALS_ATHLETE_ID"]
INTERVALS_API_KEY    = os.environ["INTERVALS_API_KEY"]
ANTHROPIC_API_KEY    = os.environ["ANTHROPIC_API_KEY"]
CRAFT_API_BASE       = os.environ["CRAFT_API_BASE"].rstrip("/")

INTERVALS_BASE = "https://intervals.icu/api/v1"

# Claude models. Daily uses the cheaper Haiku; weekly uses Sonnet.
MODEL_DAILY  = "claude-haiku-4-5-20251001"
MODEL_WEEKLY = "claude-sonnet-4-6"

# Melbourne is UTC+10 (AEST) / UTC+11 (AEDT). +10 is fine for working out the
# local calendar date because the jobs run mid-morning and evening, never near
# local midnight, so daylight saving never shifts the date.
MELBOURNE = dt.timezone(dt.timedelta(hours=10))

# --- Athlete context -------------------------------------------------------
# Edit this freely as goals change. Claude reads it verbatim.
ATHLETE_CONTEXT = """\
Athlete: recreational multi-sport endurance athlete based in Melbourne, Australia.
Trains across running, cycling, swimming and gym, plus occasional basketball,
hiking and badminton.

Current focus and goals:
- Running: improving for cross country.
- Cycling: building FTP. Currently around 246 W, target 270 W by end of 2026.
- Three Peaks Challenge (cycling) planned for next year.
- IRONMAN 70.3 Geelong (half distance) on the calendar.
- IRONMAN Thun, Switzerland (full 140.6 distance). This will be the athlete's
  second full IRONMAN; the first was Vitoria-Gasteiz in 2025.

Coaching approach: evidence-based, polarised where appropriate, mindful of
balancing three endurance disciplines plus strength work without overreaching.
"""

# --------------------------------------------------------------------------
# Intervals.icu
# --------------------------------------------------------------------------

def _iget(path, params=None):
    r = requests.get(
        f"{INTERVALS_BASE}{path}",
        params=params or {},
        auth=("API_KEY", INTERVALS_API_KEY),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def melbourne_today():
    return dt.datetime.now(MELBOURNE).date()


def get_wellness(days):
    today = melbourne_today()
    oldest = today - dt.timedelta(days=days)
    records = _iget(
        f"/athlete/{INTERVALS_ATHLETE_ID}/wellness",
        {"oldest": oldest.isoformat(), "newest": today.isoformat()},
    )
    keep = ["id", "ctl", "atl", "rampRate", "restingHR", "hrv", "hrvSDNN",
            "sleepSecs", "sleepScore", "sleepQuality", "weight", "readiness",
            "vo2max", "steps"]
    out = []
    for w in records:
        row = {k: w.get(k) for k in keep if w.get(k) is not None}
        if "ctl" in row and "atl" in row:
            row["form"] = round(row["ctl"] - row["atl"], 1)
        if row.get("sleepSecs"):
            row["sleepHours"] = round(row["sleepSecs"] / 3600, 1)
        out.append(row)
    return out


def get_activities(days):
    today = melbourne_today()
    oldest = today - dt.timedelta(days=days)
    acts = _iget(
        f"/athlete/{INTERVALS_ATHLETE_ID}/activities",
        {"oldest": oldest.isoformat(), "newest": today.isoformat()},
    )
    keep = ["name", "type", "start_date_local", "moving_time", "distance",
            "total_elevation_gain", "icu_training_load", "icu_intensity",
            "average_heartrate", "max_heartrate", "icu_average_watts",
            "icu_weighted_avg_watts", "icu_ftp", "icu_rpe", "feel",
            "average_speed"]
    out = []
    for a in acts:
        row = {k: a.get(k) for k in keep if a.get(k) is not None}
        if row.get("moving_time"):
            row["minutes"] = round(row["moving_time"] / 60)
        if row.get("distance"):
            row["km"] = round(row["distance"] / 1000, 2)
        out.append(row)
    return out


def get_events(start, end):
    return _iget(
        f"/athlete/{INTERVALS_ATHLETE_ID}/events",
        {"oldest": start.isoformat(), "newest": end.isoformat()},
    )


# --------------------------------------------------------------------------
# Claude
# --------------------------------------------------------------------------

def call_claude(model, max_tokens, system, user_content):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()


# --------------------------------------------------------------------------
# Craft
# --------------------------------------------------------------------------

def craft_document_id():
    r = requests.get(f"{CRAFT_API_BASE}/documents", timeout=30)
    r.raise_for_status()
    items = r.json().get("items", [])
    live = [d for d in items if not d.get("isDeleted")]
    if not live:
        raise RuntimeError("No documents available on the Craft connection.")
    return live[0]["id"]


def craft_append(markdown):
    doc_id = craft_document_id()
    r = requests.post(
        f"{CRAFT_API_BASE}/blocks",
        json={"markdown": markdown,
              "position": {"position": "start", "pageId": doc_id}},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------

DAILY_SYSTEM = f"""You are a personal endurance coach writing a short daily \
readiness check. Be terse, specific and quantitative. No filler, no pep-talk.

{ATHLETE_CONTEXT}

You will receive recent daily wellness records (most recent last) and any \
workouts planned for today.

Write at most six short lines, in this shape:
- Readiness: one word (Green / Amber / Red) plus the single most important \
reason, citing a number (e.g. "HRV 48, down 18% on the 7-day average").
- Today: the planned session, or "nothing scheduled".
- Adjustment: one concrete recommendation only if readiness is Amber or Red; \
otherwise "proceed as planned".
- One optional extra line only if something genuinely warrants it.

Never invent data. If wellness data is missing, say so plainly."""


WEEKLY_SYSTEM = f"""You are a personal endurance coach writing a weekly training \
review. Be specific, quantitative and honest. Cite real numbers from the data. \
No filler.

{ATHLETE_CONTEXT}

You will receive 28 days of daily wellness records, 14 days of completed \
activities, and any upcoming races/events on the calendar.

Write the review in exactly these seven sections, using Markdown headings:

## 1. Snapshot
One or two lines: current CTL (fitness), ATL (fatigue) and Form/TSB with trend \
direction, plus the week's total training hours and load.

## 2. The week
Sessions grouped by sport (run / ride / swim / gym / other) with time and load \
for each. Note the standout session and anything missed.

## 3. Training load and form
Comment on CTL ramp rate. Flag explicitly if it is climbing faster than about \
5-7 CTL points per week (injury risk). State where Form/TSB is trending.

## 4. Intensity and balance
Comment on the easy/hard distribution and whether any discipline is being \
neglected relative to the athlete's events. Swim volume is the usual casualty \
- call it out if low.

## 5. Wellness watch
HRV, sleep and resting HR trend. Flag a sustained decline as Amber. If the \
wellness data is sparse or missing, say so.

## 6. Goal tracking
Cycling FTP progress toward 270 W (use icu_ftp from recent rides as the current \
value). Running progression for cross country. Reference upcoming events if any.

## 7. Flags and focus
Two or three concrete things for the coming week, ending with one specific \
topic worth raising in a coaching conversation.

Keep the whole review under 600 words. Never invent data."""


def build_daily_prompt(wellness, today_events):
    workouts = [e for e in today_events if e.get("category") == "WORKOUT"]
    return (
        f"Date: {melbourne_today():%A %d %B %Y} (Melbourne).\n\n"
        f"Recent wellness (oldest first):\n{json.dumps(wellness, indent=2)}\n\n"
        f"Workouts planned for today:\n"
        f"{json.dumps(workouts, indent=2, default=str)}"
    )


def build_weekly_prompt(wellness, activities, upcoming_events):
    races = [e for e in upcoming_events
             if e.get("category") in ("RACE", "TARGET")]
    return (
        f"Week ending: {melbourne_today():%A %d %B %Y} (Melbourne).\n\n"
        f"28-day wellness (oldest first):\n{json.dumps(wellness, indent=2)}\n\n"
        f"14-day completed activities:\n"
        f"{json.dumps(activities, indent=2, default=str)}\n\n"
        f"Upcoming calendar races/targets:\n"
        f"{json.dumps(races, indent=2, default=str)}"
    )


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------

def run_daily():
    today = melbourne_today()
    wellness = get_wellness(10)
    events = get_events(today, today)
    text = call_claude(MODEL_DAILY, 600, DAILY_SYSTEM,
                        build_daily_prompt(wellness, events))
    md = f"# Daily Check - {today:%a %d %b %Y}\n\n{text}\n\n---\n"
    craft_append(md)
    print(f"Daily check posted for {today}.")


def run_weekly():
    today = melbourne_today()
    wellness = get_wellness(28)
    activities = get_activities(14)
    events = get_events(today, today + dt.timedelta(days=180))
    text = call_claude(MODEL_WEEKLY, 2000, WEEKLY_SYSTEM,
                       build_weekly_prompt(wellness, activities, events))
    md = f"# Weekly Review - week ending {today:%d %b %Y}\n\n{text}\n\n---\n"
    craft_append(md)
    print(f"Weekly review posted for week ending {today}.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["daily", "weekly"], required=True)
    args = parser.parse_args()
    if args.mode == "daily":
        run_daily()
    else:
        run_weekly()


if __name__ == "__main__":
    main()
