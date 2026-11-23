"""Hidden test signals that drift fast, from the cutouts already in the cache.

None of the running controls drifts faster than 1 Hz/s, and half of the open
list does. This injects tones at the drift of the fast queue cases, at places
of their cached cutouts where nothing is catalogued, and runs the whole
first pass on them as for a real case: the seed search, the measurement in
all six scans, the off-star sum, the line followed from scan to scan, and
the rules (triage.decide). Nothing is downloaded or written.

  sky-only   present in the three on-star scans only, at S/N 15, 30 and 60;
             should be found and left open
  leak       the same at S/N 30 with half of its strength in the off-star
             scans; should be closed

usage: python tools/fast_controls.py [--every N]
"""

import copy
import json
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fast_drift_check as fdc  # noqa: E402

from coldcase import backfill, investigate, triage  # noqa: E402

VARIANTS = (("sky", 15, 0.0), ("sky", 30, 0.0), ("sky", 60, 0.0), ("leak", 30, 0.5))


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
        data_drift = -d0
        on = [i for i, s in enumerate(scans) if s["role"] == "ON"]
        ref = min(on, key=lambda i: abs(cuts[i]["tstart"] - case["mjd"]))
        t0 = min(c["tstart"] for c in cuts)
        smear = abs(d0) * cuts[ref]["tsamp"] / abs(cuts[ref]["foff"] * 1e6) + 1
        out = []
        for f in fdc.places(case, cuts, half)[1:3]:
            f_start = f - data_drift * (cuts[ref]["tstart"] - t0) * 86400.0 * 1e-6
            for kind, snr, off in VARIANTS:
                mine = copy.deepcopy(cuts)
                investigate.apply_injection(
                    {
                        "inject": {
                            "f0_mhz": f_start,
                            "drift": data_drift,
                            "snr": snr * smear,
                            "present_on": [1, 1, 1],
                            "scint": [1.0, 1.0, 1.0],
                            "off_fraction": off,
                        }
                    },
                    scans,
                    mine,
                )
                probe = {
                    "id": "probe",
                    "target": case["target"],
                    "freq_mhz": f,
                    "drift": d0,
                    "mjd": case["mjd"],
                    "freq_exact": True,
                }
                seed, info = investigate.pick_seed(probe, scans, mine, half)
                # the seed may be in any ON scan; where it is placed against the
                # catalogue position is offset_from_catalogue_hz, carried back
                # along its own drift
                found = bool(
                    info["recovered"]
                    and abs(info.get("offset_from_catalogue_hz", 1e9))
                    <= investigate._seed_half_hz(d0, cuts[ref]["tsamp"]) + 50
                    and info["drift"] * data_drift > 0
                )
                bundle = {
                    "seed": info,
                    "scans": investigate.measure(scans, mine, seed, info, half),
                }
                if info["recovered"]:
                    bundle["off_scans_stacked"] = investigate.off_stack(
                        scans, mine, seed, info
                    )
                    bundle["followed_line"] = investigate.follow_line(
                        scans, mine, seed, info
                    )
                dec = triage.decide(bundle)
                out.append(
                    {
                        "kind": kind,
                        "snr": snr,
                        "drift": abs(d0),
                        "found": found,
                        "status": dec["status"],
                        "verdict": dec["verdict"],
                        "why": [x[:2] for x in dec["direct_evidence"]],
                    }
                )
        return out
    except Exception as e:
        return f"{case['id']}: {type(e).__name__}: {e}"[:200]


def main():
    args = sys.argv[1:]
    every = int(args[args.index("--every") + 1]) if "--every" in args else 2
    jobs = [
        (case, str(path))
        for case, path in backfill.cases()
        if path.exists()
        and "inject" not in case
        and case.get("freq_exact")
        and abs(case.get("drift", 0.0)) >= 1.0
    ][::every]
    rows = []
    with ProcessPoolExecutor(max_workers=6) as ex:
        for res in ex.map(one, jobs, chunksize=2):
            if isinstance(res, str):
                print(res)
            elif res:
                rows += res
    print(f"{len(jobs)} fast cadences, {len(rows)} injections")
    by = defaultdict(list)
    for r in rows:
        by[(r["kind"], r["snr"])].append(r)
    print("\n  kind  S/N  n    found   left open  closed by a rule (why)")
    for (kind, snr), rs in sorted(by.items()):
        found = [r for r in rs if r["found"]]
        opened = sum(r["status"] == "open" for r in found)
        closed = [
            r
            for r in found
            if r["status"] == "closed" and r["verdict"] != "not_recovered"
        ]
        why = Counter(w for r in closed for w in set(r["why"]))
        print(
            f"  {kind:<5} {snr:>3} {len(rs):>4}  {len(found):>4} ({len(found) / len(rs):4.0%})"
            f"  {opened:>4}       {len(closed):>4} {dict(why)}"
        )


if __name__ == "__main__":
    main()
