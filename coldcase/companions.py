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
       python -m coldcase.companions null               chance of a false
                                                        companion (cached data)
       python -m coldcase.companions inject             rule D8 on injected
                                                        sidebands (cached data)
       python -m coldcase.companions members [--no-session] [CASE_ID ...]
           each companion as a case of its own (off-star, burst and, unless
           --no-session, same-half-hour and other-day tests)
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


def _event_profiles(b, ev_cuts):
    """The event's own track, sample by sample, in every scan that was read."""
    out = {}
    for i, (s, c) in enumerate(zip(b["scans"], ev_cuts)):
        if "error" in c or "error" in s or s["track"].get("freq_mhz") is None:
            continue
        k = int(round((s["track"]["freq_mhz"] - c["fch1"]) / c["foff"]))
        out[i] = _profile(c, k, s["track"]["drift"])
    return out


def judge(rows_ev, cuts, ref, lead, ev_prof, want):
    """One lead, {freq_mhz, drift}, measured in the cutouts `cuts` of the
    event's six scans: in which scans a line stands above its control
    windows there, and how closely it follows the event sample by sample.
    want: the positions of the scans in which the event stands out."""
    c0 = cuts[ref]
    scans, prof = [], {}
    for i, (s, c) in enumerate(zip(rows_ev, cuts)):
        if "error" in c:
            continue
        dt = (c["tstart"] - c0["tstart"]) * 86400.0
        k = (lead["freq_mhz"] + lead["drift"] * dt * 1e-6 - c["fch1"]) / c["foff"]
        tr, ctrl, _, _, _, _ = investigate._track_and_controls(c, k, dt, lead["drift"])
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
    return {
        **lead,
        "scans": scans,
        "stands_out_in": sorted(there),
        "correlation_with_event": corr,
        "outcome": outcome(there, want, corr),
    }


def outcome(there, want, corr):
    """A companion stands above its controls in one or more of the scans in
    which the event does, in none of the others, and follows the event sample
    by sample. A weaker companion need not reach its margin in every one of
    the event's scans."""
    if not there:
        return "no distinct line"
    if not set(there) <= set(want):
        return "a line, in other scans than the event"
    if corr is None or corr < CORR_MIN:
        return "a line in the same scans, not following the event"
    return "companion"


def rejudge(say=print):
    """Apply today's outcome() to the stored measurements."""
    n = 0
    for p in sorted(OUT.glob("q*.json")):
        d = json.loads(p.read_text())
        want = _pattern(_bundle(p.stem))
        for x in d["leads"]:
            if "scans" not in x:
                continue
            new = outcome(set(x["stands_out_in"]), want, x["correlation_with_event"])
            if new != x["outcome"]:
                say(f"{p.stem} {x['freq_mhz']:.6f}: {x['outcome']} -> {new}")
                x["outcome"] = new
                n += 1
        p.write_text(json.dumps(d, indent=1))
    say(f"{n} outcomes changed")


def measure(cid, found, con):
    """The leads of one event in the raw data. Returns (rows, megabytes)."""
    from . import runner

    b = _bundle(cid)
    # a queue case, or one kept outside the queue (coldcase/followup.py)
    case = runner._case_from_queue(con, cid) if cid.startswith("q") else b["case"]
    got = investigate._cached_cuts(case, b)
    if got is None:
        return None, 0.0
    _, ev_cuts, _ = got
    rows_ev = b["scans"]
    ref = next(i for i, s in enumerate(rows_ev) if s["pos"] == b["seed"]["seed_pos"])
    want = set(found["event"]["scans_with_the_line"])
    ev_prof = _event_profiles(b, ev_cuts)
    out, mb = [], 0.0

    def net(cs):
        return (
            sum(
                0 if c.get("cached") else c.get("net_bytes", 0)
                for c in cs
                if "error" not in c
            )
            / 1e6
        )

    for lead in found["leads"]:
        # the event's own scan first: it decides most leads, and the other
        # five scans are read only for a lead that stands out there and
        # follows the event
        first = repeat._fetch_all([(rows_ev[ref]["url"], lead["freq_mhz"], HALF_KHZ)])
        mb += net(first)
        if "error" in first[0]:
            out.append({**lead, "outcome": "not measured", "why": first[0]["error"]})
            continue
        only = [
            first[0] if i == ref else {"error": "not read"} for i in range(len(rows_ev))
        ]
        r = judge(
            rows_ev,
            only,
            ref,
            lead,
            {ref: ev_prof[ref]} if ref in ev_prof else {},
            {rows_ev[ref]["pos"]},
        )
        if r["outcome"] != "companion":
            r["measured_in"] = "the event's own scan only"
            out.append(r)
            continue
        rest = repeat._fetch_all(
            [
                (s["url"], lead["freq_mhz"], HALF_KHZ)
                for i, s in enumerate(rows_ev)
                if i != ref
            ]
        )
        mb += net(rest)
        it = iter(rest)
        cuts = [first[0] if i == ref else next(it) for i in range(len(rows_ev))]
        out.append(judge(rows_ev, cuts, ref, lead, ev_prof, want))
    return out, mb


def null_test(say=print):
    """How often a place where nothing is catalogued passes as a companion.
    Uses the cutouts the measurement downloaded for each lead (cached): at
    places 0.4 to 3.6 kHz from the lead, the same judgement as for the lead
    itself, against the same event. Nothing is downloaded."""
    from . import runner

    con = runner.queue_db()
    n = comp = 0
    for p in sorted(OUT.glob("q*.json")):
        cid = p.stem
        b = _bundle(cid)
        got = investigate._cached_cuts(runner._case_from_queue(con, cid), b)
        if got is None:
            continue
        rows_ev = b["scans"]
        ref = next(
            i for i, s in enumerate(rows_ev) if s["pos"] == b["seed"]["seed_pos"]
        )
        ev_prof = _event_profiles(b, got[1])
        want = _pattern(b)
        for lead in json.loads(p.read_text())["leads"]:
            if "scans" not in lead:
                continue
            paths = [
                remote._cache_path(s["url"], lead["freq_mhz"], HALF_KHZ)
                for s in rows_ev
            ]
            if not all(x.exists() for x in paths):
                continue
            cuts = [
                dict(remote.read_cutout(s["url"], lead["freq_mhz"], HALF_KHZ))
                for s in rows_ev
            ]
            for m in (-9, -7, -5, -3, -1, 1, 3, 5, 7, 9):
                place = {
                    "freq_mhz": lead["freq_mhz"] + m * 0.0004,
                    "drift": lead["drift"],
                }
                r = judge(rows_ev, cuts, ref, place, ev_prof, want)
                n += 1
                comp += r["outcome"] == "companion"
    say(
        f"null test: {comp} of {n} places with nothing catalogued pass as companions ({comp / max(n, 1):.2%})"
    )
    return comp, n


def injection_test(max_cases=80, sep_khz=2.0, say=print):
    """Does D8 see injected sidebands, and stay quiet without them? On the
    blind sky-only controls whose cutouts are cached: once as they are, with
    leads placed sep_khz either side of the injected tone (nothing there),
    and once with two sidebands added there, at 0.6 times the tone's S/N and
    with its own moment-to-moment variation. Nothing is downloaded."""
    from . import controls, ledger

    led = ledger.connect()
    rows = led.execute(
        "select case_id, drift, inj_snr from controls where kind like 'sky%' "
        "and recovered=1 and masked=0 order by case_id"
    ).fetchall()
    res = {"plain": [0, 0], "sidebands": [0, 0]}
    done = 0
    for cid, drift, inj in rows:
        if done >= max_cases:
            break
        case = json.loads(
            (remote.ROOT / "cases" / "controls" / f"{cid}.json").read_text()
        )
        b = _bundle(cid)
        if controls.is_masked(b, drift, inj):
            continue
        half = case.get("half_khz", max(5.0, abs(case.get("drift", 0.0)) * 2.5 + 3.0))
        if half < sep_khz + 1.5:
            continue
        got = investigate._cached_cuts(case, b)
        if got is None:
            continue
        scans, cuts, _ = got
        rows_ev = b["scans"]
        ref = next(
            i for i, s in enumerate(rows_ev) if s["pos"] == b["seed"]["seed_pos"]
        )
        want = _pattern(b)
        if not want:
            continue
        done += 1
        f, d = b["seed"]["freq_mhz"], b["seed"]["drift"]
        leads = [{"freq_mhz": f + sgn * sep_khz * 1e-3, "drift": d} for sgn in (-1, 1)]
        for kind in ("plain", "sidebands"):
            cc = [dict(c) for c in cuts]
            if kind == "sidebands":
                for c, c_orig in zip(cc, cuts):
                    if "data" in c:
                        c["data"] = np.array(c_orig["data"], dtype=np.float64)
                for sgn in (-1, 1):
                    spec = dict(case["inject"])
                    spec["f0_mhz"] = spec["f0_mhz"] + sgn * sep_khz * 1e-3
                    spec["snr"] = 0.6 * spec["snr"]
                    investigate.apply_injection({"inject": spec}, scans, cc)
            ev_prof = _event_profiles(b, cc)
            comp = {"leads": [judge(rows_ev, cc, ref, x, ev_prof, want) for x in leads]}
            text, _ = triage.companion_rule(b, comp)
            res[kind][0] += 1
            res[kind][1] += bool(text)
    say(
        f"injection test on {done} sky-only controls: D8 closes {res['plain'][1]} of "
        f"{res['plain'][0]} with nothing beside them, and {res['sidebands'][1]} of "
        f"{res['sidebands'][0]} with two sidebands {sep_khz} kHz away"
    )
    return res


MEMBERS = OUT / "members"


def member_check(cid, session=True, say=print):
    """Each companion of an open event measured as a case of its own: the
    off-star and burst tests from the cutouts already downloaded for it,
    and, with session, the same-half-hour check (which downloads the
    neighbouring scans). Results are stored with the event's companion
    record under "members"; rule D8 closes the event when a member is closed
    by D1, D4, D5 or D7. With session, the other-day check is made too."""
    p = OUT / f"{cid}.json"
    d = json.loads(p.read_text())
    eb = _bundle(cid)
    MEMBERS.mkdir(parents=True, exist_ok=True)
    out = []
    for n, x in enumerate(d["leads"]):
        if x.get("outcome") != "companion":
            continue
        mid = f"{cid}_c{n}"
        case = {
            **eb["case"],
            "id": mid,
            "freq_mhz": x["freq_mhz"],
            "drift": x["drift"],
            "half_khz": HALF_KHZ,
            "freq_exact": True,
        }
        pseudo = {
            k: v
            for k, v in eb.items()
            if k
            not in (
                "lookalikes_1khz",
                "lookalikes_10khz",
                "same_drift_twins",
                "clock_probes",
                "family",
                "off_scans_stacked",
            )
        }
        pseudo["case"] = case
        mb = investigate.remeasure(case, pseudo, None)
        if mb is None:
            say(f"{mid}: its cutouts are not in the cache")
            continue
        mb["case"] = case
        for key in ("same_drift_twins", "clock_probes"):
            mb.pop(key, None)
        sess = days = None
        if session and mb["seed"].get("recovered"):
            from . import session as sm

            sess = sm.run(mid, save=False, close=False, verbose=False, bundle=mb)
            days = repeat.run(mid, save=False, verbose=False, bundle=mb)
        dec = triage.decide(mb, session=sess, repeat=days)
        rec = {
            "member": mid,
            "freq_mhz": x["freq_mhz"],
            "recovered": bool(mb["seed"].get("recovered")),
            "seed_snr": mb["seed"].get("snr"),
            "session": (sess or {}).get("summary"),
            "other_days": (days or {}).get("summary"),
            "status": dec["status"],
            "verdict": dec["verdict"],
            "direct_evidence": dec["direct_evidence"],
        }
        (MEMBERS / f"{mid}.json").write_text(
            json.dumps({**rec, "bundle": mb}, indent=1)
        )
        out.append(rec)
        say(
            f"{mid} {x['freq_mhz']:.6f} MHz: seed S/N {rec['seed_snr']}, {dec['status']}"
            + (
                f" ({'; '.join(dec['direct_evidence'])[:160]})"
                if dec["direct_evidence"]
                else ""
            )
            + (f"; same half hour: {sess['summary']['text']}" if sess else "")
        )
    d["members"] = out
    p.write_text(json.dumps(d, indent=1))
    return out


def main():
    args = sys.argv[1:]
    if not args or args[0] not in (
        "leads",
        "measure",
        "null",
        "inject",
        "rejudge",
        "members",
    ):
        print(__doc__)
        return
    if args[0] == "null":
        null_test()
        return
    if args[0] == "inject":
        for sep in (2.0, 3.0):
            injection_test(sep_khz=sep)
        return
    if args[0] == "rejudge":
        rejudge()
        return
    if args[0] == "members":
        sess = "--no-session" not in args
        ids = [a for a in args[1:] if not a.startswith("--")] or [
            q.stem
            for q in sorted(OUT.glob("q*.json"))
            if any(
                x.get("outcome") == "companion"
                for x in json.loads(q.read_text())["leads"]
            )
        ]
        if sess:
            with repeat._turn():
                for cid in ids:
                    member_check(cid, session=True)
        else:
            for cid in ids:
                member_check(cid, session=False)
        return
    from . import runner

    con = runner.queue_db()
    OUT.mkdir(parents=True, exist_ok=True)
    if args[0] == "leads":
        hits = db.connect()
        # named cases are added to what is there; no names: every open event
        given = [a for a in args[1:] if not a.startswith("--")]
        old = OUT / "leads.json"
        res = json.loads(old.read_text()) if given and old.exists() else {}
        for cid in given or _open_ids(con):
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
