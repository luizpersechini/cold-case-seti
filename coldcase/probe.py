"""Probe: is an open event's line a local emitter seen toward other stars in
the days around it?

The session check (D7) looks at other stars within 30 minutes. This widens
the time: the Green Bank scans of other stars in the same receiver band
within DAYS days of the event, nearest in time first, until the download cap.
In each, the event's telescope frequency is measured (forced photometry:
strongest line starting within WINDOW_HZ, drift within the event's drift
+-dtol, against about 45 control windows). A line from the event's star
cannot follow the telescope to other stars; a line fixed to the telescope
frequency can.

Chance is measured, not assumed: the same measurement is made at NULLS
nearby frequencies with no catalogued hit, in the same scans. The share of
those that stand OFF_MARGIN above their controls is the chance per scan of an
unrelated line; the chance of at least the event's number of matches follows
from it.

The hit tables also give a free first look: catalogued hits within WINDOW_HZ
of the event's frequency toward other stars in the window.

The result is evidence only. A match is a closure proposal for separate
approval; no match proves nothing, since interference comes and goes.
Writes cases/evidence/<id>/probe_week.json. Each scan costs about 60 MB.

usage: python -m coldcase.probe CASE_ID [--days 7] [--max-gb 3]
       python -m coldcase.probe sweep     hit tables only, every open event:
                                          other-star hits within WINDOW_HZ in
                                          +-1, 7, 30 days against 20 nearby
                                          frequencies; cases/probe_sweep.json
       python -m coldcase.probe close CASE_ID ...   rule D9 on approved cases
"""

import json
import sys
from math import comb

import numpy as np

from . import db, remote, repeat, triage
from . import dedoppler as dd

DAYS, MAX_GB = 7.0, 3.0
WINDOW_HZ = 2000.0  # an oscillator can wander this far between nights
NULLS, NULL_STEP_HZ = 20, 25000.0
CTRL = 25  # control windows each side, the three nearest left out
BATCH = 6
NAME = "probe_week.json"


def scans(con, cad, target, t_e, days):
    """Scans of other stars in the event's band within `days` days, one per
    (star, start time), nearest first. The event's own six are left out."""
    own = {c["name"] for c in cad}
    rows = con.execute(
        "select name, source, mjd, file_id from scans where band = ? "
        "and abs(mjd - ?) <= ? and upper(source) <> upper(?) order by abs(mjd - ?), name",
        [cad[0]["band"], t_e, days, target, t_e],
    ).fetchall()
    out, seen = [], set()
    for name, source, mjd, fid in rows:
        key = (source.upper(), round(mjd, 5))
        if name in own or key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "source": source, "mjd": mjd, "file_id": fid})
    return out


def catalogue(con, band, target, t_e, f_e, days):
    """Catalogued hits within WINDOW_HZ of f_e toward other stars, and how
    many scans of other stars there are, in +-days."""
    w = WINDOW_HZ / 1e6
    n, k, near = con.execute(
        "select count(distinct s.file_id), count(distinct h.file_id), "
        "list(distinct s.source) filter (where h.file_id is not null) "
        "from scans s left join hits h on h.file_id = s.file_id "
        "and h.freq between ? and ? "
        "where s.band = ? and abs(s.mjd - ?) <= ? and upper(s.source) <> upper(?)",
        [f_e - w, f_e + w, band, t_e, days, target],
    ).fetchone()
    return {"days": days, "scans": n, "scans_with_a_hit": k, "stars": near or []}


def null_freqs(con, f_e, file_ids):
    """NULLS frequencies NULL_STEP_HZ apart around f_e with no catalogued hit
    within 2*WINDOW_HZ in these scans."""
    out = []
    for m in range(1, NULLS):
        for f in (f_e - m * NULL_STEP_HZ / 1e6, f_e + m * NULL_STEP_HZ / 1e6):
            w = 2 * WINDOW_HZ / 1e6
            hit = con.execute(
                "select count(*) from hits where freq between ? and ? and file_id in "
                f"({','.join('?' * len(file_ids))})",
                [f - w, f + w, *file_ids],
            ).fetchone()[0]
            if not hit and len(out) < NULLS:
                out.append(round(f, 6))
        if len(out) == NULLS:
            break
    return out


def half_khz(drift_reach):
    """Cutout half-width that holds the farthest null, its controls and the
    drift across a scan."""
    ctol = int(np.ceil(WINDOW_HZ / repeat.CHAN_HZ))
    ch = (
        (CTRL + 0.5) * (2 * ctol + 3) + ctol + drift_reach * 320.0 / repeat.CHAN_HZ + 30
    )
    return float(np.ceil(NULLS / 2 * NULL_STEP_HZ / 1e3 + ch * repeat.CHAN_HZ / 1e3))


def measure(cu, freqs, drift, dtol):
    """At each frequency: the strongest line starting within WINDOW_HZ with
    drift within drift+-dtol, and its margin over the largest of its control
    windows. One dedispersion pass serves every window."""
    fo = cu["foff"] * 1e6
    ctol = WINDOW_HZ / abs(fo)
    width = 2 * int(np.ceil(ctol)) + 3
    ks, groups = [], []
    for f in freqs:
        k = (f - cu["fch1"]) / cu["foff"]
        start = len(ks)
        ks += [k] + [k + m * width for m in range(-CTRL, CTRL + 1) if abs(m) >= 3]
        groups.append((start, len(ks)))
    res = dd.track_multi(
        cu["data"], fo, cu["tsamp"], ks, drift, chan_tol=ctol, drift_tol=dtol
    )
    out = []
    for f, (a, b) in zip(freqs, groups):
        tr = res[a]
        ctrl = np.array([r["snr"] for r in res[a + 1 : b] if r["in_cutout"]])
        if tr["snr"] is None or len(ctrl) < 10:
            out.append({"freq_mhz": f, "margin": None})
            continue
        found = cu["fch1"] + tr["chan"] * cu["foff"]
        out.append(
            {
                "freq_mhz": f,
                "snr": round(tr["snr"], 1),
                "offset_hz": round((found - f) * 1e6, 1),
                "drift": round(tr["drift"], 4),
                "control_max": round(float(ctrl.max()), 1),
                "margin": round(float(tr["snr"] - ctrl.max()), 1),
            }
        )
    return out


def tail(p, n, k):
    """Chance of k or more of n scans holding an unrelated line."""
    return float(sum(comb(n, j) * p**j * (1 - p) ** (n - j) for j in range(k, n + 1)))


def summarise(rows):
    """Matches at the event's frequency against the null frequencies."""
    ev = [r for r in rows if r.get("event", {}).get("margin") is not None]
    nul = [x for r in rows for x in r.get("nulls", []) if x.get("margin") is not None]
    match = [r for r in ev if r["event"]["margin"] >= triage.OFF_MARGIN]
    n_hit = sum(x["margin"] >= triage.OFF_MARGIN for x in nul)
    p = (n_hit + 1) / (len(nul) + 1) if nul else None
    stars = sorted({r["source"] for r in match})
    out = {
        "scans_measured": len(ev),
        "scans_matched": len(match),
        "stars_matched": stars,
        "null_trials": len(nul),
        "null_matches": n_hit,
        "chance_per_scan": None if p is None else round(p, 4),
        "chance_of_this_many": None
        if p is None or not ev
        else round(tail(p, len(ev), len(match)), 6),
    }
    if not ev:
        out["text"] = "no scan of another star measured"
    elif match:
        out["text"] = (
            f"the line at the event's telescope frequency toward {len(stars)} other "
            f"star(s) in {len(match)} of {len(ev)} scans; chance of unrelated lines "
            f"{out['chance_of_this_many']:.2g} (from {len(nul)} null measurements)"
        )
    else:
        out["text"] = (
            f"absent toward other stars in {len(ev)} scans within the window "
            f"(null frequencies matched {n_hit} of {len(nul)})"
        )
    return out


def run(case_id, days=DAYS, max_gb=MAX_GB, save=True, say=print):
    folder = remote.ROOT / "cases" / "evidence" / case_id
    b = json.loads((folder / "evidence.json").read_text())
    seed, case = b["seed"], b["case"]
    if not seed.get("recovered"):
        raise SystemExit(f"{case_id}: the line was not recovered")
    ev = next(s for s in b["scans"] if s["pos"] == seed["seed_pos"])
    t_e, f_e, d_e = ev["mjd"], seed["freq_mhz"], seed["drift"]
    dtol = 0.05 + 0.25 * abs(d_e)
    con = db.connect()
    cad = db.cadence_for(con, case["target"], t_e)
    band = cad[0]["band"]
    out = {
        "case_id": case_id,
        "event": {
            "target": case["target"],
            "band": band,
            "mjd": round(t_e, 5),
            "freq_mhz": f_e,
            "drift": d_e,
            "drift_tol": round(dtol, 3),
        },
        "window_hz": WINDOW_HZ,
        "catalogue": [
            catalogue(con, band, case["target"], t_e, f_e, d) for d in (days, 30.0)
        ],
    }
    for c in out["catalogue"]:
        say(
            f"hit tables, +-{c['days']:g} d: {c['scans_with_a_hit']} of {c['scans']} scans "
            f"of other stars hold a hit within {WINDOW_HZ:g} Hz {c['stars'][:8]}"
        )
    todo = scans(con, cad, case["target"], t_e, days)
    out["nulls"] = nulls = null_freqs(con, f_e, [s["file_id"] for s in todo])
    half = half_khz(abs(d_e) + dtol)
    say(
        f"{len(todo)} scans of other stars within {days:g} d; {len(nulls)} null "
        f"frequencies; cutouts +-{half:g} kHz"
    )
    rows, mb = [], 0.0
    with repeat._turn():
        for i in range(0, len(todo), BATCH):
            if mb / 1e3 >= max_gb:
                say(f"stopped at the {max_gb:g} GB cap")
                break
            part = todo[i : i + BATCH]
            for s in part:
                s["url"] = remote.url_for_scan(s["name"], s["source"])
            cuts = repeat._fetch_all([(s["url"], f_e, half) for s in part])
            for s, cu in zip(part, cuts):
                r = {
                    "source": s["source"],
                    "mjd": round(s["mjd"], 5),
                    "hours_from_event": round((s["mjd"] - t_e) * 24, 2),
                }
                if "error" in cu:
                    r["error"] = cu["error"][:160]
                else:
                    mb += 0 if cu.get("cached") else cu.get("net_bytes", 0) / 1e6
                    got = measure(cu, [f_e] + nulls, d_e, dtol)
                    r["event"], r["nulls"] = got[0], got[1:]
                e = r.get("event") or {}
                say(
                    f"  {s['source']:>12} {r['hours_from_event']:+8.2f} h  "
                    + (
                        r.get("error")
                        or f"S/N {e.get('snr')} margin {e.get('margin')} "
                        f"at {e.get('offset_hz')} Hz drift {e.get('drift')}"
                    )
                )
                rows.append(r)
            remote.clear_chunks()
            out["net_mb"] = round(mb, 1)
            out["scans"], out["summary"] = rows, summarise(rows)
            out["scans_not_reached"] = len(todo) - len(rows)
            if save:
                (folder / NAME).write_text(json.dumps(out, indent=1))
    say(f"result: {out['summary']['text']}  [{mb / 1e3:.2f} GB]")
    return out


SWEEP_DAYS = (1.0, 7.0, 30.0)
SWEEP_NULLS = 10  # each side, NULL_STEP_HZ apart
SWEEP_DTOL = 0.08


def open_events():
    """Open queue events and open follow-up events: (id, evidence bundle)."""
    from . import ledger

    led = ledger.connect()
    ids = [
        r[0]
        for r in led.execute(
            "select case_id from queue where status in ('open','open_unread') order by case_id"
        )
    ]
    try:
        ids += [
            r[0]
            for r in led.execute(
                "select case_id from followup where status='open' order by case_id"
            )
        ]
    except Exception:
        pass
    for cid in ids:
        p = remote.ROOT / "cases" / "evidence" / cid / "evidence.json"
        if p.exists():
            yield cid, json.loads(p.read_text())


def event_of(b):
    """Target, time, telescope frequency and drift (data sign) of an event."""
    from .checks import CATALOGUE_DRIFT_SIGN

    case, seed = b["case"], b["seed"]
    if seed.get("recovered"):
        ev = next(s for s in b["scans"] if s["pos"] == seed["seed_pos"])
        return case["target"], ev["mjd"], seed["freq_mhz"], seed["drift"], True
    return (
        case["target"],
        case["mjd"],
        case["freq_mhz"],
        CATALOGUE_DRIFT_SIGN * case["drift"],
        False,
    )


def count_near(hits, f, d, t_e, days, dtol=SWEEP_DTOL):
    """From (freq, drift data sign, mjd, source, scan key) rows: the scans
    within `days` holding a hit within WINDOW_HZ of f, and those whose hit
    also has the drift d+-dtol."""
    w = WINDOW_HZ / 1e6
    near = [h for h in hits if abs(h[0] - f) <= w and abs(h[2] - t_e) <= days]
    return near, {h[4] for h in near}, {h[4] for h in near if abs(h[1] - d) <= dtol}


def sweep_one(con, cid, b):
    from .checks import CATALOGUE_DRIFT_SIGN

    target, t_e, f_e, d_e, rec = event_of(b)
    cad = db.cadence_for(con, target, t_e)
    if cad is None:
        return {"case_id": cid, "error": "cadence not in the hit tables"}
    band, reach = cad[0]["band"], (SWEEP_NULLS * NULL_STEP_HZ + WINDOW_HZ) / 1e6
    big = max(SWEEP_DAYS)
    n_scans = {
        dd_: con.execute(
            "select count(distinct (upper(source), round(mjd, 5))) from scans "
            "where band=? and abs(mjd-?)<=? and upper(source)<>upper(?)",
            [band, t_e, dd_, target],
        ).fetchone()[0]
        for dd_ in SWEEP_DAYS
    }
    rows = con.execute(
        "select h.freq, h.drift, s.mjd, s.source from hits_u h join scans s using (file_id) "
        "where h.freq between ? and ? and s.band=? and abs(s.mjd-?)<=? "
        "and upper(s.source)<>upper(?)",
        [f_e - reach, f_e + reach, band, t_e, big, target],
    ).fetchall()
    hits = [
        (fq, CATALOGUE_DRIFT_SIGN * dr, mj, src, (src.upper(), round(mj, 5)))
        for fq, dr, mj, src in rows
    ]
    out = {
        "case_id": cid,
        "target": target,
        "band": band,
        "freq_mhz": f_e,
        "drift": d_e,
        "recovered": rec,
        "mjd": round(t_e, 5),
        "windows": {},
    }
    nulls = [
        f_e + s * m * NULL_STEP_HZ / 1e6
        for m in range(1, SWEEP_NULLS + 1)
        for s in (-1, 1)
    ]
    for days in SWEEP_DAYS:
        near, sc, same = count_near(hits, f_e, d_e, t_e, days)
        null_k = [len(count_near(hits, f, d_e, t_e, days)[1]) for f in nulls]
        first = min(near, key=lambda h: abs(h[2] - t_e), default=None)
        out["windows"][f"{days:g}"] = {
            "other_star_scans": n_scans[days],
            "scans_with_a_hit": len(sc),
            "with_the_drift": len(same),
            "stars": sorted({h[3] for h in near})[:12],
            "nearest": None
            if first is None
            else {
                "hours": round((first[2] - t_e) * 24, 2),
                "offset_hz": round((first[0] - f_e) * 1e6, 1),
                "drift": round(first[1], 4),
                "source": first[3],
            },
            "null_max": max(null_k),
            "null_median": float(np.median(null_k)),
        }
    w7 = out["windows"]["7"]
    out["probe_it"] = (
        w7["scans_with_a_hit"] >= 2 and w7["scans_with_a_hit"] > w7["null_max"]
    )
    return out


def sweep(say=print):
    """Hit-table pass over every open event: nothing downloaded, no status
    changed. Writes cases/probe_sweep.json."""
    con = db.connect()
    res = []
    for cid, b in open_events():
        try:
            r = sweep_one(con, cid, b)
        except Exception as e:
            r = {"case_id": cid, "error": f"{type(e).__name__}: {e}"[:160]}
        res.append(r)
    res.sort(
        key=lambda r: (
            not r.get("probe_it"),
            -(r.get("windows", {}).get("7", {}).get("scans_with_a_hit", -1)),
        )
    )
    for r in res:
        if "error" in r:
            say(f"{r['case_id']}: {r['error']}")
            continue
        w = r["windows"]
        say(
            f"{r['case_id']} {r['target']:<10} {r['band']} {r['freq_mhz']:.6f} "
            + "  ".join(
                f"+-{k}d {v['scans_with_a_hit']}/{v['other_star_scans']} "
                f"(drift {v['with_the_drift']}, nulls max {v['null_max']})"
                for k, v in w.items()
            )
            + ("  PROBE IT" if r["probe_it"] else "")
        )
    (remote.ROOT / "cases" / "probe_sweep.json").write_text(json.dumps(res, indent=1))
    say(f"{len(res)} events, {sum(bool(r.get('probe_it')) for r in res)} to probe")
    return res


MIN_MATCHED = 2


def closure(out):
    """Rule D9: the line at the event's telescope frequency toward other
    stars in at least MIN_MATCHED scans, with the chance of that many
    unrelated lines (from the null frequencies) below triage.CHANCE_MAX.
    Returns the evidence text, or None."""
    s = out.get("summary") or {}
    p = s.get("chance_of_this_many")
    if s.get("scans_matched", 0) < MIN_MATCHED or p is None or p >= triage.CHANCE_MAX:
        return None
    return (
        f"D9: the same line toward {len(s['stars_matched'])} other stars in "
        f"{s['scans_matched']} of {s['scans_measured']} scans within "
        f"{max(abs(r['hours_from_event']) for r in out['scans'] if 'event' in r):.0f} h "
        f"of the event; null frequencies matched {s['null_matches']} of "
        f"{s['null_trials']}; chance of unrelated lines {p:.2g}"
    )


def close(ids, say=print):
    """Apply D9 to the named open events from their probe_week.json."""
    from datetime import datetime, timezone

    from . import ledger

    led = ledger.connect()
    for cid in ids:
        p = remote.ROOT / "cases" / "evidence" / cid / NAME
        text = closure(json.loads(p.read_text())) if p.exists() else None
        if not text:
            say(f"{cid}: not closed ({'no probe' if not p.exists() else 'D9 not met'})")
            continue
        if cid.startswith("f"):
            row = led.execute(
                "select status, why from followup where case_id=?", (cid,)
            ).fetchone()
            if not row or row[0] != "open":
                say(f"{cid}: not open")
                continue
            why = json.loads(row[1]) if row[1] and row[1].startswith("[") else []
            led.execute(
                "update followup set status='closed', verdict='rfi_identified', "
                "why=?, updated=? where case_id=?",
                (
                    json.dumps(why + [text]),
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    cid,
                ),
            )
            led.commit()
        else:
            from . import runner

            con = runner.queue_db()
            row = con.execute(
                "select status from queue where case_id=?", (cid,)
            ).fetchone()
            if not row or row[0] not in ("open", "open_unread"):
                say(f"{cid}: not open")
                continue
            members = [
                r[0]
                for r in con.execute(
                    "select case_id from queue where status='open_member' and note=?",
                    (cid,),
                )
            ]
            for m in [cid] + members:
                ledger.add_verdict(
                    led,
                    {
                        "case_id": m,
                        "role": "triage",
                        "agent": "code",
                        "verdict": "rfi_identified",
                        "confidence": 1.0,
                        "rationale": text
                        + (
                            ""
                            if m == cid
                            else f"; this line belongs to the event of {cid}"
                        ),
                        "evidence_refs": [text],
                    },
                )
                runner.set_status(con, m, "closed", "rfi_identified")
                runner.log(f"{m} closed: rfi_identified (probe, D9)")
        say(f"{cid}: closed. {text}")


def main():
    a = sys.argv[1:]
    if a[:1] == ["sweep"]:
        sweep()
        return
    if a[:1] == ["close"]:
        close(a[1:])
        return
    if not a or a[0].startswith("-"):
        print(__doc__)
        return
    days = float(a[a.index("--days") + 1]) if "--days" in a else DAYS
    cap = float(a[a.index("--max-gb") + 1]) if "--max-gb" in a else MAX_GB
    run(a[0], days, cap)


if __name__ == "__main__":
    main()
