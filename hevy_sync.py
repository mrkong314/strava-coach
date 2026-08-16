"""
hevy_sync.py - Hevy <-> training pipeline.

Two modes, selected with --mode:

  push : Read planned gym sessions from the Workout Planning Google Calendar.
         Each gym event's description carries a [HEVY-ROUTINE] block (compact
         pipe-delimited syntax). Parse it and create/update the routine in Hevy
         via the Hevy API. Only events whose description contains
         [HEVY-ROUTINE] are touched, so runs/rides/XC are ignored.

  pull : Read completed workouts from Hevy (GET /v1/workouts), and write them
         into the Craft Training Log as a managed JSON section (_section
         "detail_gym"), using the same chunked-codeblock mechanism coach.py
         uses for detail_activity. Dedup by workout id; only writes when there
         is genuinely new or changed data, so it is safe on a 30-minute cron.

Environment variables (set as GitHub Actions secrets):
  HEVY_API_KEY        Hevy Pro API key (from hevy.com/settings?developer)
  CRAFT_API_BASE      e.g. https://connect.craft.do/links/XXXX/api/v1   (pull only)
  GOOGLE_CREDENTIALS_JSON  service-account JSON for calendar read        (push only)
  GCAL_CALENDAR_ID    the Workout Planning calendar id                   (push only)

Mirrors coach.py conventions: plain requests, Melbourne tz, chunked JSON
sections under an 8500-char budget, delete-then-repost section writes.
"""

import os
import re
import json
import time
import argparse
import datetime as dt
import hashlib

import requests

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

HEVY_API_KEY = os.environ.get("HEVY_API_KEY")
HEVY_BASE = "https://api.hevyapp.com/v1"
HEVY_HEADERS = {"api-key": HEVY_API_KEY or "", "Content-Type": "application/json"}

CRAFT_API_BASE = (os.environ.get("CRAFT_API_BASE") or "").rstrip("/")

MELBOURNE = dt.timezone(dt.timedelta(hours=10))
JSON_BUDGET = 8500          # Craft caps a block ~10000 chars; stay under
GYM_SECTION = "detail_gym"  # managed section name in the Training Log
PULL_LOOKBACK_DAYS = 21     # how far back the pull considers workouts
HEVY_PAGE_SIZE = 10         # GET /v1/workouts max pageSize is 10
HEVY_MAX_PAGES = 5          # safety cap when scanning recent workouts

# Reverse map: Hevy exercise_template_id -> friendly name used in the Log.
# Full library (mirrors the Craft doc "Hevy Exercise IDs"), so pulled workouts
# name cleanly whatever a block uses without editing this each block.
ID_TO_NAME = {
    # squat / quad
    "D04AC939": "Back Squat (Barbell)", "5046D0A9": "Front Squat",
    "CE1054CE": "Pause Squat (Barbell)", "38FC1AB9": "Box Squat (Barbell)",
    "3D0C7C75": "Goblet Squat", "1E42FD5F": "Hack Squat (Machine)",
    "C7973E0E": "Leg Press (Machine)", "75A4F6C4": "Leg Extension (Machine)",
    # hinge / posterior
    "C6272009": "Deadlift (Barbell)", "2B4B7310": "Romanian Deadlift (Barbell)",
    "72CFFAD5": "Romanian Deadlift (Dumbbell)", "D20D7BBE": "Sumo Deadlift (Barbell)",
    "B923B230": "Deadlift (Trap bar)", "D57C2EC7": "Hip Thrust (Barbell)",
    "4180C405": "Good Morning (Barbell)", "11A123F3": "Seated Leg Curl (Machine)",
    "B8127AD1": "Lying Leg Curl (Machine)",
    # single-leg
    "B5D3A742": "Bulgarian Split Squat", "6E6EE645": "Lunge (Barbell)",
    "B537D09F": "Lunge (Dumbbell)", "A733CC5B": "Walking Lunge (Dumbbell)",
    "128A2381": "Step Up", "937292AB": "Single Leg Romanian Deadlift (Dumbbell)",
    # calf
    "E53CCBE5": "Standing Calf Raise (Barbell)", "6DA40660": "Standing Calf Raise (Dumbbell)",
    "E05C2C38": "Standing Calf Raise (Machine)", "062AB91A": "Seated Calf Raise",
    # horizontal push
    "79D0BB3A": "Bench Press (Barbell)", "3601968B": "Bench Press (Dumbbell)",
    "50DFDFAB": "Incline Bench Press (Barbell)", "07B38369": "Incline Bench Press (Dumbbell)",
    "7EB3F7C3": "Chest Press (Machine)", "6FCD7755": "Chest Dip", "392887AA": "Push Up",
    # vertical push
    "7B8D84E8": "Overhead Press (Barbell)", "6AC96645": "Overhead Press (Dumbbell)",
    "9930DF71": "Seated Overhead Press (Dumbbell)", "A69FF221": "Arnold Press (Dumbbell)",
    "422B08F1": "Lateral Raise (Dumbbell)", "BE289E45": "Lateral Raise (Cable)",
    # vertical pull
    "1B2B1E7C": "Pull Up", "729237D1": "Pull Up (Weighted)", "29083183": "Chin Up",
    "6A6C31A5": "Lat Pulldown (Cable)", "473CF5B8": "Lat Pulldown (Machine)",
    # horizontal pull
    "55E6546F": "Bent Over Row (Barbell)", "D0C4A899": "Single Arm Cable Row",
    "0393F233": "Seated Cable Row - V Grip", "BE640BA0": "Face Pull",
    # arms
    "A5AC6449": "Bicep Curl (Barbell)", "37FCC2BB": "Bicep Curl (Dumbbell)",
    "7E3BC8B6": "Hammer Curl (Dumbbell)", "93A552C6": "Triceps Pushdown",
    "94B7239B": "Triceps Rope Pushdown", "3765684D": "Triceps Extension (Dumbbell)",
    "875F585F": "Skullcrusher (Barbell)",
    # core
    "C6C9B8A0": "Plank", "08590920": "Hanging Knee Raise", "F8356514": "Hanging Leg Raise",
    "23A48484": "Cable Crunch", "CC55119B": "Cable Pallof Press",
    "2982AA23": "Russian Twist (Weighted)", "99D5F10E": "Ab Wheel",
}

# Bodyweight / reps-only / duration: push reps (or nothing), omit weight_kg.
REPS_ONLY_IDS = {
    "1B2B1E7C",  # Pull Up
    "729237D1",  # Pull Up (Weighted) - reps-only unless you log added kg
    "29083183",  # Chin Up
    "392887AA",  # Push Up
    "128A2381",  # Step Up
    "6FCD7755",  # Chest Dip
    "08590920",  # Hanging Knee Raise
    "F8356514",  # Hanging Leg Raise
    "C6C9B8A0",  # Plank (duration)
}

# Heavy barbell compounds that get a warmup ramp on push: 2 warmup sets at
# ~60% and ~80% of the top (working) weight are prepended, typed as Hevy
# "warmup" sets so they do not count as working volume. Accessories, dumbbell/
# machine variants and reps-only lifts are excluded (no ramp).
RAMP_IDS = {
    "D04AC939",  # Back Squat (Barbell)
    "5046D0A9",  # Front Squat
    "CE1054CE",  # Pause Squat (Barbell)
    "38FC1AB9",  # Box Squat (Barbell)
    "C6272009",  # Deadlift (Barbell)
    "D20D7BBE",  # Sumo Deadlift (Barbell)
    "B923B230",  # Deadlift (Trap bar)
    "2B4B7310",  # Romanian Deadlift (Barbell)
    "79D0BB3A",  # Bench Press (Barbell)
    "50DFDFAB",  # Incline Bench Press (Barbell)
    "7B8D84E8",  # Overhead Press (Barbell)
}

# Warmup ramp: fractions of the top weight. (Hevy rest is per-exercise, not
# per-set, so warmup sets share the lift's rest_seconds; harmless as warmups
# are light.)
RAMP_FRACTIONS = (0.60, 0.80)


def _round_2p5(kg):
    """Round to nearest 2.5 kg (loadable on a barbell)."""
    return round(kg / 2.5) * 2.5


def today_mel():
    return dt.datetime.now(MELBOURNE).date()


# --------------------------------------------------------------------------
# Hevy API
# --------------------------------------------------------------------------

def hevy_get(path, params=None):
    r = requests.get(f"{HEVY_BASE}{path}", params=params or {},
                     headers=HEVY_HEADERS, timeout=60)
    r.raise_for_status()
    return r.json()


def hevy_post(path, body):
    r = requests.post(f"{HEVY_BASE}{path}", json=body,
                      headers=HEVY_HEADERS, timeout=60)
    r.raise_for_status()
    return r.json()


def hevy_put(path, body):
    r = requests.put(f"{HEVY_BASE}{path}", json=body,
                     headers=HEVY_HEADERS, timeout=60)
    if not r.ok:
        print(f"PUT {path} -> {r.status_code}: {r.text}")
    r.raise_for_status()
    return r.json()


def hevy_list_routines():
    """All routines, paginated, so push can find-or-update by title."""
    out, page = [], 1
    while page <= 20:
        data = hevy_get("/routines", {"page": page, "pageSize": 10})
        rts = data.get("routines", []) or []
        out.extend(rts)
        if page >= data.get("page_count", 1) or not rts:
            break
        page += 1
    return out


def hevy_recent_workouts():
    """Recent workouts, newest first, bounded by PULL_LOOKBACK_DAYS."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=PULL_LOOKBACK_DAYS)
    out, page = [], 1
    while page <= HEVY_MAX_PAGES:
        data = hevy_get("/workouts", {"page": page, "pageSize": HEVY_PAGE_SIZE})
        ws = data.get("workouts", []) or []
        if not ws:
            break
        out.extend(ws)
        # stop once we are clearly past the lookback window
        oldest = ws[-1].get("start_time", "")
        try:
            if dt.datetime.fromisoformat(oldest.replace("Z", "+00:00")) < cutoff:
                break
        except ValueError:
            pass
        if page >= data.get("page_count", 1):
            break
        page += 1
    # keep only those within the window
    keep = []
    for w in out:
        st = w.get("start_time", "")
        try:
            if dt.datetime.fromisoformat(st.replace("Z", "+00:00")) >= cutoff:
                keep.append(w)
        except ValueError:
            keep.append(w)
    return keep


# --------------------------------------------------------------------------
# Craft (identical mechanism to coach.py)
# --------------------------------------------------------------------------

CRAFT_RETRY_STATUSES = (429, 500, 502, 503, 504)


def craft_get(path, params=None, attempts=4):
    """GET against the Craft API with retry on transient 5xx/429.

    Craft's connect gateway throws occasional 502s; a single one should not
    kill a cron run. GETs are idempotent so retrying is safe. Backoff is
    2s, 4s, 8s (max ~14s added). POST/DELETE are deliberately NOT retried:
    with the delete-then-repost section mechanism, retrying a write that
    actually landed before the gateway error would duplicate blocks.
    """
    delay = 2
    for attempt in range(attempts):
        try:
            r = requests.get(f"{CRAFT_API_BASE}{path}", params=params or {},
                             timeout=30)
            if r.status_code in CRAFT_RETRY_STATUSES and attempt < attempts - 1:
                print(f"GET {path} -> {r.status_code}, retrying in {delay}s")
                time.sleep(delay)
                delay *= 2
                continue
            r.raise_for_status()
            return r
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            if attempt < attempts - 1:
                print(f"GET {path} -> {type(e).__name__}, retrying in {delay}s")
                time.sleep(delay)
                delay *= 2
                continue
            raise


def craft_document_id():
    r = craft_get("/documents")
    live = [d for d in r.json().get("items", []) if not d.get("isDeleted")]
    if not live:
        raise RuntimeError("No documents available on the Craft connection.")
    return live[0]["id"]


def craft_get_blocks(block_id):
    r = craft_get("/blocks", params={"id": block_id})
    return r.json()


def craft_post(markdown, doc_id, position="end"):
    r = requests.post(f"{CRAFT_API_BASE}/blocks",
                      json={"markdown": markdown,
                            "position": {"position": position, "pageId": doc_id}},
                      timeout=60)
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
    if isinstance(node, dict):
        out.append(node)
        for child in node.get("content", []) or []:
            _walk(child, out)
    elif isinstance(node, list):
        for child in node:
            _walk(child, out)


def _extract_json(markdown):
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


def read_section(section, blocks):
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
    old_ids = [b.get("id") for b in blocks
               if (_extract_json(b.get("markdown", "")) or {}).get("_section") == section]
    craft_delete(old_ids)
    stamp = dt.datetime.now(MELBOURNE).isoformat(timespec="seconds")
    for i, chunk in enumerate(_chunk_items(list(items))):
        payload = {"_section": section, "_part": i, "_updated": stamp, "items": chunk}
        markdown = "```json\n" + json.dumps(payload, separators=(",", ":")) + "\n```"
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
# PULL : Hevy completed workouts -> Craft Log detail_gym section
# --------------------------------------------------------------------------

def trim_workout(w):
    """Reduce a Hevy workout to the fields the Log needs for progression."""
    out = {
        "id": w.get("id"),
        "date": (w.get("start_time", "") or "")[:10],
        "title": w.get("title"),
        "updated_at": w.get("updated_at"),
        "exercises": [],
    }
    for ex in w.get("exercises", []) or []:
        tid = ex.get("exercise_template_id")
        name = ID_TO_NAME.get(tid, ex.get("title") or tid)
        sets = []
        for s in ex.get("sets", []) or []:
            row = {}
            if s.get("weight_kg") is not None:
                row["kg"] = s["weight_kg"]
            if s.get("reps") is not None:
                row["reps"] = s["reps"]
            if s.get("rpe") is not None:
                row["rpe"] = s["rpe"]
            if s.get("type") and s["type"] != "normal":
                row["type"] = s["type"]
            if row:
                sets.append(row)
        if sets:
            out["exercises"].append({"exercise": name, "sets": sets})
    return out


def _workout_hash(item):
    """Stable hash of the meaningful content, to detect edited workouts."""
    payload = json.dumps(item.get("exercises", []), sort_keys=True,
                         separators=(",", ":"))
    return hashlib.sha1((payload + str(item.get("updated_at"))).encode()).hexdigest()[:12]


def run_pull():
    if not HEVY_API_KEY:
        raise RuntimeError("HEVY_API_KEY not set.")
    doc_id = craft_document_id()
    blocks = []
    _walk(craft_get_blocks(doc_id), blocks)

    existing = read_section(GYM_SECTION, blocks)
    by_id = {it.get("id"): it for it in existing if it.get("id")}

    fetched = hevy_recent_workouts()
    changed = False
    for w in fetched:
        trimmed = trim_workout(w)
        wid = trimmed["id"]
        if not wid or not trimmed["exercises"]:
            continue
        trimmed["_h"] = _workout_hash(trimmed)
        prev = by_id.get(wid)
        if prev is None or prev.get("_h") != trimmed["_h"]:
            by_id[wid] = trimmed
            changed = True

    if not changed:
        print("No new or changed Hevy workouts. Nothing written.")
        return

    merged = sorted(by_id.values(), key=lambda x: x.get("date", ""), reverse=True)
    write_section(GYM_SECTION, merged, doc_id, blocks)
    print(f"Wrote {len(merged)} gym workouts to '{GYM_SECTION}' "
          f"({len(fetched)} fetched this run).")

    refresh_index(doc_id)


# --------------------------------------------------------------------------
# PUSH : calendar [HEVY-ROUTINE] blocks -> Hevy routines
# --------------------------------------------------------------------------

def parse_hevy_block(description):
    m = re.search(r"\[HEVY-ROUTINE\](.*?)\[/HEVY-ROUTINE\]", description or "",
                  re.DOTALL)
    if not m:
        return None
    title, exercises = None, []
    for line in m.group(1).strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if line.lower().startswith("title="):
            title = line.split("=", 1)[1].strip()
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 6:
            continue
        tid, _name, sets, reps, weight, rest = parts[:6]
        note = parts[6] if len(parts) > 6 else ""
        try:
            sets, reps, rest = int(sets), int(reps), int(rest)
        except ValueError:
            continue
        weight = float(weight) if weight else None
        set_objs = []
        # Warmup ramp on heavy barbell compounds: the prescribed set count
        # INCLUDES the warmups, so ramp sets come out of that total (e.g. 4
        # sets = 2 warmup + 2 working). Warmups are typed "warmup" and ramp to
        # a fraction of the top weight. Always leave at least 1 working set.
        # Skipped for reps-only lifts and when no weight is given.
        working = sets
        if tid in RAMP_IDS and tid not in REPS_ONLY_IDS and weight is not None:
            n_warm = min(len(RAMP_FRACTIONS), max(0, sets - 1))
            for frac in RAMP_FRACTIONS[:n_warm]:
                set_objs.append({"type": "warmup", "reps": reps,
                                 "weight_kg": _round_2p5(weight * frac)})
            working = sets - n_warm
        for _ in range(working):
            s = {"type": "normal", "reps": reps}
            if tid not in REPS_ONLY_IDS and weight is not None:
                s["weight_kg"] = weight
            set_objs.append(s)
        ex = {"exercise_template_id": tid, "rest_seconds": rest, "sets": set_objs}
        if note:
            ex["notes"] = note.replace("@", "")  # @ silently 400s in Hevy notes
        exercises.append(ex)
    if not title or not exercises:
        return None
    return {"routine": {"title": title, "folder_id": None,
                        "notes": title, "exercises": exercises}}


def _calendar_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    info = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/calendar.readonly"])
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def run_push():
    if not HEVY_API_KEY:
        raise RuntimeError("HEVY_API_KEY not set.")
    cal_id = os.environ["GCAL_CALENDAR_ID"]
    svc = _calendar_service()

    today = today_mel()
    time_min = dt.datetime.combine(today, dt.time.min, MELBOURNE).isoformat()
    time_max = dt.datetime.combine(today + dt.timedelta(days=28),
                                   dt.time.max, MELBOURNE).isoformat()
    events = svc.events().list(calendarId=cal_id, timeMin=time_min,
                               timeMax=time_max, singleEvents=True,
                               orderBy="startTime", maxResults=200).execute().get("items", [])

    existing = {r.get("title"): r for r in hevy_list_routines()}
    created, updated, skipped = 0, 0, 0
    for ev in events:
        routine = parse_hevy_block(ev.get("description", ""))
        if not routine:
            continue
        title = routine["routine"]["title"]
        if title in existing:
            rid = existing[title].get("id")
            # Hevy rejects folder_id on the update endpoint ("not allowed"),
            # though it is accepted on create. Strip it for the PUT only.
            update_body = {"routine": {k: v
                                       for k, v in routine["routine"].items()
                                       if k != "folder_id"}}
            hevy_put(f"/routines/{rid}", update_body)
            updated += 1
        else:
            hevy_post("/routines", routine)
            created += 1
    print(f"Push complete: {created} created, {updated} updated, {skipped} skipped.")


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True, choices=["pull", "push"])
    args = ap.parse_args()
    if args.mode == "pull":
        run_pull()
    else:
        run_push()


if __name__ == "__main__":
    main()
