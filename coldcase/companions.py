"""Companions: other lines that share an open event's scans and its drift.

A transmitter seldom sends one line. If a second line elsewhere in the band
drifts as the event does, is there in the same scans and switches on and off
with it, the two are one emitter, and what is known about either says
something about both. This module looks for such lines in two steps.

  leads     from the survey's hit tables, free: hits in the event's seed
            scan, anywhere in the band, whose listed drift (turned to the
            data's sign and scaled by frequency) matches the event's, and
            which the tables show in the same scans of the cadence as the
            event and in no other.
  measure   for the strongest MAX_LEADS leads of an event, in the raw data
            of the six scans: is there a line that stands above its control
            windows in the event's scans and not in the others, and does it
            follow the event sample by sample?

The result is a flag: it feeds the dossier and the rank and closes nothing.
The hit table alone is not enough: three hits that looked like companions
of q00671 turned out, in the raw data, to be part of an unrelated forest of
lines (2026-11-21).

usage: python -m coldcase.companions leads              every open event
       python -m coldcase.companions measure [--max-gb N] [CASE_ID ...]
"""

import json
import sys

import numpy as np

from . import db, investigate, ledger, remote, repeat, triage
from . import dedoppler as dd
from .checks import CATALOGUE_DRIFT_SIGN

OUT = remote.ROOT / "cases" / "companions"
MAX_LEADS = 5
NEAR_MHZ = 0.010  # closer than this to the event is its own cutout, not a lead
HALF_KHZ = 5.0
CORR_MIN = 0.5  # sample-by-sample correlation with the event to count


def _tol_hz(dt, drift):
    return 20.0 + 0.02 * abs(dt) + 0.1 * abs(drift) * abs(dt)


def _open_ids(con):
    return [
        r[0]
        for r in con.execute(
            "select case_id from queue where status in ('open','open_unread') "
            "order by case_id"
        ).fetchall()
    ]


def _bundle(cid):
    return json.loads(
        (remote.ROOT / "cases" / "evidence" / cid / "evidence.json").read_text()
    )


def _pattern(b):
    """The scans in which the event's track stands above all its controls."""
    return frozenset(
        s["pos"]
        for s in b["scans"]
        if "error" not in s and s.get("track_exceeds_all_controls")
    )


def leads(cid, hits):
    """Hit-table leads for one open event: {n_same_drift, pattern, leads}."""
    b = _bundle(cid)
    seed = b["seed"]
    f_ev, d_ev = seed["freq_mhz"], seed["drift"]
    cad = db.cadence_for(hits, b["case"]["target"], b["case"]["mjd"])
    if cad is None:
        return None
    by_pos = {s["pos"]: s for s in cad}
    t0 = by_pos[seed["seed_pos"]]["mjd"]
    tab = {}
    for s in cad:
        r = hits.execute(
            "select freq, drift, snr from hits where file_id=? order by freq",
            [s["file_id"]],
        ).fetchnumpy()
        tab[s["pos"]] = (
            r["freq"],
            CATALOGUE_DRIFT_SIGN * r["drift"].astype(float),
            r["snr"],
        )
    want = _pattern(b)
    F, D, S = tab[seed["seed_pos"]]
    same = (np.abs(F - f_ev) > NEAR_MHZ) & (
        np.abs(D - d_ev * F / f_ev) <= 0.02 + 0.1 * abs(d_ev)
    )
    out = []
    for i in np.flatnonzero(same):
        got = {seed["seed_pos"]}
        for pos, (F2, _, _) in tab.items():
            if pos == seed["seed_pos"] or not len(F2):
                continue
            dt = (by_pos[pos]["mjd"] - t0) * 86400.0
            fp = F[i] + D[i] * dt * 1e-6
            j = min(np.searchsorted(F2, fp), len(F2) - 1)
            near = min(abs(F2[j] - fp), abs(F2[max(j - 1, 0)] - fp)) * 1e6
            if near <= _tol_hz(dt, D[i]):
                got.add(pos)
        if frozenset(got) == want:
            out.append(
                {"freq_mhz": float(F[i]), "drift": float(D[i]), "snr": float(S[i])}
            )
    out.sort(key=lambda x: -x["snr"])
    # one lead per kHz: the strongest of a crowd stands for it
    kept = []
    for x in out:
        if all(abs(x["freq_mhz"] - k["freq_mhz"]) > 1e-3 for k in kept):
            kept.append(x)
    return {
        "event": {"freq_mhz": f_ev, "drift": d_ev, "scans_with_the_line": sorted(want)},
        "same_drift_hits_in_seed_scan": int(same.sum()),
        "same_scans_too": len(out),
        "leads": kept[:MAX_LEADS],
        "leads_not_listed": max(0, len(kept) - MAX_LEADS),
    }


def _profile(cu, chan, drift):
    flat = dd.flatten(cu["data"])
    med = float(np.median(flat))
    mad = 1.4826 * float(np.median(np.abs(flat - med)))
    nt, nf = flat.shape
    t = np.arange(nt) * cu["tsamp"]
    ks = np.clip(
        int(chan) + np.rint(drift * t / (cu["foff"] * 1e6)).astype(int), 1, nf - 2
    )
    return np.array(
        [(flat[i, k - 1 : k + 2].max() - med) / mad for i, k in enumerate(ks)]
    )


def measure(cid, found, con):
    """The leads of one event in the raw data. Returns (rows, megabytes)."""
    from . import runner

    b = _bundle(cid)
    case = runner._case_from_queue(con, cid)
    got = investigate._cached_cuts(case, b)
    if got is None:
        return None, 0.0
    _, ev_cuts, _ = got
    rows_ev = b["scans"]
    ref = next(i for i, s in enumerate(rows_ev) if s["pos"] == b["seed"]["seed_pos"])
    want = set(found["event"]["scans_with_the_line"])
    ev_prof = {}
    for i, (s, c) in enumerate(zip(rows_ev, ev_cuts)):
        if "error" in c or "error" in s or s["track"].get("freq_mhz") is None:
            continue
        k = int(round((s["track"]["freq_mhz"] - c["fch1"]) / c["foff"]))
        ev_prof[i] = _profile(c, k, s["track"]["drift"])
    out, mb = [], 0.0
    for lead in found["leads"]:
        cuts = repeat._fetch_all(
            [(s["url"], lead["freq_mhz"], HALF_KHZ) for s in rows_ev]
        )
        mb += (
            sum(
                0 if c.get("cached") else c.get("net_bytes", 0)
                for c in cuts
                if "error" not in c
            )
            / 1e6
        )
        if "error" in cuts[ref]:
            out.append({**lead, "outcome": "not measured", "why": cuts[ref]["error"]})
            continue
        c0 = cuts[ref]
        scans, prof = [], {}
        for i, (s, c) in enumerate(zip(rows_ev, cuts)):
            if "error" in c:
                continue
            dt = (c["tstart"] - c0["tstart"]) * 86400.0
            k = (lead["freq_mhz"] + lead["drift"] * dt * 1e-6 - c["fch1"]) / c["foff"]
            tr, ctrl, _, _, _, _ = investigate._track_and_controls(
                c, k, dt, lead["drift"]
            )
            if tr["snr"] is None or not len(ctrl):
                continue
            scans.append(
                {
                    "pos": s["pos"],
                    "role": s["role"],
                    "track_snr": round(tr["snr"], 1),
                    "controls_max": round(float(ctrl.max()), 1),
                    "stands_out": bool(tr["snr"] - ctrl.max() >= triage.OFF_MARGIN),
                }
            )
            prof[i] = _profile(c, tr["chan"], tr["drift"])
        there = {x["pos"] for x in scans if x["stands_out"]}
        both = [i for i in ev_prof if i in prof and rows_ev[i]["pos"] in want]
        corr = None
        if both:
            a = np.concatenate([ev_prof[i] for i in both])
            z = np.concatenate([prof[i] for i in both])
            if a.std() > 0 and z.std() > 0:
                corr = round(float(np.corrcoef(a, z)[0, 1]), 2)
        distinct = bool(there)
        same_scans = there == want
        is_comp = distinct and same_scans and corr is not None and corr >= CORR_MIN
        out.append(
            {
                **lead,
                "scans": scans,
                "stands_out_in": sorted(there),
                "correlation_with_event": corr,
                "outcome": "companion"
                if is_comp
                else "no distinct line"
                if not distinct
                else "a line, in other scans than the event"
                if not same_scans
                else "a line in the same scans, not following the event",
            }
        )
    return out, mb


def main():
    args = sys.argv[1:]
    if not args or args[0] not in ("leads", "measure"):
        print(__doc__)
        return
    from . import runner

    con = runner.queue_db()
    OUT.mkdir(parents=True, exist_ok=True)
    if args[0] == "leads":
        hits = db.connect()
        res = {}
        for cid in _open_ids(con):
            r = leads(cid, hits)
            if r is None:
                continue
            res[cid] = r
            if r["leads"]:
                print(
                    f"{cid} {r['event']['freq_mhz']:.3f} MHz drift {r['event']['drift']:+.3f}, "
                    f"line in scans {r['event']['scans_with_the_line']}: "
                    f"{r['same_drift_hits_in_seed_scan']} hits of that drift in the seed scan, "
                    f"{r['same_scans_too']} in the same scans; strongest: "
                    + ", ".join(
                        f"{x['freq_mhz']:.3f} (S/N {x['snr']:.0f})" for x in r["leads"]
                    )
                )
        (OUT / "leads.json").write_text(json.dumps(res, indent=1))
        n = sum(1 for r in res.values() if r["leads"])
        print(
            f"{len(res)} open events; {n} have leads; written to {OUT / 'leads.json'}"
        )
        return
    cap = 10.0
    if "--max-gb" in args:
        i = args.index("--max-gb")
        cap = float(args[i + 1])
        del args[i : i + 2]
    found = json.loads((OUT / "leads.json").read_text())
    ids = args[1:] or [c for c, r in found.items() if r["leads"]]
    used, skipped = 0.0, 0
    with repeat._turn():
        for cid in ids:
            if used / 1e3 >= cap:
                skipped += 1
                continue
            rows, mb = measure(cid, found[cid], con)
            used += mb
            if rows is None:
                print(f"{cid}: its own cutouts are no longer in the cache")
                continue
            (OUT / f"{cid}.json").write_text(
                json.dumps({"case_id": cid, "leads": rows}, indent=1)
            )
            for x in rows:
                print(
                    f"{cid} {x['freq_mhz']:.6f} MHz: {x['outcome']}"
                    + (
                        f" (stands out in scans {x['stands_out_in']}, follows the event r = {x['correlation_with_event']})"
                        if "scans" in x
                        else ""
                    )
                )
            remote.clear_chunks()
    print(
        f"downloaded {used / 1e3:.2f} GB; {skipped} events left for the cap of {cap} GB"
    )


if __name__ == "__main__":
    main()
