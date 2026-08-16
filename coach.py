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
  _section : detail_activity | detail_wellness | detail_laps |
             weekly | records | events
  _part    : 0-based index of this block within its section
  items    : a slice of that section's list

Sections:
  detail_activity / detail_wellness  - rolling 90-day window, rewritten each sync
  detail_laps - lap/interval breakdown for substantial activities in the
                window. A flat list; each item is one lap tagged with `aid`
                (parent activity id) and `n` (lap number). Fetched once per
                activity, since laps are static once an activity is complete.
  weekly   - append-only, one item per completed week
  records  - PB / FTP progression (seeded once)
  events   - append-only milestone races and key sessions
  log_index - one small block, rewritten after every write: current part-block
              ids per section (with date ranges) plus a hash of today's
              content. Read by the Coach Claude skills; see refresh_index().

Activities the Intervals.icu API returns as stubs (id + start time only,
e.g. Strava-sourced activities such as indoor rides) carry no usable data and
are dropped from both the detail sync and the weekly rollup.

Environment variables (GitHub Actions secrets):
  INTERVALS_ATHLETE_ID   e.g. i588094
  INTERVALS_API_KEY      Intervals.icu API key
  CRAFT_API_BASE         e.g. https://connect.craft.do/links/XXXX/api/v1
"""

import os
import re
import json
import argparse
import hashlib
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

# Laps are fetched only for runs, rides and swims at least this long.
LAP_MIN_MINUTES = 20
# Sanity cap on laps stored per activity. Real interval sessions are well
# under this; only an ultra-long auto-lapped activity would ever hit it.
MAX_LAPS = 120

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


def is_data_activity(a):
    """True if an activity carries real training data.

    The Intervals.icu API returns Strava-sourced activities (for example
    indoor ROUVY rides) as stubs with only id and start time. Those carry
    no usable data and are dropped.
    """
    return bool(a.get("type"))


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


def fetch_intervals(activity_id):
    """Return the ordered interval/lap list for one activity, or []."""
    data = iget(f"/activity/{activity_id}/intervals")
    return data.get("icu_intervals") or []


def lap_eligible(activity):
    """True if an activity is worth fetching lap detail for.

    Restricted to runs, rides and swims of meaningful length; short easy
    activities (e.g. 1 km night runs) have no lap structure worth a call.
    """
    if _sport_group(activity.get("type")) not in ("run", "ride", "swim"):
        return False
    return (activity.get("minutes") or 0) >= LAP_MIN_MINUTES


def _pace_min_km(seconds, metres):
    if seconds and metres and metres > 0:
        return round((seconds / 60) / (metres / 1000), 2)
    return None


# Intervals.icu interval field -> compact lap key. None values drop out, so a
# run carries no power keys and a ride carries no run-specific keys.
LAP_FIELD_MAP = [
    ("average_heartrate", "hr"),
    ("min_heartrate", "hrmin"),
    ("max_heartrate", "hrmax"),
    ("average_watts", "w"),
    ("min_watts", "wmin"),
    ("max_watts", "wmax"),
    ("weighted_average_watts", "np"),
    ("intensity", "if"),
    ("zone", "zone"),
    ("training_load", "load"),
]


def trim_lap(iv, n, aid):
    """Compact one Intervals.icu interval object into a flat lap row."""
    row = {"aid": aid, "n": n}
    t = iv.get("type")
    if t:
        row["type"] = str(t).lower()
    sec = iv.get("moving_time")
    if sec is not None:
        row["sec"] = sec
    dist = iv.get("distance")
    if dist is not None:
        row["m"] = round(dist)
    for src, dst in LAP_FIELD_MAP:
        v = iv.get(src)
        if v is not None:
            row[dst] = v
    cad = iv.get("average_cadence")
    if cad is not None:
        row["cad"] = round(cad)
    pace = _pace_min_km(sec, dist)
    if pace is not None:
        row["pace"] = pace
    gap_speed = iv.get("gap")          # grade-adjusted speed, m/s
    if gap_speed:
        row["gap"] = round((1000 / gap_speed) / 60, 2)
    elev = iv.get("total_elevation_gain")
    if elev:
        row["el"] = round(elev, 1)
    grad = iv.get("average_gradient")
    if grad is not None:
        row["grad"] = round(grad * 100, 1)
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
# Log index (consumed by the Coach Claude skills)
# --------------------------------------------------------------------------
# One small managed block, rewritten after every write to the Log. It gives
# the skills (a) the current part-block ids per section with date ranges, so
# they can read the Log without search-based discovery, and (b) a hash of
# today's content (wellness + activities + gym), so the /today change gate
# can decide "unchanged" from this one block without reading any part-block.

INDEX_SECTION = "log_index"
INDEX_MARKER = "LOG-INDEX-V1"

_DATE_KEYS = {
    "detail_activity": lambda it: (it.get("start_date_local") or "")[:10],
    "detail_wellness": lambda it: it.get("id") or "",
    "detail_gym":      lambda it: (it.get("date") or "")[:10],
    "detail_metrics":  lambda it: (it.get("date") or "")[:10],
    "weekly":          lambda it: it.get("week_start") or "",
    "events":          lambda it: it.get("date") or "",
}


def _part_date_range(section, items):
    fn = _DATE_KEYS.get(section)
    if not fn or not items:
        return None, None
    dates = sorted(d for d in (fn(it) for it in items) if d)
    if not dates:
        return None, None
    return dates[0], dates[-1]


def _today_content_hash(blocks, today_iso):
    """SHA-256 over today's wellness item, activities and gym workouts,
    verbatim as stored in the Log."""
    wellness = [it for it in read_section("detail_wellness", blocks)
                if it.get("id") == today_iso]
    acts = sorted(
        (it for it in read_section("detail_activity", blocks)
         if (it.get("start_date_local") or "").startswith(today_iso)),
        key=lambda a: ((a.get("start_date_local") or ""),
                       str(a.get("id") or "")))
    gym = sorted(
        (it for it in read_section("detail_gym", blocks)
         if (it.get("date") or "")[:10] == today_iso),
        key=lambda g: str(g.get("id") or ""))
    payload = {"date": today_iso,
               "wellness": wellness[0] if wellness else None,
               "activities": acts,
               "gym": gym}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True,
                   separators=(",", ":")).encode()).hexdigest()


def refresh_index(doc_id):
    """Rewrite the log_index block from the document's current state.
    Self-contained (does its own walk); call after any write to the Log.
    Never fatal: an index failure must not fail the sync."""
    try:
        blocks = []
        _walk(craft_get_blocks(doc_id), blocks)

        parts, old_ids = {}, []
        for b in blocks:
            d = _extract_json(b.get("markdown", ""))
            if not d or not d.get("_section"):
                continue
            sec = d["_section"]
            if sec == INDEX_SECTION:
                old_ids.append(b.get("id"))
                continue
            items = d.get("items", []) or []
            entry = {"part": d.get("_part", 0), "id": b.get("id"),
                     "n": len(items)}
            lo, hi = _part_date_range(sec, items)
            if lo:
                entry["from"], entry["to"] = lo, hi
            parts.setdefault(sec, []).append(entry)
        for sec in parts:
            parts[sec].sort(key=lambda e: e["part"])

        today_iso = today_mel().isoformat()
        payload = {
            "_section": INDEX_SECTION, "_part": 0,
            "marker": INDEX_MARKER,
            "_updated": dt.datetime.now(MELBOURNE)
                          .isoformat(timespec="seconds"),
            "today": {"date": today_iso,
                      "hash": _today_content_hash(blocks, today_iso)},
            "parts": parts,
        }
        craft_delete(old_ids)
        craft_post("```json\n"
                   + json.dumps(payload, separators=(",", ":"))
                   + "\n```", doc_id)
        print(f"log_index refreshed ({sum(len(v) for v in parts.values())} "
              f"part blocks indexed).")
    except Exception as e:  # noqa: BLE001 - index is best-effort
        print(f"log_index refresh failed (non-fatal): {e}")


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
    write_section("detail_laps", [], doc_id, blocks)
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

    activities = [trim_activity(a) for a in fetch_activities(oldest, today)
                  if is_data_activity(a)]
    wellness = [trim_wellness(w) for w in fetch_wellness(oldest, today)]

    write_section("detail_activity", activities, doc_id, blocks)
    write_section("detail_wellness", wellness, doc_id, blocks)

    # Laps: static once an activity is complete, so fetch each activity once
    # and reuse it thereafter. Steady-state syncs fetch nothing.
    stored = {}
    for r in read_section("detail_laps", blocks):
        stored.setdefault(r.get("aid"), []).append(r)

    laps_out, fetched = [], 0
    for a in activities:
        if not lap_eligible(a):
            continue
        aid = a.get("id")
        if aid in stored:
            laps_out.extend(stored[aid])
            continue
        try:
            ivs = fetch_intervals(aid)
        except requests.RequestException as e:
            print(f"  laps fetch failed for {aid}: {e}")
            continue
        laps_out.extend(trim_lap(iv, i + 1, aid)
                        for i, iv in enumerate(ivs[:MAX_LAPS]))
        fetched += 1

    # Only rewrite detail_laps when its set of activities actually changed
    # (new activity fetched, or an old one aged out of the window).
    if {r["aid"] for r in laps_out} != set(stored):
        write_section("detail_laps", laps_out, doc_id, blocks)

    print(f"Sync complete: {len(activities)} activities, "
          f"{len(wellness)} wellness records, "
          f"laps for {len({r['aid'] for r in laps_out})} activities "
          f"({fetched} newly fetched).")

    refresh_index(doc_id)


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

    acts = [trim_activity(a) for a in fetch_activities(week_start, week_end)
            if is_data_activity(a)]
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

    refresh_index(doc_id)


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
DETAIL_DAYS = 90

# Craft caps a block at 10,000 chars. Budget the items payload well under that
# to leave room for the JSON wrapper and code fences.
JSON_BUDGET = 8500

# Event detection thresholds
RUN_EVENT_KM   = 40
RIDE_EVENT_KM  = 200
SWIM_EVENT_KM  = 4

# Laps are fetched only for runs, rides and swims at least this long.
LAP_MIN_MINUTES = 20
# Sanity cap on laps stored per activity. Real interval sessions are well
# under this; only an ultra-long auto-lapped activity would ever hit it.
MAX_LAPS = 120

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


def is_data_activity(a):
    """True if an activity carries real training data.

    The Intervals.icu API returns Strava-sourced activities (for example
    indoor ROUVY rides) as stubs with only id and start time. Those carry
    no usable data and are dropped.
    """
    return bool(a.get("type"))


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


def fetch_intervals(activity_id):
    """Return the ordered interval/lap list for one activity, or []."""
    data = iget(f"/activity/{activity_id}/intervals")
    return data.get("icu_intervals") or []


def lap_eligible(activity):
    """True if an activity is worth fetching lap detail for.

    Restricted to runs, rides and swims of meaningful length; short easy
    activities (e.g. 1 km night runs) have no lap structure worth a call.
    """
    if _sport_group(activity.get("type")) not in ("run", "ride", "swim"):
        return False
    return (activity.get("minutes") or 0) >= LAP_MIN_MINUTES


def _pace_min_km(seconds, metres):
    if seconds and metres and metres > 0:
        return round((seconds / 60) / (metres / 1000), 2)
    return None


# Intervals.icu interval field -> compact lap key. None values drop out, so a
# run carries no power keys and a ride carries no run-specific keys.
LAP_FIELD_MAP = [
    ("average_heartrate", "hr"),
    ("min_heartrate", "hrmin"),
    ("max_heartrate", "hrmax"),
    ("average_watts", "w"),
    ("min_watts", "wmin"),
    ("max_watts", "wmax"),
    ("weighted_average_watts", "np"),
    ("intensity", "if"),
    ("zone", "zone"),
    ("training_load", "load"),
]


def trim_lap(iv, n, aid):
    """Compact one Intervals.icu interval object into a flat lap row."""
    row = {"aid": aid, "n": n}
    t = iv.get("type")
    if t:
        row["type"] = str(t).lower()
    sec = iv.get("moving_time")
    if sec is not None:
        row["sec"] = sec
    dist = iv.get("distance")
    if dist is not None:
        row["m"] = round(dist)
    for src, dst in LAP_FIELD_MAP:
        v = iv.get(src)
        if v is not None:
            row[dst] = v
    cad = iv.get("average_cadence")
    if cad is not None:
        row["cad"] = round(cad)
    pace = _pace_min_km(sec, dist)
    if pace is not None:
        row["pace"] = pace
    gap_speed = iv.get("gap")          # grade-adjusted speed, m/s
    if gap_speed:
        row["gap"] = round((1000 / gap_speed) / 60, 2)
    elev = iv.get("total_elevation_gain")
    if elev:
        row["el"] = round(elev, 1)
    grad = iv.get("average_gradient")
    if grad is not None:
        row["grad"] = round(grad * 100, 1)
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
# Log index (consumed by the Coach Claude skills)
# --------------------------------------------------------------------------
# One small managed block, rewritten after every write to the Log. It gives
# the skills (a) the current part-block ids per section with date ranges, so
# they can read the Log without search-based discovery, and (b) a hash of
# today's content (wellness + activities + gym), so the /today change gate
# can decide "unchanged" from this one block without reading any part-block.

INDEX_SECTION = "log_index"
INDEX_MARKER = "LOG-INDEX-V1"

_DATE_KEYS = {
    "detail_activity": lambda it: (it.get("start_date_local") or "")[:10],
    "detail_wellness": lambda it: it.get("id") or "",
    "detail_gym":      lambda it: (it.get("date") or "")[:10],
    "weekly":          lambda it: it.get("week_start") or "",
    "events":          lambda it: it.get("date") or "",
}


def _part_date_range(section, items):
    fn = _DATE_KEYS.get(section)
    if not fn or not items:
        return None, None
    dates = sorted(d for d in (fn(it) for it in items) if d)
    if not dates:
        return None, None
    return dates[0], dates[-1]


def _today_content_hash(blocks, today_iso):
    """SHA-256 over today's wellness item, activities and gym workouts,
    verbatim as stored in the Log."""
    wellness = [it for it in read_section("detail_wellness", blocks)
                if it.get("id") == today_iso]
    acts = sorted(
        (it for it in read_section("detail_activity", blocks)
         if (it.get("start_date_local") or "").startswith(today_iso)),
        key=lambda a: ((a.get("start_date_local") or ""),
                       str(a.get("id") or "")))
    gym = sorted(
        (it for it in read_section("detail_gym", blocks)
         if (it.get("date") or "")[:10] == today_iso),
        key=lambda g: str(g.get("id") or ""))
    payload = {"date": today_iso,
               "wellness": wellness[0] if wellness else None,
               "activities": acts,
               "gym": gym}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True,
                   separators=(",", ":")).encode()).hexdigest()


def refresh_index(doc_id):
    """Rewrite the log_index block from the document's current state.
    Self-contained (does its own walk); call after any write to the Log.
    Never fatal: an index failure must not fail the sync."""
    try:
        blocks = []
        _walk(craft_get_blocks(doc_id), blocks)

        parts, old_ids = {}, []
        for b in blocks:
            d = _extract_json(b.get("markdown", ""))
            if not d or not d.get("_section"):
                continue
            sec = d["_section"]
            if sec == INDEX_SECTION:
                old_ids.append(b.get("id"))
                continue
            items = d.get("items", []) or []
            entry = {"part": d.get("_part", 0), "id": b.get("id"),
                     "n": len(items)}
            lo, hi = _part_date_range(sec, items)
            if lo:
                entry["from"], entry["to"] = lo, hi
            parts.setdefault(sec, []).append(entry)
        for sec in parts:
            parts[sec].sort(key=lambda e: e["part"])

        today_iso = today_mel().isoformat()
        payload = {
            "_section": INDEX_SECTION, "_part": 0,
            "marker": INDEX_MARKER,
            "_updated": dt.datetime.now(MELBOURNE)
                          .isoformat(timespec="seconds"),
            "today": {"date": today_iso,
                      "hash": _today_content_hash(blocks, today_iso)},
            "parts": parts,
        }
        craft_delete(old_ids)
        craft_post("```json\n"
                   + json.dumps(payload, separators=(",", ":"))
                   + "\n```", doc_id)
        print(f"log_index refreshed ({sum(len(v) for v in parts.values())} "
              f"part blocks indexed).")
    except Exception as e:  # noqa: BLE001 - index is best-effort
        print(f"log_index refresh failed (non-fatal): {e}")


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
    write_section("detail_laps", [], doc_id, blocks)
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

    activities = [trim_activity(a) for a in fetch_activities(oldest, today)
                  if is_data_activity(a)]
    wellness = [trim_wellness(w) for w in fetch_wellness(oldest, today)]

    write_section("detail_activity", activities, doc_id, blocks)
    write_section("detail_wellness", wellness, doc_id, blocks)

    # Laps: static once an activity is complete, so fetch each activity once
    # and reuse it thereafter. Steady-state syncs fetch nothing.
    stored = {}
    for r in read_section("detail_laps", blocks):
        stored.setdefault(r.get("aid"), []).append(r)

    laps_out, fetched = [], 0
    for a in activities:
        if not lap_eligible(a):
            continue
        aid = a.get("id")
        if aid in stored:
            laps_out.extend(stored[aid])
            continue
        try:
            ivs = fetch_intervals(aid)
        except requests.RequestException as e:
            print(f"  laps fetch failed for {aid}: {e}")
            continue
        laps_out.extend(trim_lap(iv, i + 1, aid)
                        for i, iv in enumerate(ivs[:MAX_LAPS]))
        fetched += 1

    # Only rewrite detail_laps when its set of activities actually changed
    # (new activity fetched, or an old one aged out of the window).
    if {r["aid"] for r in laps_out} != set(stored):
        write_section("detail_laps", laps_out, doc_id, blocks)

    print(f"Sync complete: {len(activities)} activities, "
          f"{len(wellness)} wellness records, "
          f"laps for {len({r['aid'] for r in laps_out})} activities "
          f"({fetched} newly fetched).")

    refresh_index(doc_id)


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

    acts = [trim_activity(a) for a in fetch_activities(week_start, week_end)
            if is_data_activity(a)]
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

    refresh_index(doc_id)


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
