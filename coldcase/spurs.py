"""Spurs of a strong comb: an event that is one tooth of a comb of much
stronger lines in its own scan.

Electronics that run off a clock leak lines at whole multiples of the clock
rate, a strong carrier mixed with it shows up again at those offsets, and a
weak line sitting exactly on such a grid is a spur of the same device. The
comb test (D2) looks for combs among the queue's own cases; this one looks
in the survey's hit tables, where the strong lines are, since a strong line
is not a near-miss and never entered the queue.

An event is a tooth when, in its seed scan, two or more lines at least
STRONGER times its S/N and of the same drift sit at whole multiples (up to
MAX_K) of one spacing from it, the spacing is a round clock rate to within
CLOCK_PPM (a power of two in hertz or kilohertz, or 1, 2, 2.5 or 5 times a
power of ten), and at least one of those lines is also in an off-star scan
of the cadence. It closes the event (rule D2). Chance is measured by the
same test at frequencies shifted off the event (null_test).

usage: python -m coldcase.spurs                 every open event (dry run)
       python -m coldcase.spurs --null          chance, at shifted places
       python -m coldcase.spurs --controls      on the blind test signals
       python -m coldcase.spurs --apply
"""

import json
import sys

import numpy as np

from . import db, ledger
from .checks import CATALOGUE_DRIFT_SIGN
from .remote import ROOT

STRONGER = 10.0
MAX_K = 8
TOL_HZ = 30.0
CLOCK_PPM = 200.0
SPAN_MHZ = 17.0
TEXT = "a tooth of a comb of stronger lines"


def round_rate(hz):
    """The round clock rate hz matches to within CLOCK_PPM, or None."""
    tol = CLOCK_PPM * 1e-6
    for n in range(10, 25):
        if abs(hz / 2**n - 1) <= tol:
            return f"2^{n} Hz"
    for n in range(0, 15):
        if abs(hz / (1000 * 2**n) - 1) <= tol:
            return f"{2**n * 1000 / 1e6:g} MHz (2^{n} kHz)"
    for e in range(3, 8):
        for m in (1, 2, 2.5, 5):
            if abs(hz / (m * 10**e) - 1) <= tol:
                return f"{m * 10**e / 1e6:g} MHz"
    return None


def _cadence(con, b):
    return db.cadence_for(con, b["case"]["target"], b["case"]["mjd"])


def test(con, b, shift_mhz=0.0):
    """The tooth test for one bundle, at the event's frequency plus shift_mhz
    (a shift tests a place with nothing there). Returns a dict or None."""
    cad = _cadence(con, b)
    if cad is None:
        return None
    fid = {s["pos"]: s["file_id"] for s in cad}
    mjd = {s["pos"]: s["mjd"] for s in cad}
    sp = b["seed"]["seed_pos"]
    f_case = b["case"]["freq_mhz"] + shift_mhz
    # the event as measured (an injected test signal is not in the tables);
    # its drift turned to the tables' convention for comparing with them
    seed = b["seed"]
    if not seed.get("recovered"):
        return None
    f0 = seed["freq_mhz"] + shift_mhz
    d0 = CATALOGUE_DRIFT_SIGN * seed["drift"]
    s0 = seed["snr"]
    rows = con.execute(
        "select freq, drift, snr from hits where file_id=? and freq between ? and ? and snr >= ?",
        [fid[sp], f0 - SPAN_MHZ, f0 + SPAN_MHZ, STRONGER * s0],
    ).fetchall()
    dtol = 0.05 + 0.25 * abs(d0)
    strong = [
        (f, d, s)
        for f, d, s in rows
        if abs(d - d0) <= dtol and abs(f - f0) * 1e6 > TOL_HZ
    ]
    best = None
    for f, _, _ in strong:
        off = (f - f0) * 1e6
        for k in range(1, MAX_K + 1):
            spacing = abs(off) / k
            name = round_rate(spacing)
            if not name:
                continue
            teeth = [
                x
                for x in strong
                if (
                    lambda o: (
                        round(o / spacing) != 0
                        and abs(o - round(o / spacing) * spacing)
                        <= TOL_HZ + 1e-6 * abs(o)
                    )
                )((x[0] - f0) * 1e6)
                and abs(round((x[0] - f0) * 1e6 / spacing)) <= MAX_K
            ]
            if len(teeth) >= 2 and (best is None or len(teeth) > len(best[2])):
                best = (spacing, name, teeth)
    if best is None:
        return {"tooth": False}
    spacing, name, teeth = best
    # is a tooth also in an off-star scan of the cadence?
    off_seen = []
    for pos, f_id in fid.items():
        if pos % 2 == 0:
            continue
        dt = (mjd[pos] - mjd[sp]) * 86400.0
        for f, d, s in teeth:
            fp = f + CATALOGUE_DRIFT_SIGN * d * dt * 1e-6  # the tables' drift points the other way
            tol = (20.0 + 0.02 * abs(dt) + 0.1 * abs(d) * abs(dt)) * 1e-6
            hit = con.execute(
                "select snr from hits where file_id=? and freq between ? and ? order by snr desc limit 1",
                [f_id, fp - tol, fp + tol],
            ).fetchone()
            if hit:
                off_seen.append((pos, round(f, 6), round(hit[0], 1)))
                break
    return {
        "tooth": bool(off_seen),
        "spacing_hz": round(spacing, 1),
        "clock": name,
        "teeth": [
            (round(f, 6), round((f - f0) * 1e6), round(s)) for f, _, s in sorted(teeth)
        ],
        "event_snr": round(s0, 1),
        "off_star": off_seen,
        "case_freq_mhz": f_case,
    }


def text_for(r):
    t = r["teeth"]
    return (
        f"D2: {TEXT}: {len(t)} lines at least {STRONGER:g} times stronger, of the same "
        f"drift, sit at whole multiples of {r['spacing_hz'] / 1e6:.6f} MHz ({r['clock']}) "
        f"from it in its scan (strongest S/N {max(x[2] for x in t)}), and are there off the "
        f"star too"
    )


def _open(con_q):
    return [
        r[0]
        for r in con_q.execute(
            "select case_id from queue where status in ('open','open_unread','open_member') order by case_id"
        )
    ]


def _bundle(cid):
    return json.loads((ROOT / "cases" / "evidence" / cid / "evidence.json").read_text())


def survey(say=print):
    from . import runner

    con, q = db.connect(), runner.queue_db()
    out = {}
    for cid in _open(q):
        r = test(con, _bundle(cid))
        if r and r["tooth"]:
            out[cid] = r
            say(f"{cid}: {text_for(r)}")
        elif r and r.get("teeth"):
            say(
                f"{cid}: on a comb at {r['clock']} but no tooth off the star (not closed)"
            )
    say(f"{len(out)} open lines are teeth of a stronger comb")
    return out


def null_test(say=print):
    """The same test at shifted places: 17 non-round shifts per open event
    and per closed queue case with a recovered seed."""
    from . import runner

    con, q = db.connect(), runner.queue_db()
    shifts = [
        s * 1e-3
        for s in (
            -913.7,
            -507.3,
            -251.1,
            -113.9,
            -37.3,
            41.7,
            119.3,
            263.9,
            541.1,
            977.3,
        )
    ]
    n = hits = 0
    for (cid,) in q.execute("select case_id from queue where status!='no_data'"):
        p = ROOT / "cases" / "evidence" / cid / "evidence.json"
        if not p.exists():
            continue
        b = json.loads(p.read_text())
        if not b["seed"].get("recovered"):
            continue
        for sh in shifts:
            r = test(con, b, sh)
            if r is None:
                continue
            n += 1
            hits += bool(r["tooth"])
    say(
        f"null test: {hits} of {n} shifted places pass as teeth ({hits / max(n, 1):.2%})"
    )
    return hits, n


def controls_test(say=print):
    led = ledger.connect()
    con = db.connect()
    n = fired = 0
    for cid, kind in led.execute(
        "select case_id, kind from controls where recovered=1 and masked=0"
    ):
        p = ROOT / "cases" / "evidence" / cid / "evidence.json"
        if not p.exists() or kind == "leak":
            continue
        r = test(con, json.loads(p.read_text()))
        n += 1
        fired += bool(r and r["tooth"])
    say(f"sky-only test signals: the rule fires on {fired} of {n}")
    return fired, n


def apply(say=print):
    from . import runner

    res = survey(say=lambda *a: None)
    q = runner.queue_db()
    closed = []
    for cid, r in res.items():
        row = q.execute("select status from queue where case_id=?", (cid,)).fetchone()
        if not row or not row[0].startswith("open"):
            continue
        why = text_for(r)
        members = (
            []
            if row[0] == "open_member"
            else [
                m
                for (m,) in q.execute(
                    "select case_id from queue where status='open_member' and note=?",
                    (cid,),
                )
            ]
        )
        for c, w in [(cid, why)] + [
            (m, f"{why}; this line belongs to the event of {cid}") for m in members
        ]:
            ledger.add_verdict(
                ledger.connect(),
                {
                    "case_id": c,
                    "role": "triage",
                    "agent": "code",
                    "verdict": "rfi_identified",
                    "confidence": 1.0,
                    "rationale": w,
                    "evidence_refs": [w],
                },
            )
            runner.set_status(q, c, "closed", "rfi_identified")
            runner.log(f"{c} closed: rfi_identified ({TEXT})")
            closed.append(c)
    say(f"closed {len(closed)} lines as teeth of a stronger comb")
    return closed


def main():
    a = sys.argv[1:]
    if "--null" in a:
        null_test()
    elif "--controls" in a:
        controls_test()
    elif "--apply" in a:
        apply()
    else:
        survey()


if __name__ == "__main__":
    main()
