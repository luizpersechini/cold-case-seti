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
    return {"noise_units": round(z, 1), "at_time_s": round(i * cu["tsamp"], 1),
            "offset_from_prediction_hz": round((_freq(cu, k) - f_pred) * 1e6, 1)}


def pick_seed(case, scans, cuts, half_khz):
    """Find the candidate in the data. Catalogues differ in conventions, so
    unless the case says freq_exact, accept the strongest signal in the cutout
    whose drift magnitude matches the catalogue (either sign)."""
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
    for i in order:
        c = cuts[i]
        fo = c["foff"] * 1e6
        if exact:
            # look only where the catalogue says, so a crowded cutout cannot
            # push the candidate out of the list of strongest hits
            hits = dd.search(c["data"], fo, c["tsamp"], snr_min=6, top=200,
                             drift_center=d0, drift_half=0.05 + 0.3 * abs(d0))
        else:
            hits = dd.search(c["data"], fo, c["tsamp"], max_drift=_max_drift(c, half_khz),
                             snr_min=6, top=30)
        comp = []
        for h in hits:
            off = (_freq(c, h["chan"]) - f0) * 1e6
            if abs(abs(h["drift"]) - abs(d0)) > 0.05 + 0.3 * abs(d0):
                continue
            if exact and (abs(off) > 100 + 300 * abs(d0) or h["drift"] * d0 < 0):
                continue
            comp.append({**h, "offset_hz": off})
        if comp:
            best = max(comp, key=lambda h: h["snr"])
            return i, {
                "seed_pos": scans[i]["pos"],
                "recovered": True,
                "seed_is_scan_nearest_catalogue_time": i == by_time,
                "snr": round(best["snr"], 1),
                "freq_mhz": round(_freq(c, best["chan"]), 6),
                "drift": round(best["drift"], 4),
                "catalogue_freq_mhz": f0,
                "catalogue_drift": d0,
                "offset_from_catalogue_hz": round(best["offset_hz"], 1),
                "drift_sign_differs_from_catalogue": bool(best["drift"] * d0 < 0),
                "compatible_signals_in_seed_scan": len(comp),
                "other_compatible": [
                    {
                        "offset_hz": round(h["offset_hz"], 1),
                        "drift": round(h["drift"], 4),
                        "snr": round(h["snr"], 1),
                    }
                    for h in sorted(comp, key=lambda h: -h["snr"])[1:4]
                ],
            }
    if exact:
        # The blind search can miss a catalogue hit whose drift is ill-defined
        # (a short burst fits many drifts). Measure at the catalogue position
        # itself and accept it if it stands clear of the control windows.
        c = cuts[by_time]
        fo = c["foff"] * 1e6
        k = (f0 - c["fch1"]) / c["foff"]
        ctol = (100 + 300 * abs(d0)) / abs(fo)
        width = 2 * int(np.ceil(ctol)) + 3
        ctrl = [k + m * width for m in range(-25, 26) if abs(m) >= 3]
        res = dd.track_multi(c["data"], fo, c["tsamp"], [k] + ctrl, d0, chan_tol=ctol,
                             drift_tol=0.05 + 0.3 * abs(d0))
        tr = res[0]
        cmax = max((r["snr"] for r in res[1:] if r["in_cutout"]), default=float("inf"))
        if tr["in_cutout"] and tr["snr"] >= 6 and tr["snr"] > cmax:
            f_found = _freq(c, tr["chan"])
            return by_time, {
                "seed_pos": scans[by_time]["pos"], "recovered": True,
                "method": "measured at the catalogue position; the blind search found no "
                          "hit with a matching drift there, so the drift is poorly defined",
                "seed_is_scan_nearest_catalogue_time": True,
                "snr": round(tr["snr"], 1), "freq_mhz": round(f_found, 6),
                "drift": round(tr["drift"], 4),
                "catalogue_freq_mhz": f0, "catalogue_drift": d0,
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
        ctol = max(3, tol_hz(dt, d_seed) / abs(fo))
        dtol = 0.05 + 0.25 * abs(d_seed)
        # control windows: same measurement at places where nothing is predicted
        width = 2 * int(np.ceil(ctol)) + 3
        ctrl_k = [k_pred + m * width for m in range(-25, 26) if abs(m) >= 3]
        res = dd.track_multi(
            cu["data"],
            fo,
            cu["tsamp"],
            [k_pred] + ctrl_k,
            d_seed,
            chan_tol=ctol,
            drift_tol=dtol,
        )
        tr = res[0]
        ctrl = np.array([r["snr"] for r in res[1:] if r["in_cutout"]])
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
                "track_time_profile": None if tr["chan"] is None
            else dd.time_profile(cu["data"], fo, cu["tsamp"], tr["chan"], tr["drift"]),
            "brightest_sample_in_cutout": _brightest(cu, f_pred),
            "control_windows": None
                if len(ctrl) == 0
                else {
                    "n": int(len(ctrl)),
                    "median_snr": round(float(np.median(ctrl)), 1),
                    "p95_snr": round(float(np.percentile(ctrl, 95)), 1),
                    "max_snr": round(float(ctrl.max()), 1),
                },
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
        rows.append(row)
    return rows


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
        res = dd.track_multi(cu["data"], fo, cu["tsamp"], [k] + [k + m * width for m in ms], d,
                             chan_tol=ctol, drift_tol=0.05 + 0.25 * abs(d))
        per.append((res[0], dict(zip(ms, res[1:]))))
    if len(per) < 2 or any(r[0]["snr"] is None for r in per):
        return None
    total = sum(r[0]["snr"] for r in per)
    common = [m for m in per[0][1] if all(q[1][m]["in_cutout"] for q in per)]
    if len(common) < 10:
        return None
    sums = np.array([sum(q[1][m]["snr"] for q in per) for m in common])
    return {
        "n_off_scans": len(per), "sum_track_snr": round(float(total), 1),
        "n_controls": int(len(sums)), "control_sum_median": round(float(np.median(sums)), 1),
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
        return {"done": False, "reason": "only done for filterbank files; in HDF5 files "
                "each probe costs a full 60 MB chunk per scan"}
    f, d = info["freq_mhz"], info["drift"]
    offs = [i for i, s in enumerate(scans) if s["role"] == "OFF" and "error" not in cuts[i]]
    near_off = min(offs, key=lambda i: abs(cuts[i]["tstart"] - c["tstart"]), default=None)
    half = 0.02 + 0.1 * abs(d)
    lo, hi = c["band_mhz"]
    flagged = [h for h in checks.harmonics(f) if h["within_tol"]]
    best = min(flagged, key=lambda h: h["p_chance"], default=None)
    ppm = None if best is None else best["residual_khz"] * 1e3 / (best["multiple"] * best["clock_mhz"])

    def probe(fp):
        on = _fetch((scans[seed]["url"], fp, 2.0))
        if "error" in on:
            return None
        fo = on["foff"] * 1e6
        hits = dd.search(on["data"], fo, on["tsamp"], snr_min=8, top=3,
                         drift_center=d, drift_half=half)
        if not hits:
            return {}
        h = hits[0]
        fh = _freq(on, h["chan"])
        row = {"found_freq_mhz": round(fh, 6), "offset_hz": round((fh - fp) * 1e6, 1),
               "drift": round(h["drift"], 4), "snr": round(h["snr"], 1), "off_snr": None,
               "chan_hz": abs(fo),
               "drift_step": dd.drift_step(fo, on["tsamp"], on["data"].shape[0])}
        if near_off is not None:
            of = _fetch((scans[near_off]["url"], fp, 2.0))
            if "error" not in of:
                fo2 = of["foff"] * 1e6
                dt = (of["tstart"] - on["tstart"]) * 86400.0
                k = ((fh + h["drift"] * dt * 1e-6) - of["fch1"]) / of["foff"]
                r = dd.track(of["data"], fo2, of["tsamp"], k, d,
                             chan_tol=max(3, tol_hz(dt, d) / abs(fo2)), drift_tol=half + 0.03)
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
                    and abs(r["drift"] - exp_drift) <= 2 * step)
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
        "done": True, "drift_window_hz_s": [round(d - half, 4), round(d + half, 4)],
        "off_scan": None if near_off is None else scans[near_off]["source"],
        "n_probe_frequencies": n_done, "n_with_same_drift_signal": len(found),
        "control_random_frequencies": {"n": ctrl_done, "n_with_same_drift_signal": ctrl_found},
        "candidate_clock": None if best is None else {
            "clock_mhz": best["clock_mhz"], "multiple": best["multiple"],
            "candidate_offset_ppm": round(ppm, 3)},
        "n_matching_one_oscillator": sum(1 for r in found if r.get("matches_one_oscillator")),
        # chance that an unrelated same-drift signal in a probe cutout lands on the
        # one-oscillator prediction: (4 channels / 4 kHz cutout) x (4 drift steps / window)
        "p_chance_match_per_probe": round(
            (4 * c["foff"] * 1e6 / 4000.0).__abs__()
            * min(1.0, 4 * dd.drift_step(c["foff"] * 1e6, c["tsamp"], c["data"].shape[0])
                  / (2 * half)), 5),
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
        ax.plot([d_seed * (dt + dur) * 1e-3], [dur], marker="^", color="red", ms=6,
                clip_on=False)
        ax.set_xlim(-2.5, 2.5)
        ax.set_ylabel(f"{s['role']} {c['source_name']}\nt (s)", fontsize=7)
        ax.tick_params(labelsize=7)
    axes[-1, 0].set_xlabel(
        f"offset from {f_seed:.6f} MHz (kHz). Red triangles on panel edges = predicted "
        "track start/end (annotation, not data)", fontsize=8)
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
        gains = np.clip(1 + flicker * rng.standard_normal(nt), 0.0, None)  # drawn for every scan
        if amp <= 0:
            continue
        data = cu["data"]
        med = np.median(data)
        mad = 1.4826 * np.median(np.abs(data - med))
        power = amp * spec["snr"] * mad / np.sqrt(nt)
        dt0 = (cu["tstart"] - t0) * 86400.0
        for i in range(nt):
            fa, fb = freq(dt0 + i * cu["tsamp"]), freq(dt0 + (i + 1) * cu["tsamp"])
            ka, kb = sorted(((fa - cu["fch1"]) / cu["foff"], (fb - cu["fch1"]) / cu["foff"]))
            lo, hi = int(np.floor(ka)), int(np.floor(kb))
            if lo < 0 or hi >= nf:
                continue
            # share the row's power among the channels crossed during the row
            w = np.diff(np.clip(np.arange(lo, hi + 2), ka, kb))
            w = w / w.sum() if w.sum() > 0 else np.ones(hi - lo + 1) / (hi - lo + 1)
            data[i, lo:hi + 1] += power * gains[i] * w


def run(case, verbose=True):
    site = case.get("site", "GBT")
    con = db.connect() if case.get("use_hit_tables", True) else None
    scans, cad = resolve_scans(case, con)
    missing = [s["name"] for s in scans if not s.get("url")]
    half = case.get("half_khz", max(5.0, abs(case.get("drift", 0.0)) * 2.5 + 3.0))
    jobs = [(s["url"], case["freq_mhz"], half) for s in scans if s.get("url")]
    with ProcessPoolExecutor(max_workers=min(6, len(jobs)), initializer=_quiet_worker) as ex:
        got = iter(list(ex.map(_fetch, jobs)))
    cuts = [next(got) if s.get("url") else {"error": "no public file found", "url": None}
            for s in scans]
    if "inject" in case:
        apply_injection(case, scans, cuts)
    seed, info = pick_seed(case, scans, cuts, half)
    rows = measure(scans, cuts, seed, info, half)
    c = cuts[seed]
    f, d = info["freq_mhz"], info["drift"]
    bundle = {
        "case": {k: v for k, v in case.items() if k != "scans"},
        "site": site, "seed": info, "scans": rows, "files_missing": missing,
        "harmonics_within_5khz": [h for h in checks.harmonics(f) if h["within_tol"]],
        "allocation": checks.allocation(f),
    }
    if c.get("ra_hours") is not None:
        bundle["pointing"] = {"ra_hours": round(c["ra_hours"], 5),
                              "dec_deg": round(c["dec_deg"], 4), "note": c.get("dec_note")}
        bundle["geometry"] = checks.geometry(c["ra_hours"], c["dec_deg"], c["tstart"], site)
        bundle["expected_drift_barycentric_rest"] = checks.expected_drift(
            c["ra_hours"], c["dec_deg"], c["tstart"], site, f)
    if con is not None:
        bundle["lookalikes_1khz"] = checks.lookalikes(con, f, d, case["target"], c["tstart"])
        wide = checks.lookalikes(con, f, d, case["target"], c["tstart"], window_hz=10000.0, top=0)
        bundle["lookalikes_10khz"] = {k: v for k, v in wide.items() if k != "strongest_other"}
        if cad:
            bundle["neighbourhood"] = checks.neighbourhood(
                con, f, d, [s["file_id"] for s in cad])
    if info["recovered"]:
        bundle["off_scans_stacked"] = off_stack(scans, cuts, seed, info)
    if info["recovered"] and case.get("twins", True):
        bundle["same_drift_twins"] = twins(scans, cuts, seed, info)
    if info["recovered"] and case.get("probes", True):
        bundle["clock_probes"] = clock_probes(scans, cuts, seed, info)
    out = remote.ROOT / "cases" / "evidence" / case["id"]
    out.mkdir(parents=True, exist_ok=True)
    plot(case, scans, cuts, info, seed, out / "waterfall.png")
    # the same plot without names, for blinded readers
    n_off, blind_cuts = 0, []
    for s_, cu in zip(scans, cuts):
        if "error" in cu:
            blind_cuts.append(cu)
            continue
        n_off += s_["role"] == "OFF"
        blind_cuts.append({**cu, "source_name": "target" if s_["role"] == "ON"
                           else f"pointing {n_off}"})
    plot({"id": code_for(case["id"]), "target": ""}, scans, blind_cuts, info, seed,
         out / "waterfall_blind.png")
    (out / "evidence.json").write_text(json.dumps(bundle, indent=1))
    led = ledger.connect()
    ledger.add_case(led, {**case, "site": site})
    ledger.add_evidence(led, case["id"], out / "evidence.json", info)
    if verbose:
        print(f"{case['id']}: seed pos {info['seed_pos']} recovered={info['recovered']} "
              f"snr={info['snr']} f={f:.6f} d={d:+.4f} "
              f"offset={info.get('offset_from_catalogue_hz')}Hz")
        for r in rows:
            if "error" in r:
                print(f"  {r['pos']} {r['role']:3} {str(r['source']):12} ERROR {r['error']}")
            else:
                cw = r["control_windows"] or {}
                print(f"  {r['pos']} {r['role']:3} {str(r['source']):12} dt={r['dt_s']:>8} "
                      f"track_snr={r['track']['snr']} controls_max={cw.get('max_snr')} "
                      f"exceeds={r['track_exceeds_all_controls']}")
        tw = bundle.get("same_drift_twins")
        if tw and "error" not in tw:
            print(f"  twins: {tw['n_twins_on']} in ON, {tw['n_twins_also_in_off']} also in OFF")
        cp = bundle.get("clock_probes")
        if cp and cp.get("done"):
            cr = cp["control_random_frequencies"]
            print(f"  clock probes: {cp['n_with_same_drift_signal']} of "
                  f"{cp['n_probe_frequencies']} show a same-drift signal; random controls "
                  f"{cr['n_with_same_drift_signal']} of {cr['n']}; "
                  f"{cp['n_matching_one_oscillator']} match one oscillator")
    return bundle


def main():
    with open(sys.argv[1]) as fh:
        run(json.load(fh))


if __name__ == "__main__":
    main()
