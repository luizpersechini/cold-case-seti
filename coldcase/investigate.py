"""Gather the evidence bundle for one case: cutouts for every scan of the
cadence, measurements along the predicted track, and the context checks.
Writes cases/evidence/<id>/evidence.json and waterfall.png, and registers
both in the ledger. Produces measurements only; the verdict is the agents'.

usage: python -m coldcase.investigate CASE_JSON
"""

import json
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np

from . import checks, db, ledger, remote
from . import dedoppler as dd

ON_POS = (0, 2, 4)


class NoData(Exception):
    """The public archive lists no file for any scan of the case."""


def tol_hz(dt, drift):
    return 20.0 + 0.02 * abs(dt) + 0.1 * abs(drift) * abs(dt)


def _quiet_worker():
    """Download workers leave at once and silently when the run is stopped
    (Ctrl-C or the dashboard's Stop button); the main process reports it."""
    import os
    import signal

    signal.signal(signal.SIGINT, lambda *_: os._exit(0))


def _fetch(job):
    url, f, half = job
    try:
        return remote.read_cutout(url, f, half)
    except Exception as e:  # reported in the bundle, not hidden
        return {"error": f"{type(e).__name__}: {e}", "url": url}


def _wide(job):
    return remote.read_wide(*job)


def resolve_scans(case, con):
    if "scans" in case:
        return [dict(s, pos=i) for i, s in enumerate(case["scans"])], None
    cad = db.cadence_for(con, case["target"], case["mjd"])
    if cad is None:
        raise SystemExit(
            f"no six-scan cadence found for {case['target']} near {case['mjd']}"
        )
    scans = [
        {
            "pos": s["pos"],
            "role": "ON" if s["pos"] in ON_POS else "OFF",
            "source": s["source"],
            "file_id": s["file_id"],
            "name": s["name"],
            "url": remote.url_for_scan(s["name"], s["source"]),
        }
        for s in cad
    ]
    return scans, cad


def _max_drift(cut, half_khz):
    scan_len = cut["data"].shape[0] * cut["tsamp"]
    return min(4.0, 0.4 * half_khz * 1e3 / scan_len)


def _freq(cut, chan):
    return cut["fch1"] + chan * cut["foff"]


def _brightest(cu, f_pred):
    i, k, z = dd.brightest_sample(cu["data"])
    return {
        "noise_units": round(z, 1),
        "at_time_s": round(i * cu["tsamp"], 1),
        "offset_from_prediction_hz": round((_freq(cu, k) - f_pred) * 1e6, 1),
    }


# How far from the catalogue frequency a signal may start and still be the
# catalogued hit, before the drift over one time sample is added. Where the
# true position is known, in the blind controls, the seeds found with a far
# wider window sat this close (see README); 2026-11-21.
SEED_HZ = 50.0


def _seed_half_hz(d0, tsamp):
    """Half width of the place the catalogued hit must start in: SEED_HZ plus
    what the catalogue drift moves a tone during one time sample, since a
    fast tone is smeared that far inside its first sample."""
    return SEED_HZ + abs(d0) * tsamp


def _near_hz(d0):
    """How far around the catalogue frequency the search collects signals of
    the catalogue drift. Wider than the seed's own place, so that a strong
    line beside the candidate is seen and its crossings are not taken for
    the candidate."""
    return 100 + 300 * abs(d0)


def _drift_half(d0):
    """How far from the catalogue drift, at either sign, a seed's may be."""
    return 0.05 + 0.3 * abs(d0)


FLASH_SHARE = 0.5  # more than this share of a track's power in one sample
STEADY_SHARE = 0.5  # a steady line this strong next to a flash is taken
STEADY_LOOK = 5  # candidates per scan looked at


def _share(c, h):
    p = dd.time_profile(c["data"], c["foff"] * 1e6, c["tsamp"], h["chan"], h["drift"])
    return p["largest_single_sample_share"] if p else 1.0


def _steadier(found, cuts, best):
    """When the strongest candidate is a flash, the strongest steady one with
    at least STEADY_SHARE of its S/N, in any ON scan. A flash that happens to
    fall in the seed's window would otherwise be followed instead of the tone
    there, and the case closed as a burst. Returns {i, h, flash} or None."""
    c0 = next(cuts[i] for _, i, cp in found if cp[0] is best)
    s0 = _share(c0, best)
    if s0 <= FLASH_SHARE:
        return None
    pick = None
    for _, i, cp in found:
        for h in cp[:STEADY_LOOK]:
            if h is best or h["snr"] < STEADY_SHARE * best["snr"]:
                continue
            if pick and h["snr"] <= pick["h"]["snr"]:
                continue
            if _share(cuts[i], h) <= FLASH_SHARE:
                pick = {"i": i, "h": h}
    if pick:
        pick["flash"] = {
            "snr": round(best["snr"], 1),
            "largest_single_sample_share": s0,
            "freq_mhz": round(_freq(c0, best["chan"]), 6),
            "drift": round(best["drift"], 4),
        }
    return pick


def pick_seed(case, scans, cuts, half_khz):
    """Find the candidate in the data. Catalogues differ in conventions, so
    accept the strongest signal whose drift magnitude matches the catalogue,
    at either sign: the survey's hit tables give the drift with the opposite
    sign to the data (tools/drift_sign.py). With freq_exact the seed is the
    signal that starts at the catalogue frequency (see _seed_half_hz), not
    the strongest one in the neighbourhood: a line hundreds of hertz away is
    another line. In an ON scan other than the catalogue's, that place is
    where the hit's own drift would have carried it. Every ON scan is
    searched and the strongest hit kept."""
    f0, d0 = case["freq_mhz"], case.get("drift", 0.0)
    exact = case.get("freq_exact", False)
    ok = [i for i, c in enumerate(cuts) if "error" not in c]
    if not ok:
        raise SystemExit(
            "no scan could be read: " + "; ".join(c["error"] for c in cuts)
        )
    t_ref = case.get("mjd", cuts[ok[0]]["tstart"])
    order = (
        sorted(
            (i for i in ok if scans[i]["role"] == "ON"),
            key=lambda i: abs(cuts[i]["tstart"] - t_ref),
        )
        or ok
    )
    by_time = order[0]
    found = []
    for i in order:
        c = cuts[i]
        fo = c["foff"] * 1e6
        if exact:
            # look only where the catalogue says, so a crowded cutout cannot
            # push the candidate out of the list of strongest hits
            hits = []
            for sign in (1, -1) if d0 else (1,):
                hits += dd.search(
                    c["data"],
                    fo,
                    c["tsamp"],
                    snr_min=6,
                    top=200,
                    drift_center=sign * d0,
                    drift_half=_drift_half(d0),
                )
        else:
            hits = dd.search(
                c["data"],
                fo,
                c["tsamp"],
                max_drift=_max_drift(c, half_khz),
                snr_min=6,
                top=30,
            )
        # The catalogue frequency holds in the catalogue's own scan. In another
        # scan a tone has drifted on, so a hit is placed against where its own
        # drift would have carried it from there.
        dt = (c["tstart"] - cuts[by_time]["tstart"]) * 86400.0
        comp = []
        for h in hits:
            off = (_freq(c, h["chan"]) - f0) * 1e6
            place = off - h["drift"] * dt if exact else off
            if abs(abs(h["drift"]) - abs(d0)) > _drift_half(d0):
                continue
            if exact and abs(place) > _near_hz(d0):
                continue
            comp.append({**h, "offset_hz": off, "from_place_hz": place})
        # a line also shows, weaker, where a path of the other sign crosses it;
        # that is the same signal, not a second one
        reach = abs(d0) * c["data"].shape[0] * c["tsamp"] + 30.0
        kept = []
        for h in sorted(comp, key=lambda h: -h["snr"]):
            if not any(
                abs(h["offset_hz"] - g["offset_hz"]) <= reach
                and (h["drift"] * g["drift"] < 0 or h["chan"] == g["chan"])
                for g in kept
            ):
                kept.append(h)
        comp = kept
        if exact:
            # two steps of the drift grid, over the time since the catalogue's
            # scan, for what the hit's measured drift leaves uncertain
            slack = 2 * dd.drift_step(fo, c["tsamp"], c["data"].shape[0]) * abs(dt)
            comp = [
                h
                for h in comp
                if abs(h["from_place_hz"]) <= _seed_half_hz(d0, c["tsamp"]) + slack
            ]
        if comp and exact:
            # with freq_exact every ON scan is looked at and the strongest hit
            # kept: in a crowded field the first scan can hold a weak
            # unrelated hit where the candidate itself is absent
            found.append((comp[0]["snr"], i, comp))
            continue
        if comp:
            best = comp[0]
            return i, {
                "seed_pos": scans[i]["pos"],
                "recovered": True,
                "seed_is_scan_nearest_catalogue_time": i == by_time,
                "snr": round(best["snr"], 1),
                "freq_mhz": round(_freq(c, best["chan"]), 6),
                "drift": round(best["drift"], 4),
                "catalogue_freq_mhz": f0,
                "catalogue_drift": d0,
                "offset_from_catalogue_hz": round(best["from_place_hz"], 1),
                "drift_sign_differs_from_catalogue": bool(best["drift"] * d0 < 0),
                "compatible_signals_in_seed_scan": len(comp),
                "other_compatible": [
                    {
                        "offset_hz": round(h["offset_hz"], 1),
                        "drift": round(h["drift"], 4),
                        "snr": round(h["snr"], 1),
                    }
                    for h in comp[1:4]
                ],
            }
    if found:
        _, i, comp = max(found, key=lambda x: x[0])
        c, best = cuts[i], comp[0]
        passed = _steadier(found, cuts, best)
        if passed:
            i, best = passed["i"], passed["h"]
            c = cuts[i]
            comp = [best] + [
                h for h in next(cp for _, j, cp in found if j == i) if h is not best
            ]
        return i, {
            "seed_pos": scans[i]["pos"],
            "recovered": True,
            "seed_is_scan_nearest_catalogue_time": i == by_time,
            "snr": round(best["snr"], 1),
            "freq_mhz": round(_freq(c, best["chan"]), 6),
            "drift": round(best["drift"], 4),
            "catalogue_freq_mhz": f0,
            "catalogue_drift": d0,
            "offset_from_catalogue_hz": round(best["from_place_hz"], 1),
            "drift_sign_differs_from_catalogue": bool(best["drift"] * d0 < 0),
            "compatible_signals_in_seed_scan": len(comp),
            "on_scans_with_a_hit": len(found),
            **({"flash_passed_over": passed["flash"]} if passed else {}),
            "other_compatible": [
                {
                    "offset_hz": round(h["offset_hz"], 1),
                    "drift": round(h["drift"], 4),
                    "snr": round(h["snr"], 1),
                }
                for h in comp[1:4]
            ],
        }
    if exact:
        # The blind search can miss a catalogue hit whose drift is ill-defined
        # (a short burst fits many drifts). Measure at the catalogue position
        # itself and accept it if it stands clear of the control windows.
        c = cuts[by_time]
        fo = c["foff"] * 1e6
        k = (f0 - c["fch1"]) / c["foff"]
        ctol = _seed_half_hz(d0, c["tsamp"]) / abs(fo)
        width = 2 * int(np.ceil(ctol)) + 3
        ctrl = [k + m * width for m in range(-25, 26) if abs(m) >= 3]
        tr, cmax = None, float("inf")
        for sign in (1, -1) if d0 else (1,):
            res = dd.track_multi(
                c["data"],
                fo,
                c["tsamp"],
                [k] + ctrl,
                sign * d0,
                chan_tol=ctol,
                drift_tol=0.05 + 0.3 * abs(d0),
            )
            top = max(
                (r["snr"] for r in res[1:] if r["in_cutout"]), default=float("inf")
            )
            r0 = res[0]
            if r0["in_cutout"] and r0["snr"] >= 6 and r0["snr"] > top:
                if tr is None or r0["snr"] > tr["snr"]:
                    tr, cmax = r0, top
        if tr is not None:
            f_found = _freq(c, tr["chan"])
            return by_time, {
                "seed_pos": scans[by_time]["pos"],
                "recovered": True,
                "method": "measured at the catalogue position; the blind search found no "
                "hit with a matching drift there, so the drift is poorly defined",
                "seed_is_scan_nearest_catalogue_time": True,
                "snr": round(tr["snr"], 1),
                "freq_mhz": round(f_found, 6),
                "drift": round(tr["drift"], 4),
                "catalogue_freq_mhz": f0,
                "catalogue_drift": d0,
                "offset_from_catalogue_hz": round((f_found - f0) * 1e6, 1),
                "drift_sign_differs_from_catalogue": bool(tr["drift"] * d0 < 0),
                "controls_max_snr": round(cmax, 1),
            }
    c = cuts[by_time]
    return by_time, {
        "seed_pos": scans[by_time]["pos"],
        "recovered": False,
        "snr": None,
        "freq_mhz": f0,
        "drift": d0,
        "catalogue_freq_mhz": f0,
        "catalogue_drift": d0,
        "note": "no signal with the catalogue drift magnitude at S/N >= 6 in any ON "
        "cutout; tracks below are measured at the catalogue values",
    }


def without_peak(cu, ks, drift, ctol, dtol, tr, ctrl_max):
    """What is left of a track, and of its control windows, when its strongest
    time sample is taken out. A steady tone keeps nearly all of its S/N; a hit
    that was one flash keeps nothing above its controls. ks: the track's
    predicted channel followed by the control channels; tr and ctrl_max: the
    track and the control maximum measured on the whole scan."""
    fo = cu["foff"] * 1e6
    if not tr["snr"] or tr["snr"] <= 0:
        return None
    tp = dd.time_profile(cu["data"], fo, cu["tsamp"], tr["chan"], tr["drift"])
    if tp is None:
        return None
    i = int(round(tp["at_time_s"] / cu["tsamp"]))
    res = dd.track_multi(
        dd.without_sample(cu["data"], i),
        fo,
        cu["tsamp"],
        ks,
        drift,
        chan_tol=ctol,
        drift_tol=dtol,
    )
    ctrl = [r["snr"] for r in res[1:] if r["in_cutout"]]
    if res[0]["snr"] is None or not ctrl:
        return None
    return {
        "sample_left_out_at_s": tp["at_time_s"],
        "full_snr": round(tr["snr"], 2),
        "full_control_max_snr": round(ctrl_max, 2),
        "snr": round(res[0]["snr"], 2),
        "control_max_snr": round(max(ctrl), 2),
        "share_of_full_snr": round(res[0]["snr"] / tr["snr"], 2),
    }


def backfill_without_peak(case, bundle):
    """without_peak for the seed scan of a stored bundle, measured again from
    the cutouts in the cache. Returns None when a cutout it needs is not in
    the cache (nothing is downloaded) or the scan cannot be measured."""
    half = case.get("half_khz", max(5.0, abs(case.get("drift", 0.0)) * 2.5 + 3.0))
    rows = bundle["scans"]
    seed = next(i for i, s in enumerate(rows) if s["pos"] == bundle["seed"]["seed_pos"])
    need = range(len(rows)) if "inject" in case else [seed]
    scans, cuts = [], []
    for i, s in enumerate(rows):
        scans.append({"pos": s["pos"], "role": s["role"]})
        if i not in need:
            cuts.append({"error": "not read"})
            continue
        if "error" in s or not s.get("url"):
            return None
        if not remote._cache_path(s["url"], case["freq_mhz"], half).exists():
            return None
        cuts.append(dict(remote.read_cutout(s["url"], case["freq_mhz"], half)))
    if "inject" in case:
        apply_injection(case, scans, cuts)
    cu = cuts[seed]
    f, d = bundle["seed"]["freq_mhz"], bundle["seed"]["drift"]
    fo = cu["foff"] * 1e6
    k = (f - cu["fch1"]) / cu["foff"]
    ctol = max(3, tol_hz(0.0, d) / abs(fo))
    dtol = 0.05 + 0.25 * abs(d)
    width = 2 * int(np.ceil(ctol)) + 3
    ks = [k] + [k + m * width for m in range(-25, 26) if abs(m) >= 3]
    res = dd.track_multi(
        cu["data"], fo, cu["tsamp"], ks, d, chan_tol=ctol, drift_tol=dtol
    )
    ctrl = [r["snr"] for r in res[1:] if r["in_cutout"]]
    if res[0]["snr"] is None or res[0]["chan"] is None or not ctrl:
        return None
    return without_peak(cu, ks, d, ctol, dtol, res[0], max(ctrl))


def _cached_cuts(case, bundle):
    """The scans of a stored bundle with their cutouts read from the cache
    (and the injection of a control added again). None when a cutout that
    was read before is no longer there; nothing is downloaded."""
    half = case.get("half_khz", max(5.0, abs(case.get("drift", 0.0)) * 2.5 + 3.0))
    scans, cuts = [], []
    for s in bundle["scans"]:
        scans.append({k: s.get(k) for k in ("pos", "role", "source", "url")})
        if "error" in s or not s.get("url"):
            cuts.append({"error": s.get("error", "not read"), "url": s.get("url")})
            continue
        if not remote._cache_path(s["url"], case["freq_mhz"], half).exists():
            return None
        cuts.append(dict(remote.read_cutout(s["url"], case["freq_mhz"], half)))
    if "inject" in case:
        apply_injection(case, scans, cuts)
    return scans, cuts, half


def replot(case, bundle):
    """Draw the two plots of a stored bundle again, from the cache, with the
    track of its present seed. False when a cutout is no longer in the cache."""
    got = _cached_cuts(case, bundle)
    if got is None:
        return False
    scans, cuts, _ = got
    seed = next(
        i
        for i, s in enumerate(bundle["scans"])
        if s["pos"] == bundle["seed"]["seed_pos"]
    )
    out = remote.ROOT / "cases" / "evidence" / case["id"]
    _plots(case, scans, cuts, bundle["seed"], seed, out)
    return True


def _plots(case, scans, cuts, info, seed, out):
    plot(case, scans, cuts, info, seed, out / "waterfall.png")
    # the same plot without names, for blinded readers
    n_off, blind_cuts = 0, []
    for s_, cu in zip(scans, cuts):
        if "error" in cu:
            blind_cuts.append(cu)
            continue
        n_off += s_["role"] == "OFF"
        blind_cuts.append(
            {
                **cu,
                "source_name": "target" if s_["role"] == "ON" else f"pointing {n_off}",
            }
        )
    plot(
        {"id": code_for(case["id"]), "target": ""},
        scans,
        blind_cuts,
        info,
        seed,
        out / "waterfall_blind.png",
    )


def remeasure(case, bundle, con=None):
    """The measured part of a stored bundle, made again from the cutouts in
    the cache: the seed, the six scans, the off-scan stack and, with a hit
    table connection, the catalogue lookalikes. Nothing is downloaded: returns
    None when a cutout that was read before is no longer in the cache. Parts
    that need wider data than a cutout (same_drift_twins, clock_probes) are
    kept, and marked stale when the seed moved to another track."""
    got = _cached_cuts(case, bundle)
    if got is None:
        return None
    scans, cuts, half = got
    seed, info = pick_seed(case, scans, cuts, half)
    old = bundle["seed"]
    out = {k: v for k, v in bundle.items() if k != "family"}
    out["seed"], out["scans"] = info, measure(scans, cuts, seed, info, half)
    c, f, d = cuts[seed], info["freq_mhz"], info["drift"]
    out["harmonics_within_5khz"] = [h for h in checks.harmonics(f) if h["within_tol"]]
    out["allocation"] = checks.allocation(f)
    site = bundle.get("site", "GBT")
    if c.get("ra_hours") is not None:
        out["geometry"] = checks.geometry(
            c["ra_hours"], c["dec_deg"], c["tstart"], site
        )
        out["expected_drift_barycentric_rest"] = checks.expected_drift(
            c["ra_hours"], c["dec_deg"], c["tstart"], site, f
        )
    if con is not None and "lookalikes_1khz" in bundle:
        out["lookalikes_1khz"] = checks.lookalikes(
            con, f, d, case["target"], c["tstart"]
        )
        wide = checks.lookalikes(
            con, f, d, case["target"], c["tstart"], window_hz=10000.0, top=0
        )
        out["lookalikes_10khz"] = {
            k: v for k, v in wide.items() if k != "strongest_other"
        }
    out.pop("off_scans_stacked", None)
    if info["recovered"]:
        out["off_scans_stacked"] = off_stack(scans, cuts, seed, info)
        out["followed_line"] = follow_line(scans, cuts, seed, info)
    moved = (
        not old.get("recovered")
        or not info["recovered"]
        or abs(old["drift"] - d) > 0.05 + 0.25 * abs(d)
        or abs(old["freq_mhz"] - f) * 1e6 > 100
    )
    out["remeasured"] = {
        "seed_moved": bool(moved),
        "earlier_seed": {
            k: old.get(k) for k in ("seed_pos", "recovered", "snr", "freq_mhz", "drift")
        },
    }
    if moved:
        for key in ("same_drift_twins", "clock_probes"):
            if isinstance(out.get(key), dict):
                out[key] = {
                    **out[key],
                    "stale": "measured for an earlier seed on another track",
                }
    return out


def _track_and_controls(cu, k_pred, dt, drift):
    """The forced track at a predicted channel of one cutout, and the same
    measurement in the control windows: places to either side where nothing
    is predicted. Returns the track, the S/N of the control windows inside
    the cutout, every channel measured, the two tolerances and the spacing of
    the windows in channels."""
    fo = cu["foff"] * 1e6
    ctol = max(3, tol_hz(dt, drift) / abs(fo))
    dtol = 0.05 + 0.25 * abs(drift)
    width = 2 * int(np.ceil(ctol)) + 3
    ks = [k_pred] + [k_pred + m * width for m in range(-25, 26) if abs(m) >= 3]
    res = dd.track_multi(
        cu["data"], fo, cu["tsamp"], ks, drift, chan_tol=ctol, drift_tol=dtol
    )
    ctrl = np.array([r["snr"] for r in res[1:] if r["in_cutout"]])
    return res[0], ctrl, ks, ctol, dtol, width


def _control_summary(tr, ctrl, window_hz):
    """What a scan's row keeps of its control windows. at_least_track is how
    many of them hold a track at least as strong as the one at the predicted
    place: where the windows hold lines of their own, the strongest of them
    is no yardstick, and the crowded part of rule D1 reads this instead."""
    if len(ctrl) == 0:
        return None
    out = {
        "n": int(len(ctrl)),
        "median_snr": round(float(np.median(ctrl)), 1),
        "p95_snr": round(float(np.percentile(ctrl, 95)), 1),
        "max_snr": round(float(ctrl.max()), 1),
        "window_hz": round(window_hz, 1),
    }
    if tr["snr"] is not None:
        out["at_least_track"] = int((ctrl >= tr["snr"]).sum())
    return out


def control_ranks(case, bundle):
    """at_least_track and window_hz (see _control_summary) for every scan of
    a stored bundle, measured again along its seed from the cutouts in the
    cache: {pos: {...}}. Each entry also holds the track's S/N as measured
    here, to compare with the stored one. None when a cutout that was read
    before is no longer in the cache; nothing is downloaded."""
    got = _cached_cuts(case, bundle)
    if got is None:
        return None
    scans, cuts, _ = got
    seed = next(
        i for i, s in enumerate(scans) if s["pos"] == bundle["seed"]["seed_pos"]
    )
    f, d = bundle["seed"]["freq_mhz"], bundle["seed"]["drift"]
    c = cuts[seed]
    out = {}
    for s, cu in zip(scans, cuts):
        if "error" in cu:
            continue
        dt = (cu["tstart"] - c["tstart"]) * 86400.0
        k_pred = (f + d * dt * 1e-6 - cu["fch1"]) / cu["foff"]
        tr, ctrl, _, _, _, width = _track_and_controls(cu, k_pred, dt, d)
        cs = _control_summary(tr, ctrl, width * abs(cu["foff"] * 1e6))
        if cs and "at_least_track" in cs:
            out[s["pos"]] = {
                "at_least_track": cs["at_least_track"],
                "window_hz": cs["window_hz"],
                "track_snr": round(tr["snr"], 1),
            }
    return out


def measure(scans, cuts, seed, info, half_khz):
    c = cuts[seed]
    f_seed, d_seed = info["freq_mhz"], info["drift"]
    rows = []
    for s, cu in zip(scans, cuts):
        row = {
            "pos": s["pos"],
            "role": s["role"],
            "source": s.get("source"),
            "url": cu.get("url", s.get("url")),
        }
        if "error" in cu:
            row["error"] = cu["error"]
            rows.append(row)
            continue
        fo = cu["foff"] * 1e6
        dt = (cu["tstart"] - c["tstart"]) * 86400.0
        f_pred = f_seed + d_seed * dt * 1e-6
        k_pred = (f_pred - cu["fch1"]) / cu["foff"]
        tr, ctrl, ks, ctol, dtol, width = _track_and_controls(cu, k_pred, dt, d_seed)
        blind = dd.search(
            cu["data"],
            fo,
            cu["tsamp"],
            max_drift=_max_drift(cu, half_khz),
            snr_min=8,
            top=6,
        )
        row.update(
            {
                "source_in_file": cu["source_name"],
                "mjd": cu["tstart"],
                "dt_s": round(dt, 1),
                "n_time": int(cu["data"].shape[0]),
                "tsamp_s": round(cu["tsamp"], 3),
                "chan_hz": round(abs(fo), 4),
                "predicted_freq_mhz": round(f_pred, 6),
                "track": {
                    "in_cutout": tr["in_cutout"],
                    "snr": None if tr["snr"] is None else round(tr["snr"], 1),
                    "freq_mhz": None
                    if tr["chan"] is None
                    else round(_freq(cu, tr["chan"]), 6),
                    "offset_from_prediction_hz": None
                    if tr["chan"] is None
                    else round((_freq(cu, tr["chan"]) - f_pred) * 1e6, 1),
                    "drift": None if tr["drift"] is None else round(tr["drift"], 4),
                    "drift_range_95pct": None
                    if tr["drift"] is None
                    else [round(tr["drift_lo"], 4), round(tr["drift_hi"], 4)],
                    "search_window_hz": round(
                        tr.get("window_channels", 0) * abs(fo), 1
                    ),
                },
                "track_time_profile": None
                if tr["chan"] is None
                else dd.time_profile(
                    cu["data"], fo, cu["tsamp"], tr["chan"], tr["drift"]
                ),
                "brightest_sample_in_cutout": _brightest(cu, f_pred),
                "control_windows": _control_summary(tr, ctrl, width * abs(fo)),
                "track_exceeds_all_controls": bool(
                    tr["snr"] is not None and len(ctrl) > 0 and tr["snr"] > ctrl.max()
                ),
                "strongest_in_cutout": [
                    {
                        "freq_mhz": round(_freq(cu, h["chan"]), 6),
                        "offset_from_prediction_hz": round(
                            (_freq(cu, h["chan"]) - f_pred) * 1e6, 1
                        ),
                        "drift": round(h["drift"], 4),
                        "snr": round(h["snr"], 1),
                    }
                    for h in blind
                ],
                "net_mb": round(cu.get("net_bytes", 0) / 1e6, 1),
            }
        )
        if s is scans[seed] and tr["chan"] is not None and len(ctrl):
            row["track_without_strongest_sample"] = without_peak(
                cu, ks, d_seed, ctol, dtol, tr, float(ctrl.max())
            )
        rows.append(row)
    return rows


FOLLOW_MAX_DRIFT = 4.0  # Hz/s: what the survey's own search allowed
FOLLOW_SNR_MIN = 10.0


def follow_line(scans, cuts, seed, info):
    """Follow the seed line itself from scan to scan, in time order, both
    ways from the seed scan. In the next scan the strongest line is taken
    whose start lies within reach of where the last one ended: the gap
    between the scans times the last drift, give or take a quarter of it
    plus 0.1 Hz/s, and 50 Hz; at any drift up to FOLLOW_MAX_DRIFT and S/N
    FOLLOW_SNR_MIN or more. The chain stops at the first scan with nothing
    there. For a line found in an OFF scan, chance is how likely a line at
    least that strong is to start that close to the place, from the number
    of such lines in the whole cutout. Returns the chain and the smallest
    chance among its OFF scans, times the number of OFF scans it reached."""
    if not info.get("recovered"):
        return None
    order = sorted(
        (i for i, c in enumerate(cuts) if "error" not in c),
        key=lambda i: cuts[i]["tstart"],
    )
    if seed not in order:
        return None
    c0 = cuts[seed]
    start = info["freq_mhz"]
    dur0 = c0["data"].shape[0] * c0["tsamp"]
    first = {
        "pos": scans[seed]["pos"],
        "role": scans[seed]["role"],
        "freq_mhz": start,
        "drift": info["drift"],
        "snr": info.get("snr"),
        "seed": True,
    }
    chain = [first]
    k = order.index(seed)
    for step, idx in ((1, order[k + 1 :]), (-1, list(reversed(order[:k])))):
        last, last_c = first, c0
        for i in idx:
            c = cuts[i]
            fo = c["foff"] * 1e6
            dur_l = last_c["data"].shape[0] * last_c["tsamp"]
            dur = c["data"].shape[0] * c["tsamp"]
            if step == 1:
                gap = (
                    c["tstart"] - last_c["tstart"]
                ) * 86400.0  # last start -> this start
                d = last["drift"]
                place = last["freq_mhz"] + d * gap * 1e-6
                reach = abs(gap) * (0.25 * abs(d) + 0.1) + 50.0
            else:
                gap = (
                    last_c["tstart"] - c["tstart"]
                ) * 86400.0  # this start -> last start
                d = last["drift"]
                place = last["freq_mhz"] - d * gap * 1e-6
                reach = abs(gap) * (0.25 * abs(d) + 0.1) + 50.0
            hits = dd.search(
                c["data"],
                fo,
                c["tsamp"],
                max_drift=FOLLOW_MAX_DRIFT,
                snr_min=FOLLOW_SNR_MIN,
                top=200,
            )
            near = [h for h in hits if abs(_freq(c, h["chan"]) - place) * 1e6 <= reach]
            if not near:
                break
            h = max(near, key=lambda h: h["snr"])
            nf = c["data"].shape[1]
            width_hz = nf * abs(fo)
            as_strong = sum(1 for x in hits if x["snr"] >= h["snr"])
            chance = min(1.0, as_strong * 2 * reach / width_hz)
            last = {
                "pos": scans[i]["pos"],
                "role": scans[i]["role"],
                "freq_mhz": round(_freq(c, h["chan"]), 6),
                "drift": round(h["drift"], 4),
                "snr": round(h["snr"], 1),
                "offset_from_place_hz": round((_freq(c, h["chan"]) - place) * 1e6, 1),
                "reach_hz": round(reach, 1),
                "chance": round(chance, 4),
            }
            chain.append(last)
            last_c = c
    chain.sort(key=lambda x: x["pos"])
    offs = [x for x in chain if x["role"] == "OFF"]
    best = min((x["chance"] for x in offs), default=None)
    return {
        "chain": chain,
        "off_scans_reached": len(offs),
        "chance": None if best is None else round(min(1.0, best * len(offs)), 4),
    }


def off_stack(scans, cuts, seed, info):
    """Sum of the forced-track S/N over all OFF scans, against the same sum at
    control offsets. More sensitive than any single OFF scan to a signal that
    is weakly present in all of them."""
    c = cuts[seed]
    f, d = info["freq_mhz"], info["drift"]
    per = []
    for s_, cu in zip(scans, cuts):
        if s_["role"] != "OFF" or "error" in cu:
            continue
        fo = cu["foff"] * 1e6
        dt = (cu["tstart"] - c["tstart"]) * 86400.0
        k = ((f + d * dt * 1e-6) - cu["fch1"]) / cu["foff"]
        ctol = max(3, tol_hz(dt, d) / abs(fo))
        width = 2 * int(np.ceil(ctol)) + 3
        ms = [m for m in range(-25, 26) if abs(m) >= 3]
        res = dd.track_multi(
            cu["data"],
            fo,
            cu["tsamp"],
            [k] + [k + m * width for m in ms],
            d,
            chan_tol=ctol,
            drift_tol=0.05 + 0.25 * abs(d),
        )
        per.append((res[0], dict(zip(ms, res[1:]))))
    if len(per) < 2 or any(r[0]["snr"] is None for r in per):
        return None
    total = sum(r[0]["snr"] for r in per)
    common = [m for m in per[0][1] if all(q[1][m]["in_cutout"] for q in per)]
    if len(common) < 10:
        return None
    sums = np.array([sum(q[1][m]["snr"] for q in per) for m in common])
    return {
        "n_off_scans": len(per),
        "sum_track_snr": round(float(total), 1),
        "n_controls": int(len(sums)),
        "control_sum_median": round(float(np.median(sums)), 1),
        "control_sum_p95": round(float(np.percentile(sums, 95)), 1),
        "control_sum_max": round(float(sums.max()), 1),
        "margin_over_control_max": round(float(total - sums.max()), 1),
        "exceeds_all_controls": bool(total > sums.max()),
    }


def twins(scans, cuts, seed, info):
    """Other signals with the candidate's drift in a wide band around it, in
    the seed ON scan, and whether each also shows in the nearest OFF scan."""
    f, d = info["freq_mhz"], info["drift"]
    offs = [
        i for i, s in enumerate(scans) if s["role"] == "OFF" and "error" not in cuts[i]
    ]
    near_off = min(
        offs, key=lambda i: abs(cuts[i]["tstart"] - cuts[seed]["tstart"]), default=None
    )
    jobs = [(scans[seed]["url"], f)] + (
        [(scans[near_off]["url"], f)] if near_off is not None else []
    )
    wide = [remote.read_wide(*j) for j in jobs]
    w = wide[0]
    if "error" in w:
        return {"error": w["error"]}
    fo = w["foff"] * 1e6
    half = 0.02 + 0.1 * abs(d)
    hits = dd.search(
        w["data"], fo, w["tsamp"], snr_min=10, top=60, drift_center=d, drift_half=half
    )
    tw = [
        h for h in hits if abs((_freq(w, h["chan"]) - f) * 1e6) > max(50.0, 4 * abs(fo))
    ]
    out = {
        "band_searched_mhz": [
            round(min(_freq(w, 0), _freq(w, w["data"].shape[1] - 1)), 4),
            round(max(_freq(w, 0), _freq(w, w["data"].shape[1] - 1)), 4),
        ],
        "drift_window_hz_s": [round(d - half, 4), round(d + half, 4)],
        "drift_window_includes_zero": bool(abs(d) <= half),
        "list_capped_at": 60,
        "n_twins_on": len(tw),
        "off_scan_checked": None,
        "n_twins_also_in_off": None,
        "net_mb": 0.0,
    }
    out["net_mb"] = round(sum(x.get("net_bytes", 0) for x in wide) / 1e6, 1)
    snr_off = [None] * len(tw)
    if tw and len(wide) > 1 and "error" not in wide[1]:
        o = wide[1]
        fo2 = o["foff"] * 1e6
        dt = (o["tstart"] - w["tstart"]) * 86400.0
        ks = [
            ((_freq(w, h["chan"]) + h["drift"] * dt * 1e-6) - o["fch1"]) / o["foff"]
            for h in tw
        ]
        ctol = max(3, tol_hz(dt, d) / abs(fo2))
        width = 2 * int(np.ceil(ctol)) + 3
        rng = np.random.default_rng(0)
        ctrl_k = rng.uniform(o["data"].shape[1] * 0.05, o["data"].shape[1] * 0.95, 200)
        ctrl_k = [k for k in ctrl_k if all(abs(k - x) > 3 * width for x in ks)]
        res = dd.track_multi(
            o["data"],
            fo2,
            o["tsamp"],
            ks + ctrl_k,
            d,
            chan_tol=ctol,
            drift_tol=half + 0.03,
        )
        ctrl = np.array([r["snr"] for r in res[len(ks) :] if r["in_cutout"]])
        thr = float(np.percentile(ctrl, 99)) if len(ctrl) else float("inf")
        snr_off = [r["snr"] for r in res[: len(ks)]]
        out["off_scan_checked"] = scans[near_off]["source"]
        out["off_detection_threshold_snr"] = round(thr, 1)
        out["n_twins_also_in_off"] = int(
            sum(1 for s in snr_off if s is not None and s > thr)
        )
    out["twins"] = [
        {
            "offset_khz": round((_freq(w, h["chan"]) - f) * 1e3, 3),
            "drift": round(h["drift"], 4),
            "snr_on": round(h["snr"], 1),
            "snr_off": None if s is None else round(s, 1),
        }
        for h, s in sorted(zip(tw, snr_off), key=lambda p: -p[0]["snr"])
    ]
    return out


def clock_probes(scans, cuts, seed, info):
    """Filterbank files only (cheap exact reads): look for a signal with the
    candidate's drift at f +- n*clock for common clock frequencies, in the seed
    ON scan and the nearest OFF scan. The same search at random frequencies
    gives the base rate. If the candidate sits near a multiple of a clock, each
    found signal is also tested against the single-oscillator prediction: a
    mistuned oscillator puts every harmonic the same fraction off its nominal
    frequency and drifts every harmonic by the same fraction."""
    c = cuts[seed]
    if "band_mhz" not in c:
        return {
            "done": False,
            "reason": "only done for filterbank files; in HDF5 files "
            "each probe costs a full 60 MB chunk per scan",
        }
    f, d = info["freq_mhz"], info["drift"]
    offs = [
        i for i, s in enumerate(scans) if s["role"] == "OFF" and "error" not in cuts[i]
    ]
    near_off = min(
        offs, key=lambda i: abs(cuts[i]["tstart"] - c["tstart"]), default=None
    )
    half = 0.02 + 0.1 * abs(d)
    lo, hi = c["band_mhz"]
    flagged = [h for h in checks.harmonics(f) if h["within_tol"]]
    best = min(flagged, key=lambda h: h["p_chance"], default=None)
    ppm = (
        None
        if best is None
        else best["residual_khz"] * 1e3 / (best["multiple"] * best["clock_mhz"])
    )

    def probe(fp):
        on = _fetch((scans[seed]["url"], fp, 2.0))
        if "error" in on:
            return None
        fo = on["foff"] * 1e6
        hits = dd.search(
            on["data"],
            fo,
            on["tsamp"],
            snr_min=8,
            top=3,
            drift_center=d,
            drift_half=half,
        )
        if not hits:
            return {}
        h = hits[0]
        fh = _freq(on, h["chan"])
        row = {
            "found_freq_mhz": round(fh, 6),
            "offset_hz": round((fh - fp) * 1e6, 1),
            "drift": round(h["drift"], 4),
            "snr": round(h["snr"], 1),
            "off_snr": None,
            "chan_hz": abs(fo),
            "drift_step": dd.drift_step(fo, on["tsamp"], on["data"].shape[0]),
        }
        if near_off is not None:
            of = _fetch((scans[near_off]["url"], fp, 2.0))
            if "error" not in of:
                fo2 = of["foff"] * 1e6
                dt = (of["tstart"] - on["tstart"]) * 86400.0
                k = ((fh + h["drift"] * dt * 1e-6) - of["fch1"]) / of["foff"]
                r = dd.track(
                    of["data"],
                    fo2,
                    of["tsamp"],
                    k,
                    d,
                    chan_tol=max(3, tol_hz(dt, d) / abs(fo2)),
                    drift_tol=half + 0.03,
                )
                row["off_snr"] = None if r["snr"] is None else round(r["snr"], 1)
        return row

    targets = {}
    for clk in checks.CLOCKS_MHZ:
        for n in (-2, -1, 1, 2):
            fp = round(f + n * clk, 6)
            if lo + 0.01 < fp < hi - 0.01:
                targets.setdefault(fp, []).append(f"{n:+d} x {clk:g} MHz")
    found, n_done = [], 0
    for fp, labels in sorted(targets.items()):
        r = probe(fp)
        if r is None:
            continue
        n_done += 1
        if r:
            chan, step = r.pop("chan_hz"), r.pop("drift_step")
            row = {"probe_mhz": fp, "spacing": labels, **r}
            if ppm is not None:
                exp_off = (fp - f) * ppm  # Hz, since ppm * MHz = Hz
                exp_drift = d * r["found_freq_mhz"] / f
                row["expected_offset_if_one_oscillator_hz"] = round(exp_off, 1)
                row["matches_one_oscillator"] = bool(
                    abs(r["offset_hz"] - exp_off) <= 2 * chan
                    and abs(r["drift"] - exp_drift) <= 2 * step
                )
            found.append(row)
    rng = np.random.default_rng(1)
    ctrl_found = ctrl_done = 0
    while ctrl_done < n_done:
        fp = float(rng.uniform(lo + 0.01, hi - 0.01))
        if any(abs(fp - t) < 0.01 for t in targets):
            continue
        r = probe(round(fp, 6))
        if r is None:
            continue
        ctrl_done += 1
        ctrl_found += bool(r)
    return {
        "done": True,
        "drift_window_hz_s": [round(d - half, 4), round(d + half, 4)],
        "off_scan": None if near_off is None else scans[near_off]["source"],
        "n_probe_frequencies": n_done,
        "n_with_same_drift_signal": len(found),
        "control_random_frequencies": {
            "n": ctrl_done,
            "n_with_same_drift_signal": ctrl_found,
        },
        "candidate_clock": None
        if best is None
        else {
            "clock_mhz": best["clock_mhz"],
            "multiple": best["multiple"],
            "candidate_offset_ppm": round(ppm, 3),
        },
        "n_matching_one_oscillator": sum(
            1 for r in found if r.get("matches_one_oscillator")
        ),
        # chance that an unrelated same-drift signal in a probe cutout lands on the
        # one-oscillator prediction: (4 channels / 4 kHz cutout) x (4 drift steps / window)
        "p_chance_match_per_probe": round(
            (4 * c["foff"] * 1e6 / 4000.0).__abs__()
            * min(
                1.0,
                4
                * dd.drift_step(c["foff"] * 1e6, c["tsamp"], c["data"].shape[0])
                / (2 * half),
            ),
            5,
        ),
        "found": found,
    }


def plot(case, scans, cuts, info, seed, path):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    good = [(s, c) for s, c in zip(scans, cuts) if "error" not in c]
    h = 1.9 if len(good) <= 6 else 1.25
    fig, axes = plt.subplots(
        len(good), 1, figsize=(9, h * len(good) + 1), squeeze=False
    )
    f_seed, d_seed, t_seed = info["freq_mhz"], info["drift"], cuts[seed]["tstart"]
    for ax, (s, c) in zip(axes[:, 0], good):
        flat = dd.flatten(c["data"])
        med = np.median(flat)
        mad = 1.4826 * np.median(np.abs(flat - med))
        nt, nf = flat.shape
        x0 = (c["fch1"] - f_seed) * 1e3
        x1 = (_freq(c, nf - 1) - f_seed) * 1e3
        dur = nt * c["tsamp"]
        ax.imshow(
            flat,
            aspect="auto",
            origin="upper",
            cmap="viridis",
            vmin=med - 2 * mad,
            vmax=med + 8 * mad,
            extent=[x0, x1, dur, 0],
            interpolation="nearest",
        )
        dt = (c["tstart"] - t_seed) * 86400.0
        # mark where the track is predicted with triangles on the panel edges
        # only: a line drawn through the data gets read as a signal
        ax.plot([d_seed * dt * 1e-3], [0], marker="v", color="red", ms=6, clip_on=False)
        ax.plot(
            [d_seed * (dt + dur) * 1e-3],
            [dur],
            marker="^",
            color="red",
            ms=6,
            clip_on=False,
        )
        ax.set_xlim(-2.5, 2.5)
        ax.set_ylabel(f"{s['role']} {c['source_name']}\nt (s)", fontsize=7)
        ax.tick_params(labelsize=7)
    axes[-1, 0].set_xlabel(
        f"offset from {f_seed:.6f} MHz (kHz). Red triangles on panel edges = predicted "
        "track start/end (annotation, not data)",
        fontsize=8,
    )
    fig.suptitle(
        f"{case['id']}  {case['target']}  drift {d_seed:+.4f} Hz/s", fontsize=10
    )
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def code_for(case_id):
    """Opaque code shown to readers instead of the case id."""
    import hashlib

    return "case-" + hashlib.sha1(("coldcase:" + case_id).encode()).hexdigest()[:6]


def apply_injection(case, scans, cuts):
    """Tests only: add a synthetic narrowband drifting tone to the cutouts in
    memory. spec: f0_mhz (at the first scan's start), drift, snr (target
    per-scan S/N before drift smearing), present_on and scint (one factor per
    ON scan), off_fraction (amplitude in OFF scans relative to ON). Optional,
    for realism: flicker (fractional sample-to-sample amplitude scatter),
    curve (change of drift, Hz/s^2) and seed for the flicker."""
    spec = case["inject"]
    t0 = min(c["tstart"] for c in cuts if "error" not in c)
    flicker, curve = spec.get("flicker", 0.0), spec.get("curve", 0.0)
    rng = np.random.default_rng(spec.get("seed", 0))

    def freq(t):
        return spec["f0_mhz"] + (spec["drift"] * t + 0.5 * curve * t * t) * 1e-6

    on_i = 0
    for s_, cu in zip(scans, cuts):
        if s_["role"] == "ON":
            amp = spec["present_on"][on_i] * spec["scint"][on_i]
            on_i += 1
        else:
            amp = spec.get("off_fraction", 0.0)
        if "error" in cu:
            continue
        nt, nf = cu["data"].shape
        gains = np.clip(
            1 + flicker * rng.standard_normal(nt), 0.0, None
        )  # drawn for every scan
        if amp <= 0:
            continue
        data = cu["data"]
        med = np.median(data)
        mad = 1.4826 * np.median(np.abs(data - med))
        power = amp * spec["snr"] * mad / np.sqrt(nt)
        dt0 = (cu["tstart"] - t0) * 86400.0
        for i in range(nt):
            fa, fb = freq(dt0 + i * cu["tsamp"]), freq(dt0 + (i + 1) * cu["tsamp"])
            ka, kb = sorted(
                ((fa - cu["fch1"]) / cu["foff"], (fb - cu["fch1"]) / cu["foff"])
            )
            lo, hi = int(np.floor(ka)), int(np.floor(kb))
            if lo < 0 or hi >= nf:
                continue
            # share the row's power among the channels crossed during the row
            w = np.diff(np.clip(np.arange(lo, hi + 2), ka, kb))
            w = w / w.sum() if w.sum() > 0 else np.ones(hi - lo + 1) / (hi - lo + 1)
            data[i, lo : hi + 1] += power * gains[i] * w


def run(case, verbose=True):
    site = case.get("site", "GBT")
    con = db.connect() if case.get("use_hit_tables", True) else None
    scans, cad = resolve_scans(case, con)
    missing = [s["name"] for s in scans if not s.get("url")]
    half = case.get("half_khz", max(5.0, abs(case.get("drift", 0.0)) * 2.5 + 3.0))
    jobs = [(s["url"], case["freq_mhz"], half) for s in scans if s.get("url")]
    if not jobs:
        raise NoData(
            f"the public archive lists no file for any of the {len(scans)} scans"
        )
    with ProcessPoolExecutor(
        max_workers=min(6, len(jobs)), initializer=_quiet_worker
    ) as ex:
        got = iter(list(ex.map(_fetch, jobs)))
    cuts = [
        next(got) if s.get("url") else {"error": "no public file found", "url": None}
        for s in scans
    ]
    if "inject" in case:
        apply_injection(case, scans, cuts)
    seed, info = pick_seed(case, scans, cuts, half)
    rows = measure(scans, cuts, seed, info, half)
    c = cuts[seed]
    f, d = info["freq_mhz"], info["drift"]
    bundle = {
        "case": {k: v for k, v in case.items() if k != "scans"},
        "site": site,
        "seed": info,
        "scans": rows,
        "files_missing": missing,
        "harmonics_within_5khz": [h for h in checks.harmonics(f) if h["within_tol"]],
        "allocation": checks.allocation(f),
    }
    if c.get("ra_hours") is not None:
        bundle["pointing"] = {
            "ra_hours": round(c["ra_hours"], 5),
            "dec_deg": round(c["dec_deg"], 4),
            "note": c.get("dec_note"),
        }
        bundle["geometry"] = checks.geometry(
            c["ra_hours"], c["dec_deg"], c["tstart"], site
        )
        bundle["expected_drift_barycentric_rest"] = checks.expected_drift(
            c["ra_hours"], c["dec_deg"], c["tstart"], site, f
        )
    if con is not None:
        bundle["lookalikes_1khz"] = checks.lookalikes(
            con, f, d, case["target"], c["tstart"]
        )
        wide = checks.lookalikes(
            con, f, d, case["target"], c["tstart"], window_hz=10000.0, top=0
        )
        bundle["lookalikes_10khz"] = {
            k: v for k, v in wide.items() if k != "strongest_other"
        }
        if cad:
            bundle["neighbourhood"] = checks.neighbourhood(
                con, f, d, [s["file_id"] for s in cad]
            )
    if info["recovered"]:
        bundle["off_scans_stacked"] = off_stack(scans, cuts, seed, info)
        bundle["followed_line"] = follow_line(scans, cuts, seed, info)
    if info["recovered"] and case.get("twins", True):
        bundle["same_drift_twins"] = twins(scans, cuts, seed, info)
    if info["recovered"] and case.get("probes", True):
        bundle["clock_probes"] = clock_probes(scans, cuts, seed, info)
    out = remote.ROOT / "cases" / "evidence" / case["id"]
    out.mkdir(parents=True, exist_ok=True)
    _plots(case, scans, cuts, info, seed, out)
    (out / "evidence.json").write_text(json.dumps(bundle, indent=1))
    led = ledger.connect()
    ledger.add_case(led, {**case, "site": site})
    ledger.add_evidence(led, case["id"], out / "evidence.json", info)
    if verbose:
        print(
            f"{case['id']}: seed pos {info['seed_pos']} recovered={info['recovered']} "
            f"snr={info['snr']} f={f:.6f} d={d:+.4f} "
            f"offset={info.get('offset_from_catalogue_hz')}Hz"
        )
        for r in rows:
            if "error" in r:
                print(
                    f"  {r['pos']} {r['role']:3} {str(r['source']):12} ERROR {r['error']}"
                )
            else:
                cw = r["control_windows"] or {}
                print(
                    f"  {r['pos']} {r['role']:3} {str(r['source']):12} dt={r['dt_s']:>8} "
                    f"track_snr={r['track']['snr']} controls_max={cw.get('max_snr')} "
                    f"exceeds={r['track_exceeds_all_controls']}"
                )
        tw = bundle.get("same_drift_twins")
        if tw and "error" not in tw:
            print(
                f"  twins: {tw['n_twins_on']} in ON, {tw['n_twins_also_in_off']} also in OFF"
            )
        cp = bundle.get("clock_probes")
        if cp and cp.get("done"):
            cr = cp["control_random_frequencies"]
            print(
                f"  clock probes: {cp['n_with_same_drift_signal']} of "
                f"{cp['n_probe_frequencies']} show a same-drift signal; random controls "
                f"{cr['n_with_same_drift_signal']} of {cr['n']}; "
                f"{cp['n_matching_one_oscillator']} match one oscillator"
            )
    return bundle


def main():
    with open(sys.argv[1]) as fh:
        run(json.load(fh))


if __name__ == "__main__":
    main()
