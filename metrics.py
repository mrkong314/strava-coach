#!/usr/bin/env python3
"""
metrics.py - per-second stream metrics job for the Training Log.

For every aerobic activity (Run / Ride / VirtualRide, >= 15 min) in the
Log's 90-day window that has no metrics entry yet, fetch the per-second
streams from Intervals.icu, compute the metric set in stream_core.py
(band histograms, surges, EF drift v2, HRR60, W'bal, climb splits, EF at
reference HR), and write one compact entry into a managed `detail_metrics`
section of the Training Log. Entries are computed once (streams are static
after an activity completes) and dropped when the activity ages out of the
window - same lifecycle as detail_laps.

Run on a cron (see .github/workflows/metrics.yml). Safe to re-run; writes
only when the entry set changes. Calls refresh_index afterwards so the
skills can find the new part-blocks without search.

Constants (LTHR, fallback FTPs, W') are read at runtime from the Log's
`athlete_constants` section, which Claude maintains in chat whenever the
Coaching Reference changes - no repo edit needed on a constant change.
The env values below are fallbacks only, used if that block is absent.

Environment:
  INTERVALS_ATHLETE_ID, INTERVALS_API_KEY, CRAFT_API_BASE  (as coach.py)
  LTHR_BIKE (default 165), LTHR_RUN (default 175)  - fallbacks only
  WPRIME_J (default 20000) - fallback W' estimate for W'bal
  METRICS_MAX_NEW (default 60) - new activities processed per run; the
    90-day backfill completes over the first few cron cycles
"""

import os
import re
import json
import argparse
import datetime as dt

import requests

from stream_core import compute_metrics

INTERVALS_ATHLETE_ID = os.environ["INTERVALS_ATHLETE_ID"]
INTERVALS_API_KEY    = os.environ["INTERVALS_API_KEY"]
CRAFT_API_BASE       = os.environ["CRAFT_API_BASE"].rstrip("/")

LTHR_BIKE = int(os.environ.get("LTHR_BIKE", "165"))
LTHR_RUN  = int(os.environ.get("LTHR_RUN", "175"))
WPRIME_J  = int(os.environ.get("WPRIME_J", "20000"))
MAX_NEW   = int(os.environ.get("METRICS_MAX_NEW", "60"))

INTERVALS_BASE = "https://intervals.icu/api/v1"
MELBOURNE = dt.timezone(dt.timedelta(hours=10))
JSON_BUDGET = 8500
SECTION = "detail_metrics"
AERO_TYPES = {"run": "Run", "ride": "Ride", "virtualride": "VirtualRide"}
MIN_MINUTES = 15


def iget(path, params=None):
    r = requests.get(f"{INTERVALS_BASE}{path}", params=params or {},
                     auth=("API_KEY", INTERVALS_API_KEY), timeout=120)
    r.raise_for_status()
    return r.json()


def fetch_streams(activity_id):
    """Fetch per-second streams; tolerate list-of-objects or dict shapes."""
    data = iget(f"/activity/{activity_id}/streams.json")
    streams = {}
    if isinstance(data, list):
        for s in data:
            if isinstance(s, dict) and s.get("type"):
                streams[s["type"]] = s.get("data")
    elif isinstance(data, dict):
        for k, v in data.items():
            streams[k] = v.get("data") if isinstance(v, dict) else v
    return streams


# ---- Craft helpers (as coach.py) ----------------------------------------

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
    r = requests.post(f"{CRAFT_API_BASE}/blocks",
                      json={"markdown": markdown,
                            "position": {"position": position,
                                         "pageId": doc_id}}, timeout=60)
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
               if (_extract_json(b.get("markdown", "")) or {}).get("_section")
               == section]
    craft_delete(old_ids)
    stamp = dt.datetime.now(MELBOURNE).isoformat(timespec="seconds")
    for i, chunk in enumerate(_chunk_items(list(items))):
        payload = {"_section": section, "_part": i,
                   "_updated": stamp, "items": chunk}
        craft_post("```json\n"
                   + json.dumps(payload, separators=(",", ":"))
                   + "\n```", doc_id)


# ---- Log index refresh (as coach.py; keep the three in sync) -------------

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
    import hashlib
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
               "activities": acts, "gym": gym}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True,
                   separators=(",", ":")).encode()).hexdigest()


def refresh_index(doc_id):
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
        today_iso = dt.datetime.now(MELBOURNE).date().isoformat()
        payload = {"_section": INDEX_SECTION, "_part": 0,
                   "marker": INDEX_MARKER,
                   "_updated": dt.datetime.now(MELBOURNE)
                                 .isoformat(timespec="seconds"),
                   "today": {"date": today_iso,
                             "hash": _today_content_hash(blocks, today_iso)},
                   "parts": parts}
        craft_delete(old_ids)
        craft_post("```json\n"
                   + json.dumps(payload, separators=(",", ":"))
                   + "\n```", doc_id)
        print(f"log_index refreshed "
              f"({sum(len(v) for v in parts.values())} part blocks).")
    except Exception as e:  # noqa: BLE001
        print(f"log_index refresh failed (non-fatal): {e}")


# ---- main ---------------------------------------------------------------

def eligible(a):
    t = AERO_TYPES.get((a.get("type") or "").lower())
    if not t:
        return None
    if (a.get("minutes") or 0) < MIN_MINUTES:
        return None
    return t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    doc_id = craft_document_id()
    blocks = []
    _walk(craft_get_blocks(doc_id), blocks)

    # Runtime constants from the Log's athlete_constants block (maintained
    # by Claude alongside the Coaching Reference); env values are fallbacks.
    const_rows = read_section("athlete_constants", blocks)
    const = const_rows[0] if const_rows else {}
    lthr_bike = int(const.get("lthr_bike") or LTHR_BIKE)
    lthr_run  = int(const.get("lthr_run") or LTHR_RUN)
    ftp_bike  = int(const.get("ftp_bike") or 205)
    ftp_run   = int(const.get("ftp_run") or 354)
    wprime    = int(const.get("wprime_j") or WPRIME_J)
    src = "athlete_constants" if const else "env fallback"
    print(f"constants ({src}): LTHR run {lthr_run} / bike {lthr_bike}, "
          f"fallback FTP run {ftp_run} / bike {ftp_bike}, W' {wprime} J")

    activities = read_section("detail_activity", blocks)
    stored = {m.get("aid"): m for m in read_section(SECTION, blocks)}
    window_aids = {str(a.get("id")) for a in activities}

    todo = []
    for a in activities:
        sport = eligible(a)
        aid = str(a.get("id"))
        if sport and aid not in stored:
            todo.append((a, sport))
    todo.sort(key=lambda x: x[0].get("start_date_local") or "", reverse=True)

    done, failed = 0, 0
    for a, sport in todo[:MAX_NEW]:
        aid = str(a.get("id"))
        try:
            s = fetch_streams(aid)
            t = s.get("time") or list(range(
                len(s.get("watts") or s.get("heartrate") or [])))
            if not t or (not s.get("watts") and not s.get("heartrate")):
                stored[aid] = {"aid": aid,
                               "date": (a.get("start_date_local") or "")[:10],
                               "sport": sport, "schema": "metrics-v1",
                               "note": "no usable streams"}
                done += 1
                continue
            ftp = a.get("icu_ftp") or (ftp_bike if sport != "Run" else ftp_run)
            lthr = lthr_run if sport == "Run" else lthr_bike
            m = compute_metrics(
                t, s.get("watts"), s.get("heartrate"), s.get("cadence"),
                s.get("velocity_smooth"), s.get("altitude"),
                s.get("distance"), ftp=ftp, lthr=lthr, sport=sport,
                wprime=wprime)
            m["aid"] = aid
            m["date"] = (a.get("start_date_local") or "")[:10]
            stored[aid] = m
            done += 1
        except Exception as e:  # noqa: BLE001 - per-activity isolation
            failed += 1
            print(f"  metrics failed for {aid}: {e}")

    merged = [m for aid, m in stored.items() if aid in window_aids]
    merged.sort(key=lambda m: m.get("date", ""), reverse=True)

    changed = (done > 0) or (set(stored) - window_aids)
    remaining = max(0, len(todo) - MAX_NEW)
    print(f"metrics: {done} computed, {failed} failed, "
          f"{remaining} remaining in backlog, {len(merged)} stored.")
    if args.dry_run:
        print("(dry run - nothing written)")
        return
    if changed:
        write_section(SECTION, merged, doc_id, blocks)
        refresh_index(doc_id)
    else:
        print("No changes - nothing written.")


if __name__ == "__main__":
    main()
