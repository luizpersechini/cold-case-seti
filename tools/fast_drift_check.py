"""Does the search for a catalogued hit find fast-drifting tones, and how
often does it find one where there is none?

The running controls hold no signal that drifts faster than 1 Hz/s, so they
say nothing about the fast half of the queue. This check uses the wide
cutouts already in the cache: those of the queue cases whose catalogue drift
is --min-drift or more. At four places of each cadence where nothing is
catalogued it runs the seed search (investigate.pick_seed) on the data as
they are, and again with a tone injected that drifts at the case's rate.

Each run is made twice: with the window the search used until 2026-11-21
(100 Hz plus the drift over 300 s around the catalogue frequency) and with
today's (investigate._seed_half_hz). It reports how often an empty place
gives a seed, and how many injected tones are found, by strength. Nothing is
downloaded or written.

usage: python tools/fast_drift_check.py [--min-drift 1.0] [--every N]
"""

import copy
import json
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

from coldcase import backfill, investigate

LEVELS = (8, 12, 20, 40)  # S/N a tone should reach along its track
NARROW = investigate._seed_half_hz


def WIDE(d0, tsamp):
    return 100 + 300 * abs(d0)


def seed(window, probe, scans, cuts, half):
    """pick_seed with the given window for the seed's place."""
    investigate._seed_half_hz = window
    try:
        return investigate.pick_seed(probe, scans, cuts, half)[1]
    finally:
        investigate._seed_half_hz = NARROW


def places(case, cuts, half_khz):
    """Four frequencies of the cadence clear of the catalogue position, where
    a tone of the case's drift stays inside the cutout for a whole scan."""
    d = abs(case["drift"])
    c = cuts[0]
    span = c["data"].shape[0] * c["tsamp"]
    fh = WIDE(d, c["tsamp"])
    pad = (d + investigate._drift_half(d)) * span + 3 * abs(c["foff"] * 1e6)
    usable = half_khz * 1e3 - pad - fh - d * span
    if usable * 0.45 < 2 * fh + d * span:
        return []
    return [case["freq_mhz"] + m * usable * 1e-6 for m in (-0.9, -0.45, 0.45, 0.9)]


def one(job):
    case, path = job
    try:
        b = json.loads(Path(path).read_text())
        got = investigate._cached_cuts(case, b)
        if got is None:
            return None
        scans, cuts, half = got
        if any("error" in c for c in cuts):
            return None
        d0 = case["drift"]
        on = [i for i, s in enumerate(scans) if s["role"] == "ON"]
        ref = min(on, key=lambda i: abs(cuts[i]["tstart"] - case["mjd"]))
        t0 = min(c["tstart"] for c in cuts)
        tsamp = cuts[ref]["tsamp"]
        smear = abs(d0) * tsamp / abs(cuts[ref]["foff"] * 1e6) + 1
        data_drift = -d0  # the hit tables' sign is the opposite of the data's
        out = []
        for n, f in enumerate(places(case, cuts, half)):
            probe = {
                "id": f"{case['id']}_p{n}",
                "target": case["target"],
                "freq_mhz": f,
                "drift": d0,
                "mjd": case["mjd"],
                "freq_exact": True,
            }
            row = {"drift": abs(d0), "empty": {}, "tones": {}}
            for name, window in (("wide", WIDE), ("narrow", NARROW)):
                row["empty"][name] = bool(
                    seed(window, probe, scans, cuts, half)["recovered"]
                )
            f_start = f - data_drift * (cuts[ref]["tstart"] - t0) * 86400.0 * 1e-6
            for level in LEVELS:
                mine = copy.deepcopy(cuts)
                investigate.apply_injection(
                    {
                        "inject": {
                            "f0_mhz": f_start,
                            "drift": data_drift,
                            "snr": level * smear,
                            "present_on": [1, 1, 1],
                            "scint": [1, 1, 1],
                        }
                    },
                    scans,
                    mine,
                )
                row["tones"][level] = {}
                for name, window in (("wide", WIDE), ("narrow", NARROW)):
                    x = seed(window, probe, scans, mine, half)
                    # the tone itself, not something else in a wide window
                    row["tones"][level][name] = bool(
                        x["recovered"]
                        and abs(x["freq_mhz"] - f) * 1e6 <= NARROW(d0, tsamp)
                        and x["drift"] * data_drift > 0
                    )
            out.append(row)
        return out
    except Exception as e:
        return f"{case['id']}: {type(e).__name__}: {e}"[:200]


def main():
    args = sys.argv[1:]
    min_drift = (
        float(args[args.index("--min-drift") + 1]) if "--min-drift" in args else 1.0
    )
    every = int(args[args.index("--every") + 1]) if "--every" in args else 1
    jobs = [
        (case, str(path))
        for case, path in backfill.cases()
        if path.exists()
        and "inject" not in case
        and case.get("freq_exact")
        and abs(case.get("drift", 0.0)) >= min_drift
    ][::every]
    rows, skipped = [], 0
    with ProcessPoolExecutor(max_workers=8) as ex:
        for res in ex.map(one, jobs, chunksize=2):
            if isinstance(res, str):
                print(res)
            elif not res:
                skipped += 1
            else:
                rows += res
    print(
        f"{len(jobs)} cadences with catalogue drift of {min_drift} Hz/s or more; "
        f"{skipped} without usable places; {len(rows)} places tried"
    )
    bins = ((1, 2), (2, 3), (3, 4), (4, 99))
    by = defaultdict(list)
    for r in rows:
        by[next(b for b in bins if b[0] <= r["drift"] < b[1])].append(r)
    by["all"] = rows
    print("\nempty places that give a seed")
    print("  drift (Hz/s)   places   wide window      narrow window")
    for key in (*bins, "all"):
        rs = by.get(key, [])
        if not rs:
            continue
        name = (
            "all"
            if key == "all"
            else f"{key[0]} to {key[1]}"
            if key[1] < 99
            else "4 and up"
        )
        a = sum(r["empty"]["wide"] for r in rs)
        z = sum(r["empty"]["narrow"] for r in rs)
        print(
            f"  {name:<12} {len(rs):8d}   {a:5d} ({a / len(rs):5.1%})   "
            f"{z:5d} ({z / len(rs):5.1%})"
        )
    print("\ninjected tones found as the seed, by the S/N they should reach")
    print("  S/N aimed at   wide window      narrow window")
    for level in LEVELS:
        a = sum(r["tones"][level]["wide"] for r in rows)
        z = sum(r["tones"][level]["narrow"] for r in rows)
        print(
            f"  {level:<13d}  {a:5d} ({a / len(rows):5.1%})   {z:5d} ({z / len(rows):5.1%})"
        )


if __name__ == "__main__":
    main()
