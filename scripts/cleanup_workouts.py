#!/usr/bin/env python3
"""
cleanup_workouts.py -- one-off: delete ALL planned workouts this pipeline owns
(external_id prefix "wkplan-") from intervals.icu over a forward window, so
the fixed push_workouts.py can recreate them cleanly.

Deletes only WORKOUT-category events whose external_id starts with "wkplan-".
Hand-made workouts and anything from other tools are untouched.

Env:
  INTERVALS_ATHLETE_ID   e.g. i12345
  INTERVALS_API_KEY      intervals.icu personal API key
Optional:
  START_OFFSET_DAYS      window start = today + this, default 1 (tomorrow)
  WINDOW_DAYS            window length from today, default 14
  DRY_RUN                "1" to log without deleting
"""

import os
import sys
import json
import datetime as dt

import requests

INTERVALS_BASE = "https://intervals.icu/api/v1"
EXTERNAL_ID_PREFIX = "wkplan-"
START_OFFSET_DAYS = int(os.environ.get("START_OFFSET_DAYS", "1"))
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "14"))
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"


def _env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        print(f"ERROR: missing required env var {name}", file=sys.stderr)
        sys.exit(2)
    return val


def main() -> int:
    athlete = _env("INTERVALS_ATHLETE_ID")
    api_key = _env("INTERVALS_API_KEY")

    today = dt.date.today()
    start = today + dt.timedelta(days=START_OFFSET_DAYS)
    end = today + dt.timedelta(days=WINDOW_DAYS)
    print(f"[cleanup] window {start} .. {end} inclusive  (dry_run={DRY_RUN})",
          flush=True)

    s = requests.Session()
    s.auth = ("API_KEY", api_key)
    s.headers.update({"Content-Type": "application/json"})

    r = s.get(
        f"{INTERVALS_BASE}/athlete/{athlete}/events",
        params={
            "oldest": start.isoformat(),
            "newest": end.isoformat(),
            "category": "WORKOUT",
            "fields": "id,external_id,name,type,start_date_local",
        },
        timeout=30,
    )
    r.raise_for_status()
    events = r.json()

    owned = [e for e in events
             if (e.get("external_id") or "").startswith(EXTERNAL_ID_PREFIX)]
    print(f"[cleanup] {len(events)} WORKOUT events in window, "
          f"{len(owned)} owned (prefix {EXTERNAL_ID_PREFIX!r})", flush=True)

    deleted = failed = 0
    for e in owned:
        label = (f"{(e.get('start_date_local') or '')[:10]} "
                 f"{e.get('type', ''):14} {e.get('name', '')[:48]} "
                 f"(id={e['id']}, ext={e.get('external_id')})")
        print(f"  - delete {label}", flush=True)
        if DRY_RUN:
            deleted += 1
            continue
        try:
            dr = s.delete(f"{INTERVALS_BASE}/athlete/{athlete}/events/{e['id']}",
                          timeout=30)
            dr.raise_for_status()
            deleted += 1
        except requests.HTTPError as ex:
            failed += 1
            body = ""
            try:
                body = ex.response.text[:200]
            except Exception:
                pass
            print(f"    ! failed: HTTP "
                  f"{ex.response.status_code if ex.response else '?'} {body}",
                  file=sys.stderr, flush=True)

    print(f"[cleanup] done: deleted={deleted} failed={failed}", flush=True)
    return 1 if failed else 0


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
