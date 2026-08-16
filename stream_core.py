"""
stream_core.py - per-second stream metrics for aerobic activities.

Pure functions, stdlib only. Shared verbatim between the strava-coach repo
(metrics.py, which fetches streams from Intervals and writes detail_metrics
into the Training Log) and the today-brief skill (scripts/analyse_streams.py,
the ad hoc fallback when a detail_metrics entry is absent).

Input: parallel per-second arrays (time seconds, watts, heartrate, cadence,
velocity m/s, altitude m, distance m - any may be None) plus ftp, lthr,
sport. Output: one compact metrics dict (schema v1).

Design notes:
- "moving" = samples where velocity > 0.3 m/s when velocity exists, else all
  samples with a forward time step <= 10 s.
- Power histogram: seconds in 5%-of-FTP buckets from <40% to >130%, so any
  cap or zone question ("time above X% FTP") is answerable later without
  re-touching streams. HR histogram likewise in %LTHR buckets.
- Decoupling v2 = lag-corrected windowed EF slope (%/hour, positive = HR
  rising for the same power). Legacy first-half/second-half Pw:Hr kept for
  continuity.
- All values rounded; absent inputs produce absent keys, never fabricated.
"""

SCHEMA = "metrics-v1"

PW_EDGES = [0] + [round(0.40 + 0.05 * i, 2) for i in range(19)] + [99.0]
# buckets: <40%, 40-45 ... 125-130, >130  (21 buckets, fractions of FTP)
HR_EDGES = [0, 0.68, 0.81, 0.85, 0.89, 0.94, 1.00, 1.03, 1.06, 99.0]
# generic Friel-boundary superset (%LTHR): covers bike+run zone edges


def _clean(t, arr):
    return [(a if isinstance(a, (int, float)) else None) for a in arr] \
        if arr else None


def moving_mask(t, velocity):
    n = len(t)
    mask = [True] * n
    for i in range(1, n):
        if t[i] - t[i - 1] > 10:
            mask[i] = False
    if velocity:
        for i in range(n):
            v = velocity[i]
            if v is not None and v <= 0.3:
                mask[i] = False
    return mask


def _vals(arr, mask):
    return [a for a, m in zip(arr, mask) if m and a is not None]


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _pct(xs, p):
    if not xs:
        return None
    ys = sorted(xs)
    k = min(len(ys) - 1, max(0, int(round(p / 100.0 * (len(ys) - 1)))))
    return ys[k]


def rolling30(arr):
    out, s, q = [], 0.0, []
    for a in arr:
        a = a or 0
        q.append(a)
        s += a
        if len(q) > 30:
            s -= q.pop(0)
        out.append(s / len(q))
    return out


def np_power(watts):
    if not watts:
        return None
    r = rolling30(watts)
    m = sum(x ** 4 for x in r) / len(r)
    return m ** 0.25


def histogram(vals, anchor, edges):
    counts = [0] * (len(edges) - 1)
    for v in vals:
        f = v / anchor
        for i in range(len(edges) - 1):
            if edges[i] <= f < edges[i + 1]:
                counts[i] += 1
                break
    return counts


def runs_above(flags, min_len):
    """(count, total_s, max_s) of consecutive-True runs of length >= min_len,
    tolerating single-sample dips of <= 5 s inside a run."""
    events, cur, dip = [], 0, 0
    for f in flags:
        if f:
            cur += dip + 1
            dip = 0
        else:
            if cur:
                dip += 1
                if dip > 5:
                    events.append(cur)
                    cur, dip = 0, 0
            else:
                dip = 0
    if cur:
        events.append(cur)
    events = [e for e in events if e >= min_len]
    return (len(events), sum(events), max(events) if events else 0)


def best_lag(watts, hr, max_lag=90, step=5):
    """HR lag (s) that maximises correlation with power, searched on 5 s
    subsamples for speed."""
    w = watts[::step]
    h = hr[::step]
    n = min(len(w), len(h))
    if n < 60:
        return 30
    w, h = w[:n], h[:n]
    best, best_r = 30, -2
    for lag in range(0, max_lag + 1, step):
        k = lag // step
        a = w[: n - k] if k else w
        b = h[k:] if k else h
        pairs = [(x, y) for x, y in zip(a, b) if x is not None and y]
        if len(pairs) < 50:
            continue
        xs, ys = zip(*pairs)
        mx, my = _mean(xs), _mean(ys)
        sxy = sum((x - mx) * (y - my) for x, y in pairs)
        sxx = sum((x - mx) ** 2 for x in xs)
        syy = sum((y - my) ** 2 for y in ys)
        if sxx <= 0 or syy <= 0:
            continue
        r = sxy / (sxx * syy) ** 0.5
        if r > best_r:
            best_r, best = r, lag
    return best


def ef_drift(watts, hr, ftp, window=300):
    """Lag-corrected windowed EF slope. Returns (drift_pct_per_hr, lag_s,
    n_windows, ef_first, ef_last) or None when under-determined."""
    if not watts or not hr:
        return None
    lag = best_lag(watts, hr)
    h = hr[lag:] + [None] * lag
    wins = []
    for start in range(0, len(watts) - window + 1, window):
        pw = [w for w in watts[start:start + window] if w is not None]
        ph = [x for x in h[start:start + window] if x]
        if len(pw) < window * 0.8 or len(ph) < window * 0.8:
            continue
        mw, mh = _mean(pw), _mean(ph)
        if mw < 0.45 * ftp or mh <= 60:
            continue  # recovery/coasting window - not an EF sample
        wins.append((start + window / 2.0, mw / mh))
    if len(wins) < 4:
        return None
    xs = [w[0] / 3600.0 for w in wins]
    ys = [w[1] for w in wins]
    mx, my = _mean(xs), _mean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    drift = -slope / ys[0] * 100.0  # positive = EF falling = HR drifting up
    return (round(drift, 1), lag, len(wins),
            round(ys[0], 3), round(ys[-1], 3))


def halves_decoupling(watts, hr):
    n = min(len(watts), len(hr)) // 2
    if n < 600:
        return None

    def ef(seg_w, seg_h):
        pw = [w for w in seg_w if w is not None]
        ph = [x for x in seg_h if x]
        if not pw or not ph:
            return None
        mh = _mean(ph)
        return _mean(pw) / mh if mh else None

    e1 = ef(watts[:n], hr[:n])
    e2 = ef(watts[n:], hr[n:])
    if not e1 or not e2:
        return None
    return round((e1 - e2) / e1 * 100.0, 1)  # positive = drift


def hrr60(watts, hr, ftp):
    """Max 60 s HR recovery after a hard effort (30 s avg power >= FTP or
    HR >= 92% of an inferred effort peak) followed by soft pedalling."""
    if not watts or not hr:
        return None
    r30 = rolling30(watts)
    best = None
    i, n = 0, len(r30)
    while i < n - 150:
        if r30[i] >= ftp and (hr[i] or 0) > 100:
            j = i
            while j < n and r30[j] >= 0.85 * ftp:
                j += 1
            soft = all((watts[k] or 0) < 0.60 * ftp
                       for k in range(j, min(j + 90, n)))
            if soft and j + 60 < n and hr[j] and hr[j + 60]:
                drop = hr[j] - hr[j + 60]
                if drop > 0 and (best is None or drop > best):
                    best = drop
            i = j + 90
        else:
            i += 1
    return best


def wbal_min(watts, cp, wprime):
    bal, lo = wprime, wprime
    for w in watts:
        w = w or 0
        if w > cp:
            bal -= (w - cp)
        else:
            bal += (cp - w) * (wprime - bal) / wprime
        bal = min(bal, wprime)
        lo = min(lo, bal)
    return lo


def grade_series(altitude, distance):
    if not altitude or not distance:
        return None
    g, half = [], 15
    for i in range(len(altitude)):
        a, b = max(0, i - half), min(len(altitude) - 1, i + half)
        dd = (distance[b] or 0) - (distance[a] or 0)
        da = (altitude[b] or 0) - (altitude[a] or 0)
        g.append((da / dd * 100.0) if dd > 5 else 0.0)
    return g


def compute_metrics(t, watts, hr, cadence, velocity, altitude, distance,
                    ftp, lthr, sport, wprime=20000):
    """Main entry. Returns the metrics dict (schema v1)."""
    n = len(t)
    watts = _clean(t, watts)
    hr = _clean(t, hr)
    cadence = _clean(t, cadence)
    velocity = _clean(t, velocity)
    altitude = _clean(t, altitude)
    distance = _clean(t, distance)
    mask = moving_mask(t, velocity)
    out = {"schema": SCHEMA, "sport": sport, "ftp": ftp, "lthr": lthr,
           "moving_s": sum(1 for m in mask if m)}

    pw = _vals(watts, mask) if watts else []
    ph = _vals(hr, mask) if hr else []

    if pw and ftp:
        npv = np_power(pw)
        avg = _mean(pw)
        out["power"] = {
            "avg": round(avg), "np": round(npv),
            "vi": round(npv / avg, 3) if avg else None,
            "p50": round(_pct(pw, 50)), "p75": round(_pct(pw, 75)),
            "p95": round(_pct(pw, 95)),
            "hist_pct_ftp": histogram(pw, ftp, PW_EDGES),
            "hist_edges": "5%-of-FTP buckets, <40% then 40..135 in 5s, >135",
        }
        flags105 = [(w or 0) > 1.05 * ftp for w in pw]
        flags90 = [(w or 0) > 0.90 * ftp for w in pw]
        c105 = runs_above(flags105, 10)
        c90 = runs_above(flags90, 10)
        out["surges"] = {
            "gt105_count": c105[0], "gt105_total_s": c105[1],
            "gt105_max_s": c105[2],
            "gt90_count": c90[0], "gt90_total_s": c90[1],
        }
        flags95 = [(w or 0) >= 0.95 * ftp for w in pw]
        out["longest_ge95pct_s"] = runs_above(flags95, 30)[2]
        out["time_over_ftp_s"] = sum(1 for w in pw if (w or 0) > ftp)
        out["wbal_min_kj_est"] = round(wbal_min(pw, ftp, wprime) / 1000.0, 1)

    if ph and lthr:
        out["hr"] = {"avg": round(_mean(ph)), "max": max(ph),
                     "hist_pct_lthr": histogram(ph, lthr, HR_EDGES),
                     "hist_edges": "%LTHR: <68,68-81,81-85,85-89,89-94,"
                                   "94-100,100-103,103-106,>106"}

    if pw and ph and out["moving_s"] >= 1800:
        d = ef_drift(pw, ph, ftp)
        if d:
            out["ef_drift"] = {"pct_per_hr": d[0], "hr_lag_s": d[1],
                               "windows": d[2], "ef_first": d[3],
                               "ef_last": d[4]}
        hd = halves_decoupling(pw, ph)
        if hd is not None:
            out["decoupling_halves_pct"] = hd
        # EF at reference aerobic band (75-85% LTHR, power engaged)
        ref = [(w, x) for w, x in zip(pw, ph)
               if x and 0.75 * lthr <= x <= 0.85 * lthr
               and (w or 0) > 0.40 * ftp]
        if len(ref) >= 600:
            out["ef_ref"] = {"w_per_bpm": round(
                _mean([w for w, _ in ref]) / _mean([x for _, x in ref]), 3),
                "seconds": len(ref)}
        r = hrr60(pw, ph, ftp)
        if r:
            out["hrr60_bpm"] = r

    if cadence:
        cv = [c for c, m in zip(cadence, mask) if m and c]
        if len(cv) >= 600:
            third = len(cv) // 3
            c1, c3 = _mean(cv[:third]), _mean(cv[-third:])
            out["cadence"] = {"avg": round(_mean(cv), 1),
                              "drift": round(c3 - c1, 1)}

    g = grade_series(altitude, distance)
    if g and pw and sport in ("Ride",):
        climb = [w for w, gr, m in zip(watts, g, mask)
                 if m and w is not None and gr >= 3]
        flat = [w for w, gr, m in zip(watts, g, mask)
                if m and w is not None and abs(gr) < 1]
        if len(climb) >= 120:
            out["np_climbing"] = round(np_power(climb))
            out["time_climbing_s"] = len(climb)
        if len(flat) >= 300:
            out["np_flat"] = round(np_power(flat))
    if g and sport == "Run" and velocity and watts:
        up = [(v, w) for v, w, gr, m in zip(velocity, watts, g, mask)
              if m and v and w is not None and gr >= 3]
        down = [(v, w) for v, w, gr, m in zip(velocity, watts, g, mask)
                if m and v and w is not None and gr <= -3]
        if len(up) >= 120:
            out["uphill"] = {"pace_min_km": round(1000 / _mean([v for v, _ in up]) / 60, 2),
                             "avg_w": round(_mean([w for _, w in up]))}
        if len(down) >= 120:
            out["downhill"] = {"pace_min_km": round(1000 / _mean([v for v, _ in down]) / 60, 2),
                               "avg_w": round(_mean([w for _, w in down]))}
    return out
