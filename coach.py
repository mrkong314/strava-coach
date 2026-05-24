#!/usr/bin/env python3
"""
coach.py - Intervals.icu -> Craft training data pipeline.

Pure data sync. No LLM in the pipeline; all analysis happens in chat.

Modes:
  sync     Refresh the detail data (last 90 days of activities + wellness).
           Run every 30 min. On first run, if the Training Log has no managed
           blocks, it seeds Events and Records from history_seed.json.
  rollup   Append last week's summary, plus any new events detected that week.
           Run weekly on Monday.

Craft caps a single block at 10,000 characters, so each logical section is
stored across as many sub-blocks as needed. Every managed block is a fenced
JSON object carrying:
  _section : detail_activity | detail_wellness | weekly | records | events
  _part    : 0-based index of this block within its section
  items    : a slice of that section's list

Sections:
  detail_activity / detail_wellness  - rolling 90-day window, rewritten each sync
  weekly   - append-only, one item per completed week
  records  - PB / FTP progression (seeded once)
  events   - append-only milestone races and key sessions

Per-interval / lap detail is not yet captured; it will be added in a follow-up
with compact trimming.

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

MELBOURNE = dt.timezone(dt.timedelta(hours=10))

DETAIL_DAYS = 90

# Craft caps a block at 10,000 chars. Budget the items payload well under that
# to leave room for the JSON wrapper and code fences.
JSON_BUDGET = 8500

# Event detection thresholds
RUN_EVENT_KM   = 40
RIDE_EVENT_KM  = 200
SWIM_EVENT_KM  = 4

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


def craft_delete(block_ids):
    ids = [x for x in block_ids if x]
    if not ids:
        return
    r = requests.delete(f"{CRAFT_API_BASE}/blocks",
                        json={"blockIds": ids}, timeout=60)
    r.raise_for_status()


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


def has_managed(blocks):
    return any((_extract_json(b.get("markdown", "")) or {}).get("_section")
               for b in blocks)


def read_section(section, blocks):
    """Collect every item of a section across its sub-blocks, in order."""
    parts = []
    for b in blocks:
        d = _extract_json(b.get("markdown", ""))
        if d and d.get("_section") == section:
            parts.append((d.get("_part", 0), d.get("items", []) or []))
    parts.sort(key=lambda x: x[0])
    out = []
    for _, items in parts:
        out.extend(items)
    return out


def _chunk_items(items):
    """Split a list into chunks whose JSON stays under JSON_BUDGET chars."""
    chunks, cur, cur_len = [], [], 0
    for it in items:
        size = len(json.dumps(it, separators=(",", ":")))
        if cur and cur_len + size + 1 > JSON_BUDGET:
            chunks.append(cur)
            cur, cur_len = [], 0
        cur.append(it)
        cur_len += size + 1
    if cur or not chunks:
        chunks.append(cur)
    return chunks


def write_section(section, items, doc_id, blocks):
    """Delete a section's existing sub-blocks and write it fresh, chunked."""
    old_ids = [b.get("id") for b in blocks
               if (_extract_json(b.get("markdown", "")) or {}).get("_section")
               == section]
    craft_delete(old_ids)
    stamp = dt.datetime.now(MELBOURNE).isoformat(timespec="seconds")
    for i, chunk in enumerate(_chunk_items(list(items))):
        payload = {"_section": section, "_part": i,
                   "_updated": stamp, "items": chunk}
        markdown = ("```json\n"
                    + json.dumps(payload, separators=(",", ":"))
                    + "\n```")
        craft_post(markdown, doc_id)


# --------------------------------------------------------------------------
# First-run scaffold + seed
# --------------------------------------------------------------------------

def ensure_structure(doc_id, blocks):
    """Seed Events and Records if the document has no managed blocks."""
    if has_managed(blocks):
        return False

    print("No managed sections found. Seeding history.")
    seed = {"events": [], "records": {}}
    if os.path.exists(SEED_FILE):
        with open(SEED_FILE) as f:
            seed = json.load(f)

    # flatten the records dict into a list of {category, ...} items
    rec_items = []
    for category, entries in (seed.get("records") or {}).items():
        for r in entries:
            row = {"category": category}
            row.update(r)
            rec_items.append(row)

    write_section("events", seed.get("events", []), doc_id, blocks)
    write_section("records", rec_items, doc_id, blocks)
    write_section("weekly", [], doc_id, blocks)
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

    activities = [trim_activity(a) for a in fetch_activities(oldest, today)]
    wellness = [trim_wellness(w) for w in fetch_wellness(oldest, today)]

    write_section("detail_activity", activities, doc_id, blocks)
    write_section("detail_wellness", wellness, doc_id, blocks)
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

    weekly = read_section("weekly", blocks)
    weekly.append(week_entry)
    write_section("weekly", weekly, doc_id, blocks)

    events = read_section("events", blocks)
    known = {e.get("activity_id") for e in events if e.get("activity_id")}
    new_events = [e for e in _detect_events(acts)
                  if e.get("activity_id") not in known]
    if new_events:
        events.extend(new_events)
        write_section("events", events, doc_id, blocks)

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
