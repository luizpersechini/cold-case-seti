"""Phase 2 pilot: 50 blinded cases mixing real near-miss hits from the queue
with synthetic signals injected into the same data.

  build   choose the cases and write cases/pilot/*.json plus TRUTH.json
  run     gather evidence and blinded briefs, one data chunk group at a time

Kinds (hidden from readers):
  real              a queue case from the survey hit tables; truth unknown
  sky_persistent    injected in all three ON scans, nothing in OFF
  sky_intermittent  injected in one or two ON scans, nothing in OFF
  leak              injected in ON scans and, at 12-25% amplitude, in OFF scans
Injections share data chunks with the real cases so they cost no extra
download and sit in the same interference environment.

usage: pilot.py build | run
"""

import json
import sys
import time

import numpy as np

from coldcase import brief, db, investigate, remote

PILOT = remote.ROOT / "cases" / "pilot"
TRACKS = remote.DATA / "derived" / "work" / "tracks_nonrfi_drifting.parquet"
N_TOTAL, N_GROUPS, SEED = 50, 10, 20261112
QUEUE_SQL = f"""
with t as (select * from '{TRACKS}'),
s2 as (select * from t where abs(drift) < 3.9 and (
    (band = 'L' and freq between 1100 and 1900 and not freq between 1200 and 1340) or
    (band = 'S' and freq between 1800 and 2800 and not freq between 2300 and 2360))),
s3 as (select * from s2 where nsrc <= 1),
s4 as (select * from s3 where (cadence, seed_scan) in
    (select cadence, seed_scan from s3 group by 1, 2 having count(*) <= 20))
select cadence, target, band, seed_scan, freq, drift, snr, n_on, cls from s4
where snr >= 25 and band = 'L'
"""


def build():
    rng = np.random.default_rng(SEED)
    con = db.connect()
    cols = (
        "cadence",
        "target",
        "band",
        "seed_scan",
        "freq",
        "drift",
        "snr",
        "n_on",
        "cls",
    )
    queue = [dict(zip(cols, r)) for r in con.execute(QUEUE_SQL).fetchall()]
    scan_of = {}  # (cadence, pos) -> (file_id, mjd)
    for cid, pos, fid, mjd in con.execute(
        "select c.cadence_id, c.pos, c.file_id, s.mjd from cadences c "
        "join scans s using (file_id)"
    ).fetchall():
        scan_of[(cid, pos)] = (fid, mjd)
    groups = {}
    for q in queue:
        fid, mjd = scan_of[(q["cadence"], int(q["seed_scan"]))]
        row = con.execute(
            "select coarse from hits where file_id = ? order by abs(freq - ?) limit 1",
            [fid, q["freq"]],
        ).fetchall()
        q["coarse"], q["seed_mjd"], q["seed_file"] = int(row[0][0]), mjd, fid
        groups.setdefault((q["cadence"], q["coarse"]), []).append(q)
    # groups holding a two-scan intermittent case first, then the fullest ones;
    # one group per cadence
    order = sorted(
        groups,
        key=lambda g: (
            -max(x["n_on"] for x in groups[g]),
            -len(groups[g]),
            rng.random(),
        ),
    )
    chosen, seen_cad = [], set()
    for g in order:
        if g[0] not in seen_cad and len(chosen) < N_GROUPS:
            chosen.append(g)
            seen_cad.add(g[0])
    cases = []
    for gi, g in enumerate(chosen):
        members = sorted(groups[g], key=lambda x: (-x["n_on"], -x["snr"]))[:2]
        fid0, mjd0 = scan_of[(g[0], 0)]
        # chunk bounds from one hit: freq = fch1 + chan * foff, and a storage chunk
        # is one coarse channel of 1,048,576 fine channels
        chan, freq = con.execute(
            "select chan, freq from hits where file_id = ? and coarse = ? limit 1",
            [members[0]["seed_file"], g[1]]).fetchall()[0]
        foff = -2.7939677238464355e-06
        edge = freq - chan * foff  # chan is the index inside the coarse channel
        lo, hi = edge + 1048575 * foff, edge
        for q in members:
            cases.append(
                {
                    "kind": "real",
                    "group": gi,
                    "target": q["target"],
                    "site": "GBT",
                    "freq_mhz": q["freq"],
                    "drift": float(q["drift"]),
                    "mjd": q["seed_mjd"],
                    "freq_exact": True,
                    "queue": {
                        "cls": q["cls"],
                        "n_on": int(q["n_on"]),
                        "snr": float(q["snr"]),
                    },
                }
            )
        groups[g] = {
            "members": members,
            "lo": lo,
            "hi": hi,
            "mjd0": mjd0,
            "gi": gi,
            "target": members[0]["target"],
            "cadence": g[0],
        }
    n_inj = N_TOTAL - len(cases)
    kinds = (["sky_persistent", "sky_intermittent", "leak"] * (n_inj // 3 + 1))[:n_inj]
    rng.shuffle(kinds)
    patterns = [[1, 0, 0], [0, 1, 0], [0, 0, 1], [1, 1, 0], [0, 1, 1], [1, 0, 1]]
    for i, kind in enumerate(kinds):
        g = groups[chosen[i % len(chosen)]]
        taken = [c["freq_mhz"] for c in cases if c["group"] == g["gi"]]
        while True:
            f0 = float(rng.uniform(g["lo"] + 0.1, g["hi"] - 0.1))
            if g["hi"] - g["lo"] < 0.5 or all(abs(f0 - t) > 0.02 for t in taken):
                break
        drift = float(rng.uniform(0.03, 0.5) * rng.choice([-1, 1]))
        if kind == "leak":
            snr, off = (
                float(np.exp(rng.uniform(np.log(25), np.log(80)))),
                float(rng.uniform(0.12, 0.25)),
            )
        else:
            snr, off = float(np.exp(rng.uniform(np.log(12), np.log(60)))), 0.0
        present = (
            [1, 1, 1]
            if kind != "sky_intermittent"
            else patterns[rng.integers(len(patterns))]
        )
        seed_pos = (0, 2, 4)[present.index(1)]
        seed_mjd = scan_of[(g["cadence"], seed_pos)][1]
        dt = (seed_mjd - g["mjd0"]) * 86400.0
        cases.append(
            {
                "kind": kind,
                "group": g["gi"],
                "target": g["target"],
                "site": "GBT",
                "freq_mhz": round(f0 + drift * dt * 1e-6, 6),
                "drift": round(drift, 4),
                "mjd": seed_mjd,
                "freq_exact": True,
                "inject": {
                    "f0_mhz": f0,
                    "drift": drift,
                    "snr": round(snr, 1),
                    "present_on": present,
                    "off_fraction": round(off, 3),
                    "scint": [round(float(x), 2) for x in rng.uniform(0.7, 1.3, 3)],
                },
            }
        )
    ids = rng.permutation(len(cases))  # ids must not reveal the kind or the order
    PILOT.mkdir(parents=True, exist_ok=True)
    truth = {}
    for n, c in zip(ids, cases):
        c["id"] = f"p{n + 1:03d}"
        truth[c["id"]] = {"kind": c.pop("kind"), "group": c["group"]}
        (PILOT / f"{c['id']}.json").write_text(json.dumps(c))
    (PILOT / "TRUTH.json").write_text(json.dumps(truth, indent=1))
    kinds_count = {}
    for t in truth.values():
        kinds_count[t["kind"]] = kinds_count.get(t["kind"], 0) + 1
    print(f"queue={len(queue)} groups={len(chosen)} cases={len(cases)} {kinds_count}")


PATTERNS = [[1, 0, 0], [0, 1, 0], [0, 0, 1], [1, 1, 0], [0, 1, 1], [1, 0, 1]]


def make_injection(rng, kind, g, scan_of, taken):
    """One injected case in group g (keys: lo, hi, mjd0, gi, target, cadence)."""
    while True:
        f0 = float(rng.uniform(g["lo"] + 0.1, g["hi"] - 0.1))
        if all(abs(f0 - t) > 0.02 for t in taken):
            break
    drift = float(rng.uniform(0.03, 0.5) * rng.choice([-1, 1]))
    if kind == "leak":
        snr = float(np.exp(rng.uniform(np.log(25), np.log(80))))
        off = float(rng.uniform(0.12, 0.25))
    else:
        snr, off = float(np.exp(rng.uniform(np.log(12), np.log(60)))), 0.0
    present = [1, 1, 1] if kind != "sky_intermittent" else PATTERNS[rng.integers(len(PATTERNS))]
    seed_mjd = scan_of[(g["cadence"], (0, 2, 4)[present.index(1)])][1]
    dt = (seed_mjd - g["mjd0"]) * 86400.0
    return {
        "kind": kind, "group": g["gi"], "target": g["target"], "site": "GBT",
        "freq_mhz": round(f0 + drift * dt * 1e-6, 6), "drift": round(drift, 4),
        "mjd": seed_mjd, "freq_exact": True,
        "inject": {"f0_mhz": f0, "drift": drift, "snr": round(snr, 1), "present_on": present,
                   "off_fraction": round(off, 3),
                   "scint": [round(float(x), 2) for x in rng.uniform(0.7, 1.3, 3)]}}


def build_holdout():
    """30 fresh injections in 6 cadences and data chunks the pilot never used,
    at frequencies drawn uniformly over the L band (not where the queue lives).
    Scored by the code triage alone."""
    rng = np.random.default_rng(SEED + 1)
    con = db.connect()
    out = remote.ROOT / "cases" / "holdout"
    out.mkdir(parents=True, exist_ok=True)
    scan_of = {}
    for cid, pos, fid, mjd in con.execute(
            "select c.cadence_id, c.pos, c.file_id, s.mjd from cadences c "
            "join scans s using (file_id)").fetchall():
        scan_of[(cid, pos)] = (fid, mjd)
    used = set()
    if (PILOT / "TRUTH.json").exists():
        for f in PILOT.glob("p*.json"):
            used.add(json.loads(f.read_text())["target"].upper())
    lband = [r[0] for r in con.execute(
        "select distinct c.cadence_id from cadences c join scans s using (file_id) "
        "where s.band = 'L' and c.pos = 0 and upper(s.source) not in "
        f"({','.join(repr(u) for u in used) or repr('')})").fetchall()]
    foff, width = -2.7939677238464355e-06, 1048576 * 2.7939677238464355e-06
    kinds = ["sky_persistent", "sky_intermittent", "leak"] * 10
    rng.shuffle(kinds)
    cases, gi = [], 0
    for cad in rng.permutation(lband):
        if gi == 6:
            break
        fid0, mjd0 = scan_of[(int(cad), 0)]
        hit = con.execute("select freq, chan, coarse from hits where file_id = ? limit 1",
                          [fid0]).fetchall()
        if not hit:
            continue
        freq, chan, coarse = hit[0]
        top0 = freq - chan * foff + coarse * width  # top edge of coarse channel 0
        while True:
            fa = float(rng.uniform(1100, 1900))
            if not 1200 <= fa <= 1340:
                break
        c = int((top0 - fa) // width)
        hi = top0 - c * width
        g = {"lo": hi - width, "hi": hi, "mjd0": mjd0, "gi": gi, "cadence": int(cad),
             "target": con.execute("select source from scans where file_id = ?",
                                   [fid0]).fetchall()[0][0]}
        for kind in kinds[gi * 5:gi * 5 + 5]:
            cases.append(make_injection(rng, kind, g, scan_of,
                                        [x["freq_mhz"] for x in cases if x["group"] == gi]))
        gi += 1
    truth = {}
    for n, c in zip(rng.permutation(len(cases)), cases):
        c["id"] = f"h{n + 1:03d}"
        truth[c["id"]] = {"kind": c.pop("kind"), "group": c["group"]}
        (out / f"{c['id']}.json").write_text(json.dumps(c))
    (out / "TRUTH.json").write_text(json.dumps(truth, indent=1))
    print(f"holdout: {len(cases)} injections in {gi} groups; chunk tops (MHz):",
          sorted({round(json.loads((out / f'{k}.json').read_text())['freq_mhz']) for k in truth}))


def refresh_rows(case_path, ev_path):
    """Recompute the per-scan measurements of an existing bundle from cached
    cutouts when it predates a field readers now rely on."""
    b = json.loads(ev_path.read_text())
    if all("track_time_profile" in s for s in b["scans"] if "error" not in s):
        return
    case = json.loads(case_path.read_text())
    scans, _ = investigate.resolve_scans(case, db.connect())
    half = case.get("half_khz", max(5.0, abs(case.get("drift", 0.0)) * 2.5 + 3.0))
    cuts = [investigate._fetch((s["url"], case["freq_mhz"], half)) for s in scans]
    if "inject" in case:
        investigate.apply_injection(case, scans, cuts)
    seed, info = investigate.pick_seed(case, scans, cuts, half)
    b["scans"] = investigate.measure(scans, cuts, seed, info, half)
    ev_path.write_text(json.dumps(b, indent=1))
    print(f"  refreshed measurements for {case['id']}", flush=True)


def run(name="pilot", briefs=True):
    import shutil

    PILOT = remote.ROOT / "cases" / name  # noqa: N806
    truth = json.loads((PILOT / "TRUTH.json").read_text())
    by_group = {}
    for cid, t in truth.items():
        by_group.setdefault(t["group"], []).append(cid)
    for gi in sorted(by_group):
        t0, complete = time.time(), True
        for cid in sorted(by_group[gi]):
            path = PILOT / f"{cid}.json"
            ev_dir = remote.ROOT / "cases" / "evidence" / cid
            if not (ev_dir / "evidence.json").exists():
                try:
                    b = investigate.run(json.loads(path.read_text()), verbose=False)
                except BaseException as e:  # keep going; the failure is on record
                    print(f"group {gi} {cid} FAILED {type(e).__name__}: {e}", flush=True)
                    complete = False
                    continue
                bad = [s["pos"] for s in b["scans"] if "error" in s]
                if bad:  # never keep evidence with a scan missing; retried next run
                    shutil.rmtree(ev_dir)
                    print(f"group {gi} {cid} INCOMPLETE scans {bad} unreadable", flush=True)
                    complete = False
                    continue
                on = [s["track"]["snr"] for s in b["scans"] if s["role"] == "ON"]
                off = [s["track"]["snr"] for s in b["scans"] if s["role"] == "OFF"]
                print(f"group {gi} {cid} recovered={b['seed']['recovered']} ON {on} OFF {off}",
                      flush=True)
            if not briefs:
                continue
            refresh_rows(path, ev_dir / "evidence.json")
            code, _ = brief.build(path)
            key_path = remote.ROOT / "cases" / "briefs" / "KEY.json"
            key = json.loads(key_path.read_text()) if key_path.exists() else {}
            key[code] = str(path)
            key_path.write_text(json.dumps(key, indent=1))
        if complete:
            remote.clear_chunks()  # downloaded chunks are kept while a group is unfinished
        print(f"group {gi} {'done' if complete else 'UNFINISHED'} in {time.time() - t0:.0f} s",
              flush=True)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "build":
        build()
    elif cmd == "build-holdout":
        build_holdout()
    elif cmd == "run":
        name = sys.argv[2] if len(sys.argv) > 2 else "pilot"
        run(name, briefs=name == "pilot")
