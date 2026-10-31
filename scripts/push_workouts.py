#!/usr/bin/env python3
"""
push_workouts.py -- windowed upsert of planned workouts from a Google Calendar
into intervals.icu.

What it does, once per run:
  1. Reads events from the "Workout Planning" Google Calendar over a forward
     window (today .. today + WINDOW_DAYS).
  2. Keeps only FUTURE, PLANNED-format events (two gates -- see should_push).
  3. Drops today's events whose discipline is already completed: intervals.icu
     activities recorded today are counted per discipline group, and today's
     planned sessions of that group are treated as done only once the activity
     count reaches the planned count (so a double day stays planned until both
     sessions exist).
  4. Builds an intervals.icu WORKOUT event for each, keyed by external_id
     derived from the Google Calendar event id.
  5. Reconciles against intervals.icu over the window plus PAST_DAYS back:
     create new, update changed, skip unchanged, and delete intervals workouts
     this script previously created whose source calendar event has gone or
     whose date/discipline is completed. Workouts intervals.icu has paired
     with an activity are never updated or deleted.

It is idempotent: running it repeatedly converges to the same state with no
duplicates. It only ever touches intervals events whose external_id carries our
prefix, so it never disturbs workouts created by hand or by other tools.

Source of truth is the calendar. The calendar's planned-event descriptions hold
the intervals.icu workout text (endurance) or the gym exercise list (strength);
past/reconciled events use a different prose format and are skipped by the
format gate.

Auth / config come from environment variables (see README):
  INTERVALS_ATHLETE_ID   e.g. i12345
  INTERVALS_API_KEY      intervals.icu personal API key
  GCAL_CALENDAR_ID       the Workout Planning calendar id
  GOOGLE_CREDENTIALS_JSON   service-account JSON (the whole blob), OR
  GOOGLE_CREDENTIALS_FILE   path to the service-account JSON file
Optional:
  WINDOW_DAYS            forward window, default 14
  PAST_DAYS              how far back owned workouts are reconciled (unpaired
                         ones on past dates are deleted), default 7
  DRY_RUN                "1" to log actions without writing to intervals.icu
  TZID                   IANA tz that defines "today" and the calendar
                         window boundaries (GitHub runners are UTC), default
                         Australia/Melbourne
"""

import os
import re
import sys
import json
import base64
import hashlib
import datetime as dt
from collections import Counter
from typing import Optional
from zoneinfo import ZoneInfo

import requests

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build as gcal_build
except ImportError:
    print("ERROR: google-api-python-client and google-auth are required. "
          "pip install -r requirements.txt", file=sys.stderr)
    raise

# --------------------------------------------------------------------------- config

INTERVALS_BASE = "https://intervals.icu/api/v1"
EXTERNAL_ID_PREFIX = "wkplan-"          # marks events THIS script owns
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "14"))
PAST_DAYS = int(os.environ.get("PAST_DAYS", "7"))
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"
TZID = os.environ.get("TZID", "Australia/Melbourne")

# intervals.icu workout types we emit
TYPE_RIDE = "Ride"
TYPE_RUN = "Run"
TYPE_SWIM = "Swim"
TYPE_WEIGHTS = "WeightTraining"
TYPE_OTHER = "Workout"

# intervals.icu activity type -> the planned type we emit, so a VirtualRide
# completes a planned Ride and a TrailRun completes a planned Run.
_TYPE_GROUP = {
    "Ride": TYPE_RIDE, "VirtualRide": TYPE_RIDE, "GravelRide": TYPE_RIDE,
    "MountainBikeRide": TYPE_RIDE, "EBikeRide": TYPE_RIDE,
    "Run": TYPE_RUN, "VirtualRun": TYPE_RUN, "TrailRun": TYPE_RUN,
    "Swim": TYPE_SWIM, "OpenWaterSwim": TYPE_SWIM,
    "WeightTraining": TYPE_WEIGHTS,
}


def type_group(t: Optional[str]) -> str:
    return _TYPE_GROUP.get(t or "", t or "")

# First-token keywords that mark a parseable endurance workout description.
# intervals.icu step syntax begins with a discipline word or a step header.
ENDURANCE_HEADERS = {
    "ride", "run", "swim", "warmup", "warm up", "warm-up", "cooldown",
    "cool down", "cool-down", "endurance", "threshold", "tempo", "vo2",
    "recovery", "intervals", "main", "main set",
}

# Markers that identify a PAST/reconciled actual event -- never push these.
ACTUAL_MARKERS = ("ACTUAL", "PLANNED", "WELLNESS")


# --------------------------------------------------------------------------- helpers

def _env(name: str, required: bool = True) -> Optional[str]:
    val = os.environ.get(name)
    if required and not val:
        print(f"ERROR: missing required env var {name}", file=sys.stderr)
        sys.exit(2)
    return val


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- google calendar

def gcal_service():
    raw = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    path = os.environ.get("GOOGLE_CREDENTIALS_FILE")
    if raw:
        # allow either plain JSON or base64-encoded JSON
        try:
            info = json.loads(raw)
        except json.JSONDecodeError:
            info = json.loads(base64.b64decode(raw).decode("utf-8"))
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/calendar.readonly"])
    elif path:
        creds = service_account.Credentials.from_service_account_file(
            path, scopes=["https://www.googleapis.com/auth/calendar.readonly"])
    else:
        print("ERROR: set GOOGLE_CREDENTIALS_JSON or GOOGLE_CREDENTIALS_FILE",
              file=sys.stderr)
        sys.exit(2)
    return gcal_build("calendar", "v3", credentials=creds, cache_discovery=False)


def read_calendar_window(svc, calendar_id: str, start: dt.date, end: dt.date):
    """Return raw Google event dicts in [start, end), with the day
    boundaries taken at local midnight in TZID (not UTC midnight)."""
    tz = ZoneInfo(TZID)
    time_min = dt.datetime.combine(start, dt.time.min, tz).isoformat()
    time_max = dt.datetime.combine(end, dt.time.min, tz).isoformat()
    events, page_token = [], None
    while True:
        resp = svc.events().list(
            calendarId=calendar_id, timeMin=time_min, timeMax=time_max,
            singleEvents=True, orderBy="startTime", maxResults=250,
            pageToken=page_token,
        ).execute()
        events.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return events


# --------------------------------------------------------------------------- gates & mapping

def event_date(ev) -> Optional[dt.date]:
    """All-day events carry start.date; timed events carry start.dateTime."""
    start = ev.get("start", {})
    if "date" in start:
        return dt.date.fromisoformat(start["date"])
    if "dateTime" in start:
        return dt.datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00")).date()
    return None


def looks_like_actual(description: str) -> bool:
    head = description.strip()[:400].upper()
    hits = sum(1 for m in ACTUAL_MARKERS if m in head)
    return hits >= 2          # ACTUAL + PLANNED (+ WELLNESS) => reconciled prose


def is_gym(summary: str, description: str) -> bool:
    return "gym" in (summary or "").lower()


def looks_like_endurance_steps(description: str) -> bool:
    first_line = description.strip().splitlines()[0].strip().lower() if description.strip() else ""
    if first_line in ENDURANCE_HEADERS:
        return True
    # also accept "<word> ..." where the first word is a header (e.g. "Ride")
    first_word = first_line.split()[0] if first_line else ""
    return first_word in ENDURANCE_HEADERS


def should_push(ev, today: dt.date) -> bool:
    """Two gates: future-dated AND parseable planned format."""
    d = event_date(ev)
    if d is None or d < today:
        return False                      # gate 1: never push past/undated
    desc = ev.get("description", "") or ""
    if not desc.strip():
        return False                      # nothing to push (e.g. bare XC note)
    if looks_like_actual(desc):
        return False                      # gate 2a: reconciled actual -> skip
    summary = ev.get("summary", "") or ""
    if is_gym(summary, desc):
        return True                       # gate 2b: gym, exercise list
    return looks_like_endurance_steps(desc)   # gate 2c: endurance step syntax


_DISCIPLINE_WORDS = {
    "ride": TYPE_RIDE, "bike": TYPE_RIDE, "cycle": TYPE_RIDE, "cycling": TYPE_RIDE,
    "swim": TYPE_SWIM,
    "run": TYPE_RUN,
}


def discipline_in(text: Optional[str]) -> Optional[str]:
    """Workout type from the first discipline word in text, else None.

    Scans whole words left to right, so "Run 15min off the bike" is a Run
    and "Long ride 270min" is a Ride."""
    for m in re.finditer(r'[a-z]+', (text or "").lower()):
        wtype = _DISCIPLINE_WORDS.get(m.group(0))
        if wtype:
            return wtype
    return None


def infer_type(summary: str, description: str) -> str:
    d = (description or "").lower()
    if is_gym(summary, description):
        return TYPE_WEIGHTS
    # The "Name:" line in the description is the primary source; the calendar
    # summary is the fallback when the name carries no discipline word
    # (e.g. "Threshold 3x15 @100% + Z2").
    wtype = discipline_in(name_from_description(description)) or discipline_in(summary)
    if wtype:
        return wtype
    # Last resort: a description whose first line is a discipline header.
    first = d.strip().split()[0] if d.strip() else ""
    if first == "ride":
        return TYPE_RIDE
    if first == "swim":
        return TYPE_SWIM
    if first == "run":
        return TYPE_RUN
    return TYPE_OTHER


def name_from_description(description: str) -> Optional[str]:
    """Planned descriptions carry an explicit workout name.

    Endurance events end with a "Name: <text>" line; gym events carry
    "title=<text>" inside the [HEVY-ROUTINE] block. Returns None when neither
    is present so the caller can fall back to the calendar summary."""
    for line in (description or "").splitlines():
        m = re.match(r'^\s*name\s*:\s*(.+?)\s*$', line, re.IGNORECASE)
        if m:
            return m.group(1)
    for line in (description or "").splitlines():
        m = re.match(r'^\s*title\s*=\s*(.+?)\s*$', line, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def external_id_for(ev) -> str:
    """Stable per calendar event. Google event ids are already unique; hash to
    keep length bounded and avoid odd characters."""
    gid = ev["id"]
    h = hashlib.sha1(gid.encode("utf-8")).hexdigest()[:16]
    return f"{EXTERNAL_ID_PREFIX}{h}"


def infer_moving_time_secs(description: str, wtype: str) -> Optional[int]:
    """Best-effort total duration in seconds.

    Endurance step syntax encodes per-step minutes like '- 180m 65%' or
    '- 8m 95%' with repeats ('3x'). We sum step minutes, honouring a leading
    'Nx' repeat header for the block that follows. Gym has no steps; fall back
    to a duration mentioned in the text ('~45min') or a default.
    """
    import re
    lines = [l.strip() for l in description.splitlines()]
    if wtype == TYPE_WEIGHTS:
        m = re.search(r'~?\s*(\d{2,3})\s*min', description.lower())
        return int(m.group(1)) * 60 if m else 45 * 60
    total = 0.0
    repeat = 1
    block = []
    block_min = 0.0

    def flush():
        nonlocal total, repeat, block_min
        total += repeat * block_min
        repeat = 1
        block_min = 0.0

    for l in lines:
        lm = l.lower()
        rep = re.match(r'^(\d+)\s*x$', lm)          # e.g. "3x"
        if rep:
            flush()
            repeat = int(rep.group(1))
            continue
        # step line: "- 8m 95%"  or "- 12m ramp 50-70%"
        sm = re.match(r'^-\s*(\d+(?:\.\d+)?)\s*m\b', lm)
        if sm:
            block_min += float(sm.group(1))
            continue
        # a non-step, non-repeat line ends any open repeat block
        if l and not l.startswith("-") and not lm.endswith("x"):
            # header like "Threshold" / "Name: ..." -> close current block
            if repeat != 1 or block_min:
                flush()
    flush()
    secs = int(round(total * 60))
    return secs if secs > 0 else None


def build_payload(ev) -> dict:
    summary = ev.get("summary", "") or "Workout"
    description = (ev.get("description", "") or "").strip()
    d = event_date(ev)
    wtype = infer_type(summary, description)
    payload = {
        "category": "WORKOUT",
        "start_date_local": f"{d.isoformat()}T00:00:00",
        "type": wtype,
        "name": name_from_description(description) or summary,
        "description": description,
        "external_id": external_id_for(ev),
    }
    secs = infer_moving_time_secs(description, wtype)
    if secs:
        payload["moving_time"] = secs
    return payload


def payload_differs(existing: dict, desired: dict) -> bool:
    for k in ("name", "description", "type"):
        if (existing.get(k) or "") != (desired.get(k) or ""):
            return True
    # compare date portion only
    ex_date = (existing.get("start_date_local") or "")[:10]
    de_date = (desired.get("start_date_local") or "")[:10]
    return ex_date != de_date


# --------------------------------------------------------------------------- intervals.icu

class Intervals:
    def __init__(self, athlete_id: str, api_key: str):
        self.athlete = athlete_id
        self.session = requests.Session()
        # HTTP basic with username "API_KEY" is the classic form and works on
        # all endpoints. (The docs also show "Authorization: ApiKey ID:KEY.")
        self.session.auth = ("API_KEY", api_key)
        self.session.headers.update({"Content-Type": "application/json"})

    def list_events(self, start: dt.date, end: dt.date):
        url = f"{INTERVALS_BASE}/athlete/{self.athlete}/events"
        params = {
            "oldest": start.isoformat(),
            "newest": (end - dt.timedelta(days=1)).isoformat(),
            "category": "WORKOUT",
            # Request external_id explicitly. The list endpoint's default
            # field set does not reliably include it; without it every owned
            # workout is invisible to the reconcile and gets re-created each
            # run (the duplicate pile-up).
            # paired_activity_id tells us intervals.icu has matched the
            # workout to a completed activity; such workouts are left alone.
            "fields": "id,external_id,name,description,type,start_date_local,"
                      "paired_activity_id",
        }
        r = self.session.get(url, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def list_activities(self, start: dt.date, end: dt.date):
        """Activities with a local start date in [start, end] inclusive."""
        url = f"{INTERVALS_BASE}/athlete/{self.athlete}/activities"
        params = {"oldest": start.isoformat(),
                  "newest": f"{end.isoformat()}T23:59:59"}
        r = self.session.get(url, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def create(self, payload: dict):
        # Use the documented bulk upsert keyed on external_id. intervals.icu
        # matches external_id against events THIS application created, so a
        # repeated create updates the existing event instead of adding a copy.
        # (The old single-event upsertOnUid=true keyed on the server-generated
        # `uid`, which we never set, so it silently inserted every run -- that
        # was the duplicate-pileup cause.) Sending a one-element array keeps the
        # per-event call structure unchanged.
        url = f"{INTERVALS_BASE}/athlete/{self.athlete}/events/bulk"
        r = self.session.post(url, params={"upsert": "true"},
                              data=json.dumps([payload]), timeout=30)
        r.raise_for_status()
        return r.json()

    def update(self, event_id, payload: dict):
        url = f"{INTERVALS_BASE}/athlete/{self.athlete}/events/{event_id}"
        r = self.session.put(url, data=json.dumps(payload), timeout=30)
        r.raise_for_status()
        return r.json()

    def delete(self, event_id):
        url = f"{INTERVALS_BASE}/athlete/{self.athlete}/events/{event_id}"
        r = self.session.delete(url, timeout=30)
        r.raise_for_status()


# --------------------------------------------------------------------------- main

def main() -> int:
    athlete_id = _env("INTERVALS_ATHLETE_ID")
    api_key = _env("INTERVALS_API_KEY")
    calendar_id = _env("GCAL_CALENDAR_ID")

    today = dt.datetime.now(ZoneInfo(TZID)).date()   # local date, not UTC
    end = today + dt.timedelta(days=WINDOW_DAYS)
    past_start = today - dt.timedelta(days=PAST_DAYS)
    log(f"[push] window {today} .. {end}, reconcile back to {past_start}  "
        f"(dry_run={DRY_RUN})")

    # ---- read calendar
    svc = gcal_service()
    raw_events = read_calendar_window(svc, calendar_id, today, end)
    log(f"[push] calendar returned {len(raw_events)} events in window")

    icu = Intervals(athlete_id, api_key)

    # ---- completion signal: activities already recorded today in intervals.icu,
    # counted per discipline group. Today's planned sessions of a group count
    # as done only once the activity count reaches the planned count, so a
    # double day (two rides) stays planned until both rides exist.
    done_today = Counter(type_group(a.get("type"))
                         for a in icu.list_activities(today, today))
    log(f"[push] completed today: {dict(done_today) or 'none'}")

    candidates = [(ev, build_payload(ev)) for ev in raw_events
                  if should_push(ev, today)]
    planned_today = Counter(type_group(p["type"]) for ev, p in candidates
                            if event_date(ev) == today)

    desired = {}     # external_id -> payload
    completed = 0
    for ev, payload in candidates:
        grp = type_group(payload["type"])
        if event_date(ev) == today and done_today[grp] >= planned_today[grp]:
            log(f"  = done   {today} {payload['type']:14} {payload['name'][:48]} "
                f"({done_today[grp]}/{planned_today[grp]} {grp})")
            completed += 1
            continue                      # gate 3: today's session completed
        desired[external_id_for(ev)] = payload
    log(f"[push] {len(desired)} events pass the gates (future + planned format), "
        f"{completed} completed today")

    # ---- read intervals existing (only ones we own), grouped by external_id.
    # A dict keyed on external_id would collapse a duplicate pile to one entry,
    # hiding the extras from reconcile -- which is how a pile can never shrink.
    # Group into lists so we can see and collapse duplicates below.
    # The read reaches PAST_DAYS back so owned workouts on past dates are
    # reconciled too (unpaired ones deleted, paired ones kept).
    existing_all = icu.list_events(past_start, end)
    existing = {}    # external_id -> list[event]
    for e in existing_all:
        eid = e.get("external_id") or ""
        if eid.startswith(EXTERNAL_ID_PREFIX):
            existing.setdefault(eid, []).append(e)
    owned_count = sum(len(v) for v in existing.values())
    dup_ids = sum(1 for v in existing.values() if len(v) > 1)
    log(f"[push] intervals has {owned_count} workouts we own in window "
        f"across {len(existing)} ids ({dup_ids} with duplicates)")

    created = updated = skipped = deleted = kept = 0

    # ---- create / update, collapsing any duplicate pile to a single event
    for pid, payload in desired.items():
        matches = existing.get(pid, [])
        if not matches:
            log(f"  + create {payload['start_date_local'][:10]} {payload['type']:14} {payload['name'][:48]}")
            if not DRY_RUN:
                icu.create(payload)
            created += 1
            continue
        # keep a paired match if there is one, else the first; delete extras
        keep = next((m for m in matches if m.get("paired_activity_id")), matches[0])
        for extra in matches:
            if extra is keep:
                continue
            log(f"  - dedup  {(extra.get('start_date_local') or '')[:10]} {extra.get('name','')[:48]}")
            if not DRY_RUN:
                icu.delete(extra["id"])
            deleted += 1
        if keep.get("paired_activity_id"):
            kept += 1                     # never touch a paired workout
        elif payload_differs(keep, payload):
            log(f"  ~ update {payload['start_date_local'][:10]} {payload['type']:14} {payload['name'][:48]}")
            if not DRY_RUN:
                icu.update(keep["id"], payload)
            updated += 1
        else:
            skipped += 1

    # ---- delete ours that no longer have a source event, are past-dated, or
    # are completed today (all copies), except any intervals.icu has paired
    for pid, matches in existing.items():
        if pid not in desired:
            for ev in matches:
                if ev.get("paired_activity_id"):
                    log(f"  = keep   {(ev.get('start_date_local') or '')[:10]} {ev.get('name','')[:48]} (paired)")
                    kept += 1
                    continue
                log(f"  - delete {(ev.get('start_date_local') or '')[:10]} {ev.get('name','')[:48]}")
                if not DRY_RUN:
                    icu.delete(ev["id"])
                deleted += 1

    log(f"[push] done: created={created} updated={updated} "
        f"skipped={skipped} deleted={deleted} kept_paired={kept}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except requests.HTTPError as e:
        body = ""
        try:
            body = e.response.text[:500]
        except Exception:
            pass
        print(f"ERROR: HTTP {e.response.status_code if e.response else '?'} "
              f"from intervals.icu: {body}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
