#!/usr/bin/env python3
"""
push_workouts.py -- windowed upsert of planned workouts from a Google Calendar
into intervals.icu.

What it does, once per run:
  1. Reads events from the "Workout Planning" Google Calendar over a forward
     window (today .. today + WINDOW_DAYS).
  2. Keeps only FUTURE, PLANNED-format events (two gates -- see should_push).
  3. Builds an intervals.icu WORKOUT event for each, keyed by external_id
     derived from the Google Calendar event id.
  4. Reconciles against intervals.icu over the same window: create new, update
     changed, skip unchanged, and delete intervals workouts this script
     previously created whose source calendar event has gone.

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
  DRY_RUN                "1" to log actions without writing to intervals.icu
  TZID                   IANA tz for interpreting all-day events, default
                         Australia/Melbourne
"""

import os
import sys
import json
import base64
import hashlib
import datetime as dt
from typing import Optional

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
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"
TZID = os.environ.get("TZID", "Australia/Melbourne")

# intervals.icu workout types we emit
TYPE_RIDE = "Ride"
TYPE_RUN = "Run"
TYPE_SWIM = "Swim"
TYPE_WEIGHTS = "WeightTraining"
TYPE_OTHER = "Workout"

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
    """Return raw Google event dicts in [start, end)."""
    time_min = dt.datetime.combine(start, dt.time.min).isoformat() + "Z"
    time_max = dt.datetime.combine(end, dt.time.min).isoformat() + "Z"
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


def infer_type(summary: str, description: str) -> str:
    s = (summary or "").lower()
    d = (description or "").lower()
    if is_gym(summary, description):
        return TYPE_WEIGHTS
    first = d.strip().split()[0] if d.strip() else ""
    if first == "ride" or "ride" in s or "bike" in s or "cycl" in s:
        return TYPE_RIDE
    if first == "swim" or "swim" in s:
        return TYPE_SWIM
    if first == "run" or "run" in s:
        return TYPE_RUN
    # endurance headers like "Warmup"/"Threshold" without a discipline word:
    # fall back to the summary, else Run (XC context is running)
    if "ride" in s or "bike" in s:
        return TYPE_RIDE
    if "run" in s:
        return TYPE_RUN
    return TYPE_OTHER


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
        "name": summary,
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
            "fields": "id,external_id,name,description,type,start_date_local",
        }
        r = self.session.get(url, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def create(self, payload: dict):
        url = f"{INTERVALS_BASE}/athlete/{self.athlete}/events"
        # upsertOnUid: intervals.icu dedups on external_id server-side, so a
        # repeated create updates the existing event instead of adding a copy.
        # Belt-and-braces with the fields fix above -- duplicates become
        # impossible even if the local read-back match ever misses again.
        r = self.session.post(url, params={"upsertOnUid": "true"},
                              data=json.dumps(payload), timeout=30)
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

    today = dt.date.today()
    end = today + dt.timedelta(days=WINDOW_DAYS)
    log(f"[push] window {today} .. {end}  (dry_run={DRY_RUN})")

    # ---- read calendar
    svc = gcal_service()
    raw_events = read_calendar_window(svc, calendar_id, today, end)
    log(f"[push] calendar returned {len(raw_events)} events in window")

    desired = {}     # external_id -> payload
    for ev in raw_events:
        if should_push(ev, today):
            pid = external_id_for(ev)
            desired[pid] = build_payload(ev)
    log(f"[push] {len(desired)} events pass the gates (future + planned format)")

    # ---- read intervals existing (only ones we own)
    icu = Intervals(athlete_id, api_key)
    existing_all = icu.list_events(today, end)
    existing = {e["external_id"]: e for e in existing_all
                if (e.get("external_id") or "").startswith(EXTERNAL_ID_PREFIX)}
    log(f"[push] intervals has {len(existing)} workouts we own in window")

    created = updated = skipped = deleted = 0

    # ---- create / update
    for pid, payload in desired.items():
        if pid not in existing:
            log(f"  + create {payload['start_date_local'][:10]} {payload['type']:14} {payload['name'][:48]}")
            if not DRY_RUN:
                icu.create(payload)
            created += 1
        elif payload_differs(existing[pid], payload):
            log(f"  ~ update {payload['start_date_local'][:10]} {payload['type']:14} {payload['name'][:48]}")
            if not DRY_RUN:
                icu.update(existing[pid]["id"], payload)
            updated += 1
        else:
            skipped += 1

    # ---- delete ours that no longer have a source event
    for pid, ev in existing.items():
        if pid not in desired:
            log(f"  - delete {(ev.get('start_date_local') or '')[:10]} {ev.get('name','')[:48]}")
            if not DRY_RUN:
                icu.delete(ev["id"])
            deleted += 1

    log(f"[push] done: created={created} updated={updated} "
        f"skipped={skipped} deleted={deleted}")
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
