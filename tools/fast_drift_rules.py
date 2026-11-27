"""What do the rules make of test signals that drift fast?

The running controls drift by 0.5 Hz/s at most, and three quarters of the
queue drifts faster. This check uses the cutouts already in the cache of the
queue cases whose catalogue drift is --min-drift or more. Beside each real
case, on tracks parallel to its own and 1.5 and 2.5 kHz away, it injects
four test signals that drift at the case's rate: two present on the star
only (one in all three ON scans, one in one or two) and two that also leak
weakly into the OFF scans. Each goes through the seed search, the six-scan
measurement, the off-scan stack, the followed line and triage.decide, as a
queue case does on its first pass (rules D1, D2 and D4; no hit tables).

The injected strength is the S/N the tone should reach along its track: the
smearing of a fast tone over the channels it crosses within a time sample
is made up for, as in tools/fast_drift_check.py. The places are not random:
they sit beside real fast cases, most of which are bursts, so the burst rule
is tested where it matters.

Nothing is downloaded and the ledger is not touched. The outcome of every
test signal goes to cases/fast_drift_rules.json.

usage: python tools/fast_drift_rules.py [--min-drift 0.5] [--max-drift X] [--every N] [--out PATH]

With --max-drift 0.5 --min-drift 0 the same test runs beside the slow cases,
where the running test signals were placed at random frequencies instead.
"""

import copy
import hashlib
import json
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

from coldcase import backfill, controls, investigate, triage

OFFSETS_HZ = (-2500.0, -1500.0, 1500.0, 2500.0)
KINDS = ("sky_persistent", "leak", "sky_intermittent", "leak")
OUT = Path(__file__).resolve().parent.parent / "cases" / "fast_drift_rules.json"


def bundle_of(probe, scans, cuts, half):
    seed, info = investigate.pick_seed(probe, scans, cuts, half)
    b = {
        "case": probe,
        "site": "GBT",
        "seed": info,
        "scans": investigate.measure(scans, cuts, seed, info, half),
    }
    if info["recovered"]:
        b["off_scans_stacked"] = investigate.off_stack(scans, cuts, seed, info)
        b["followed_line"] = investigate.follow_line(scans, cuts, seed, info)
    return b


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
        data_drift = -d0  # the hit tables' sign is the opposite of the data's
        t0 = min(c["tstart"] for c in cuts)
        on = [i for i, s in enumerate(scans) if s["role"] == "ON"]
        tsamp = cuts[on[0]]["tsamp"]
        smear = abs(d0) * tsamp / abs(cuts[on[0]]["foff"] * 1e6) + 1
        f_real_t0 = case["freq_mhz"] - data_drift * (case["mjd"] - t0) * 86400.0 * 1e-6
        rng = np.random.default_rng(
            int(hashlib.sha1(case["id"].encode()).hexdigest()[:8], 16)
        )
        out = []
        for n, (off_hz, kind) in enumerate(zip(OFFSETS_HZ, KINDS)):
            f0 = f_real_t0 + off_hz * 1e-6
            if kind == "leak":
                snr = float(np.exp(rng.uniform(np.log(25), np.log(80))))
                leak = float(rng.uniform(0.12, 0.25))
            else:
                snr, leak = float(np.exp(rng.uniform(np.log(12), np.log(60)))), 0.0
            present = (
                controls.PATTERNS[rng.integers(len(controls.PATTERNS))]
                if kind == "sky_intermittent"
                else [1, 1, 1]
            )
            first = on[present.index(1)]
            dt = (cuts[first]["tstart"] - t0) * 86400.0
            probe = {
                "id": f"{case['id']}_x{n}",
                "target": case["target"],
                "freq_mhz": f0 + data_drift * dt * 1e-6,
                "drift": d0,
                "mjd": cuts[first]["tstart"],
                "freq_exact": True,
            }
            mine = copy.deepcopy(cuts)
            investigate.apply_injection(
                {
                    "inject": {
                        "f0_mhz": f0,
                        "drift": data_drift,
                        "snr": snr * smear,
                        "present_on": present,
                        "off_fraction": leak,
                        "scint": [float(x) for x in rng.uniform(0.7, 1.3, 3)],
                        "flicker": float(rng.uniform(0.15, 0.4)),
                        "seed": int(rng.integers(1 << 30)),
                    }
                },
                scans,
                mine,
            )
            bb = bundle_of(probe, scans, mine, half)
            dec = triage.decide(bb)
            seed = bb["seed"]
            # the seed may be in any ON scan: compare with where the tone is there
            si = next(
                (i for i, x in enumerate(scans) if x["pos"] == seed.get("seed_pos")),
                first,
            )
            f_there = f0 + data_drift * (cuts[si]["tstart"] - t0) * 86400.0 * 1e-6
            own = bool(
                seed.get("recovered")
                and abs(seed["freq_mhz"] - f_there) * 1e6
                <= investigate._seed_half_hz(d0, tsamp)
                and seed["drift"] * data_drift > 0
            )
            out.append(
                {
                    "case": case["id"],
                    "kind": kind,
                    "drift": abs(d0),
                    "snr_along_track": round(snr, 1),
                    "off_fraction": round(leak, 3),
                    "recovered": bool(seed.get("recovered")),
                    "is_the_injection": own,
                    "measured_snr": seed.get("snr"),
                    "masked": bool(controls.is_masked(bb, data_drift, snr)),
                    "status": dec["status"],
                    "verdict": dec["verdict"],
                    "why": [x[:140] for x in dec.get("direct_evidence", [])][:3],
                }
            )
        return out
    except Exception as e:
        return f"{case['id']}: {type(e).__name__}: {e}"[:300]


def report(rows, say=print):
    bins = ((0, 0.1), (0.1, 0.25), (0.25, 0.5), (0.5, 1), (1, 2), (2, 3), (3, 4))
    by = defaultdict(list)
    for r in rows:
        for b in bins:
            if b[0] <= r["drift"] < b[1]:
                by[b].append(r)
        by["all"].append(r)
    say(
        "\n drift (Hz/s)   sky-only: found, left open, closed by a rule, masked   |   leaks: found, closed"
    )
    for key in (*bins, "all"):
        rs = by.get(key, [])
        sky = [r for r in rs if r["kind"] != "leak"]
        leak = [r for r in rs if r["kind"] == "leak"]
        if not sky:
            continue
        found = [r for r in sky if r["is_the_injection"] and not r["masked"]]
        closed = [r for r in found if r["status"] == "closed"]
        lf = [r for r in leak if r["is_the_injection"] and not r["masked"]]
        name = "all" if key == "all" else f"{key[0]} to {key[1]}"
        say(
            f"  {name:<10} {len(sky):5d} sky-only: {len(found):4d} found, "
            f"{sum(r['status'] == 'open' for r in found):4d} open, {len(closed):3d} closed, "
            f"{sum(r['masked'] for r in sky):3d} masked   |   {len(leak):5d} leaks: "
            f"{len(lf):4d} found, {sum(r['status'] == 'closed' for r in lf):4d} closed"
        )
    found = [
        r
        for r in rows
        if r["kind"] != "leak" and r["is_the_injection"] and not r["masked"]
    ]
    why = defaultdict(int)
    for r in found:
        if r["status"] == "closed":
            why[(r["verdict"], (r["why"] or [""])[0][:60])] += 1
    say("\n what closed the sky-only signals that were found:")
    for k, v in sorted(why.items(), key=lambda kv: -kv[1])[:12]:
        say(f"  {v:4d}  {k[0]}: {k[1]}")


def main():
    args = sys.argv[1:]
    out = Path(args[args.index("--out") + 1]) if "--out" in args else OUT
    if args[:1] == ["report"]:
        report(json.loads(out.read_text()))
        return
    min_drift = (
        float(args[args.index("--min-drift") + 1]) if "--min-drift" in args else 0.5
    )
    max_drift = (
        float(args[args.index("--max-drift") + 1]) if "--max-drift" in args else 1e9
    )
    every = int(args[args.index("--every") + 1]) if "--every" in args else 1
    jobs = [
        (case, str(path))
        for case, path in backfill.cases()
        if path.exists()
        and "inject" not in case
        and case.get("freq_exact")
        and min_drift <= abs(case.get("drift", 0.0)) < max_drift
    ][::every]
    rows, skipped = [], 0
    with ProcessPoolExecutor(max_workers=6) as ex:
        for res in ex.map(one, jobs, chunksize=2):
            if isinstance(res, str):
                print(res)
            elif not res:
                skipped += 1
            else:
                rows += res
    print(
        f"{len(jobs)} cadences with catalogue drift of {min_drift} Hz/s or more; "
        f"{skipped} without cached cutouts; {len(rows)} test signals"
    )
    out.write_text(json.dumps(rows, indent=1))
    report(rows)


if __name__ == "__main__":
    main()
