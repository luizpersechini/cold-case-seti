"""How often does the bend flag (coldcase/curvature.py) fire on a tone that
behaves like a source on the sky?

An independent check of the flag's threshold, which was set on the running
test signals (drift 0.5 Hz/s at most). Beside every queue case whose
catalogue drift is --min-drift or more and whose cutouts are cached, two
test tones are injected 1.5 kHz either side, at the case's drift, present in
all three ON scans, with a bend drawn between -1e-5 and +1e-5 Hz/s^2 (the
range the Earth's motion gives at these frequencies), S/N drawn as in
tools/fast_drift_rules.py. Each is followed from where it was injected (the
seed search is tested elsewhere) and measured as a queue case is.

Nothing is downloaded and the ledger is not touched. Writes
cases/curvature_controls.json.

usage: python tools/curvature_controls.py [--min-drift 0.5]
"""

import copy
import hashlib
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

from coldcase import backfill, curvature, investigate

OUT = Path(__file__).resolve().parent.parent / "cases" / "curvature_controls.json"


def one(job):
    case, path = job
    try:
        b = json.loads(Path(path).read_text())
        got = investigate._cached_cuts(case, b)
        if got is None:
            return None
        scans, cuts, _ = got
        if any("error" in c for c in cuts):
            return None
        data_drift = -case[
            "drift"
        ]  # the hit tables' sign is the opposite of the data's
        t0 = min(c["tstart"] for c in cuts)
        on = [i for i, s in enumerate(scans) if s["role"] == "ON"]
        tsamp = cuts[on[0]]["tsamp"]
        smear = abs(data_drift) * tsamp / abs(cuts[on[0]]["foff"] * 1e6) + 1
        f_t0 = case["freq_mhz"] - data_drift * (case["mjd"] - t0) * 86400.0 * 1e-6
        rng = np.random.default_rng(
            int(hashlib.sha1(case["id"].encode()).hexdigest()[:8], 16) + 7
        )
        out = []
        for off_hz in (-1500.0, 1500.0):
            f0 = f_t0 + off_hz * 1e-6
            snr = float(np.exp(rng.uniform(np.log(12), np.log(60))))
            bend = float(rng.uniform(-1e-5, 1e-5))
            mine = copy.deepcopy(cuts)
            investigate.apply_injection(
                {
                    "inject": {
                        "f0_mhz": f0,
                        "drift": data_drift,
                        "snr": snr * smear,
                        "present_on": [1, 1, 1],
                        "off_fraction": 0.0,
                        "scint": [float(x) for x in rng.uniform(0.7, 1.3, 3)],
                        "flicker": float(rng.uniform(0.15, 0.4)),
                        "curve": bend,
                        "seed": int(rng.integers(1 << 30)),
                    }
                },
                scans,
                mine,
            )
            first = on[0]
            dt = (cuts[first]["tstart"] - t0) * 86400.0
            seed = {
                "seed_pos": scans[first]["pos"],
                "freq_mhz": f0 + (data_drift * dt + 0.5 * bend * dt * dt) * 1e-6,
                "drift": data_drift + bend * dt,
            }
            m = curvature.measure_cuts(
                case["id"], seed, b["pointing"], b.get("site", "GBT"), scans, mine
            )
            out.append(
                {
                    "case": case["id"],
                    "drift": abs(data_drift),
                    "snr": round(snr, 1),
                    "injected_bend": bend,
                    "measured": "bend_hz_s2" in m,
                    "bend_hz_s2": m.get("bend_hz_s2"),
                    "bend_err": m.get("bend_err"),
                    "sigmas": m.get("sigmas"),
                    "flag": m["flag"],
                }
            )
        return out
    except Exception as e:
        return f"{case['id']}: {type(e).__name__}: {e}"[:300]


def main():
    args = sys.argv[1:]
    min_drift = (
        float(args[args.index("--min-drift") + 1]) if "--min-drift" in args else 0.5
    )
    jobs = [
        (case, str(path))
        for case, path in backfill.cases()
        if path.exists()
        and "inject" not in case
        and case.get("freq_exact")
        and abs(case.get("drift", 0.0)) >= min_drift
    ]
    rows = []
    with ProcessPoolExecutor(max_workers=6) as ex:
        for res in ex.map(one, jobs, chunksize=2):
            if isinstance(res, str):
                print(res)
            elif res:
                rows += res
    OUT.write_text(json.dumps(rows, indent=1))
    meas = [r for r in rows if r["measured"]]
    fl = [r for r in meas if r["flag"]]
    pulls = np.array(
        [(r["bend_hz_s2"] - r["injected_bend"]) / r["bend_err"] for r in meas]
    )
    print(
        f"{len(jobs)} cases, {len(rows)} test tones, {len(meas)} measured, {len(fl)} flagged; "
        f"pull MAD {1.4826 * np.median(np.abs(pulls - np.median(pulls))):.2f}, "
        f"{int(np.sum(np.abs(pulls) > 5))} beyond 5 sigma"
    )
    for r in fl:
        print("  flagged:", r)


if __name__ == "__main__":
    main()
