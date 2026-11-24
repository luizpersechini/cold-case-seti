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


def main():
    a = sys.argv[1:]
    if not a or a[0].startswith("-"):
        print(__doc__)
        return
    days = float(a[a.index("--days") + 1]) if "--days" in a else DAYS
    cap = float(a[a.index("--max-gb") + 1]) if "--max-gb" in a else MAX_GB
    run(a[0], days, cap)


if __name__ == "__main__":
    main()
