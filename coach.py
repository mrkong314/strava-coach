#!/usr/bin/env python3
"""
coach.py - Intervals.icu -> Craft training data pipeline.

Pure data sync. No LLM in the pipeline; all analysis happens in chat.

Modes:
  sync     Refresh the Detail section (last 90 days of activities + wellness,
           plus per-interval data for the last 21 days). Run every 30 min.
           On first run, if the Training Log has no managed sections, it
           creates the four-section structure and seeds Events and Records
           from history_seed.json.
  rollup   Append last week's summary block, plus any new events detected in
           that week. Run weekly on Monday.

The Craft Training Log is one document holding four managed blocks, each a
fenced JSON object identified by its "_section" key:
  detail   - rolling 90-day window, overwritten every sync
  weekly   - append-only, one entry per completed week
  records  - append-only PB / FTP progression
  events   - append-only milestone races and key sessions

Environment variables (GitHub Actions secrets):
  INTERVALS_ATHLETE_ID   e.g. i588094
  INTERVALS_API_KEY      Intervals.icu API key
  CRAFT_API_BASE         e.g. https://connect.craft.do/links/XXXX/api/v1
"""

import os
import re
import json
import argparse
import datetime as dt

import requests

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

INTERVALS_ATHLETE_ID = os.environ["INTERVALS_ATHLETE_ID"]
INTERVALS_API_KEY    = os.environ["INTERVALS_API_KEY"]
CRAFT_API_BASE       = os.environ["CRAFT_API_BASE"].rstrip("/")

INTERVALS_BASE = "https://intervals.icu/api/v1"

# Melbourne is UTC+10 (AEST) / UTC+11 (AEDT). +10 is fine for the local date
# because the jobs never run near local midnight.
MELBOURNE = dt.timezone(dt.timedelta(hours=10))

DETAIL_DAYS   = 90    # rolling detail window
INTERVAL_DAYS = 21    # per-interval detail kept for this many recent days

# Event detection thresholds
RUN_EVENT_KM   = 40    # runs at/near marathon distance and beyond
RIDE_EVENT_KM  = 200   # long rides
SWIM_EVENT_KM  = 4     # long swims

HERE = os.path.dirname(os.path.abspath(__file__))
SEED_FILE = os.path.join(HERE, "history_seed.json")

# --------------------------------------------------------------------------
# Intervals.icu
# --------------------------------------------------------------------------

def iget(path, params=None):
    r = requests.get(
        f"{INTERVALS_BASE}{path}",
        params=params or {},
        auth=("API_KEY", INTERVALS_API_KEY),
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def today_mel():
    return dt.datetime.now(MELBOURNE).date()


def fetch_activities(oldest, newest):
    return iget(
        f"/athlete/{INTERVALS_ATHLETE_ID}/activities",
        {"oldest": oldest.isoformat(), "newest": newest.isoformat()},
    )


def fetch_wellness(oldest, newest):
    return iget(
        f"/athlete/{INTERVALS_ATHLETE_ID}/wellness",
        {"oldest": oldest.isoformat(), "newest": newest.isoformat()},
    )


def fetch_intervals(activity_id):
    """Per-interval / lap detail for one activity. None if unavailable."""
    try:
        return iget(f"/activity/{activity_id}/intervals")
    except Exception:
        return None


# --- field trimming -------------------------------------------------------

ACT_FIELDS = ["id", "start_date_local", "type", "name", "moving_time",
              "distance", "total_elevation_gain", "icu_training_load",
              "icu_intensity", "average_heartrate", "max_heartrate",
              "icu_average_watts", "icu_weighted_avg_watts", "icu_ftp",
              "average_speed", "icu_rpe", "feel"]

WEL_FIELDS = ["id", "ctl", "atl", "rampRate", "restingHR", "hrv", "hrvSDNN",
              "sleepSecs", "sleepScore", "weight", "eftp"]


def trim_activity(a):
    row = {k: a.get(k) for k in ACT_FIELDS if a.get(k) is not None}
    mt = row.get("moving_time")
    dist = row.get("distance")
    if mt:
        row["minutes"] = round(mt / 60, 1)
    if dist:
        row["km"] = round(dist / 1000, 2)
    if mt and dist and dist > 0:
        row["pace_min_km"] = round((mt / 60) / (dist / 1000), 2)
    return row


def trim_wellness(w):
    row = {k: w.get(k) for k in WEL_FIELDS if w.get(k) is not None}
    if "ctl" in row and "atl" in row:
        row["form"] = round(row["ctl"] - row["atl"], 1)
    if row.get("sleepSecs"):
        row["sleepHours"] = round(row["sleepSecs"] / 3600, 1)
    return row


# --------------------------------------------------------------------------
# Craft
# --------------------------------------------------------------------------

def craft_document_id():
    r = requests.get(f"{CRAFT_API_BASE}/documents", timeout=30)
    r.raise_for_status()
    live = [d for d in r.json().get("items", []) if not d.get("isDeleted")]
    if not live:
        raise RuntimeError("No documents available on the Craft connection.")
    return live[0]["id"]


def craft_get_blocks(block_id):
    r = requests.get(f"{CRAFT_API_BASE}/blocks", params={"id": block_id},
                     timeout=30)
    r.raise_for_status()
    return r.json()


def craft_post(markdown, doc_id, position="end"):
    r = requests.post(
        f"{CRAFT_API_BASE}/blocks",
        json={"markdown": markdown,
              "position": {"position": position, "pageId": doc_id}},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def craft_put(block_id, markdown):
    r = requests.put(
        f"{CRAFT_API_BASE}/blocks",
        json={"blocks": [{"id": block_id, "markdown": markdown}]},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def _walk(node, out):
    """Flatten a Craft block tree into a list."""
    if isinstance(node, dict):
        out.append(node)
        for child in node.get("content", []) or []:
            _walk(child, out)
    elif isinstance(node, list):
        for child in node:
            _walk(child, out)


def _extract_json(markdown):
    """Pull a JSON object out of a block's markdown, fenced or bare."""
    if not markdown:
        return None
    text = markdown.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    if not text.startswith("{"):
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def section_block(section, doc_id, blocks):
    """Return (block_id, data) for a managed section, or (None, None)."""
    for b in blocks:
        data = _extract_json(b.get("markdown", ""))
        if data and data.get("_section") == section:
            return b.get("id"), data
    return None, None


def write_section(section, payload, doc_id, block_id=None):
    """Create or overwrite a managed section block as fenced JSON."""
    payload = dict(payload)
    payload["_section"] = section
    payload["_updated"] = dt.datetime.now(MELBOURNE).isoformat(
        timespec="seconds")
    markdown = "```json\n" + json.dumps(payload, separators=(",", ":")) + "\n```"
    if block_id:
        craft_put(block_id, markdown)
    else:
        craft_post(f"## {section.upper()}", doc_id, position="end")
        craft_post(markdown, doc_id, position="end")


# --------------------------------------------------------------------------
# First-run scaffold + seed
# --------------------------------------------------------------------------

def ensure_structure(doc_id, blocks):
    """Create the four sections if none exist. Returns True if it scaffolded."""
    has_any = any(_extract_json(b.get("markdown", "")) for b in blocks)
    if has_any:
        return False

    print("No managed sections found. Creating structure and seeding history.")
    seed = {"events": [], "records": {}}
    if os.path.exists(SEED_FILE):
        with open(SEED_FILE) as f:
            seed = json.load(f)

    write_section("detail",  {"wellness": [], "activities": []}, doc_id)
    write_section("weekly",  {"items": []}, doc_id)
    write_section("records", {"items": seed.get("records", {})}, doc_id)
    write_section("events",  {"items": seed.get("events", [])}, doc_id)
    return True


# --------------------------------------------------------------------------
# Mode: sync
# --------------------------------------------------------------------------

def run_sync():
    doc_id = craft_document_id()
    blocks = []
    _walk(craft_get_blocks(doc_id), blocks)

    if ensure_structure(doc_id, blocks):
        blocks = []
        _walk(craft_get_blocks(doc_id), blocks)

    today = today_mel()
    oldest = today - dt.timedelta(days=DETAIL_DAYS)
    interval_cutoff = today - dt.timedelta(days=INTERVAL_DAYS)

    acts_raw = fetch_activities(oldest, today)
    wel_raw = fetch_wellness(oldest, today)

    activities = [trim_activity(a) for a in acts_raw]
    wellness = [trim_wellness(w) for w in wel_raw]

    # carry forward interval data we already hold; fetch only for new activities
    det_id, det_data = section_block("detail", doc_id, blocks)
    prev_intervals = {}
    if det_data:
        for a in det_data.get("activities", []):
            if a.get("intervals") is not None and a.get("id") is not None:
                prev_intervals[a["id"]] = a["intervals"]

    for a in activities:
        sd = (a.get("start_date_local") or "")[:10]
        if sd and sd >= interval_cutoff.isoformat():
            aid = a.get("id")
            if aid in prev_intervals:
                a["intervals"] = prev_intervals[aid]
            elif aid is not None:
                iv = fetch_intervals(aid)
                if iv is not None:
                    a["intervals"] = iv

    payload = {"window_days": DETAIL_DAYS, "wellness": wellness,
               "activities": activities}
    write_section("detail", payload, doc_id, block_id=det_id)
    print(f"Sync complete: {len(activities)} activities, "
          f"{len(wellness)} wellness records.")


# --------------------------------------------------------------------------
# Mode: rollup
# --------------------------------------------------------------------------

def _sport_group(act_type):
    t = (act_type or "").lower()
    if "run" in t:
        return "run"
    if "ride" in t or "bike" in t or "cycl" in t:
        return "ride"
    if "swim" in t:
        return "swim"
    if "gym" in t or "weight" in t or "strength" in t:
        return "gym"
    return "other"


def _detect_events(activities):
    """Return event dicts for activities crossing milestone thresholds."""
    found = []
    for a in activities:
        name = a.get("name") or ""
        low = name.lower()
        sport = _sport_group(a.get("type"))
        km = a.get("km") or 0
        reasons = []
        if "race" in low:
            reasons.append("race")
        if "ftp test" in low:
            reasons.append("ftp_test")
        if sport == "run" and km >= RUN_EVENT_KM:
            reasons.append(f"run_{RUN_EVENT_KM}k_plus")
        if sport == "ride" and km >= RIDE_EVENT_KM:
            reasons.append(f"ride_{RIDE_EVENT_KM}k_plus")
        if sport == "swim" and km >= SWIM_EVENT_KM:
            reasons.append(f"swim_{SWIM_EVENT_KM}k_plus")
        if reasons:
            found.append({
                "activity_id": a.get("id"),
                "date": (a.get("start_date_local") or "")[:10],
                "sport": sport,
                "name": name,
                "km": km,
                "minutes": a.get("minutes"),
                "triggers": reasons,
            })
    return found


def run_rollup():
    doc_id = craft_document_id()
    blocks = []
    _walk(craft_get_blocks(doc_id), blocks)

    if ensure_structure(doc_id, blocks):
        blocks = []
        _walk(craft_get_blocks(doc_id), blocks)

    today = today_mel()
    week_end = today - dt.timedelta(days=1)            # Sunday just gone
    week_start = week_end - dt.timedelta(days=6)       # the Monday

    acts = [trim_activity(a) for a in fetch_activities(week_start, week_end)]
    wel = [trim_wellness(w) for w in fetch_wellness(week_start, week_end)]

    # per-sport totals
    sports = {}
    for a in acts:
        g = _sport_group(a.get("type"))
        s = sports.setdefault(g, {"sessions": 0, "minutes": 0.0, "load": 0.0,
                                  "km": 0.0, "hr_sum": 0.0, "hr_n": 0})
        s["sessions"] += 1
        s["minutes"] += a.get("minutes") or 0
        s["load"] += a.get("icu_training_load") or 0
        s["km"] += a.get("km") or 0
        if a.get("average_heartrate"):
            s["hr_sum"] += a["average_heartrate"]
            s["hr_n"] += 1
    for g, s in sports.items():
        s["minutes"] = round(s["minutes"], 1)
        s["load"] = round(s["load"], 1)
        s["km"] = round(s["km"], 2)
        s["avg_hr"] = round(s["hr_sum"] / s["hr_n"], 1) if s["hr_n"] else None
        if s["km"] > 0 and g in ("run", "swim"):
            s["avg_pace_min_km"] = round(s["minutes"] / s["km"], 2)
        del s["hr_sum"], s["hr_n"]

    last_wel = wel[-1] if wel else {}
    ftps = [a["icu_ftp"] for a in acts if a.get("icu_ftp")]

    week_entry = {
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "ctl": last_wel.get("ctl"),
        "atl": last_wel.get("atl"),
        "form": last_wel.get("form"),
        "total_load": round(sum((a.get("icu_training_load") or 0)
                                for a in acts), 1),
        "total_hours": round(sum((a.get("minutes") or 0)
                                 for a in acts) / 60, 2),
        "sessions": len(acts),
        "eftp": max(ftps) if ftps else last_wel.get("eftp"),
        "by_sport": sports,
        "events_this_week": [e["name"] for e in _detect_events(acts)],
    }

    # append weekly entry
    wk_id, wk_data = section_block("weekly", doc_id, blocks)
    items = (wk_data or {}).get("items", [])
    items.append(week_entry)
    write_section("weekly", {"items": items}, doc_id, block_id=wk_id)

    # append new events (dedup on activity_id)
    ev_id, ev_data = section_block("events", doc_id, blocks)
    ev_items = (ev_data or {}).get("items", [])
    known = {e.get("activity_id") for e in ev_items if e.get("activity_id")}
    new_events = [e for e in _detect_events(acts)
                  if e.get("activity_id") not in known]
    if new_events:
        ev_items.extend(new_events)
        write_section("events", {"items": ev_items}, doc_id, block_id=ev_id)

    print(f"Rollup complete for {week_start} to {week_end}: "
          f"{len(acts)} activities, {len(new_events)} new event(s).")


# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["sync", "rollup"], required=True)
    args = parser.parse_args()
    if args.mode == "sync":
        run_sync()
    else:
        run_rollup()


if __name__ == "__main__":
    main()
