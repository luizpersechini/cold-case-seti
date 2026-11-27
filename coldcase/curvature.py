"""How much does the line bend within a scan?

A transmitter at rest on the sky drifts because the Earth turns and moves,
and its drift changes slowly: by about 1e-5 Hz/s^2 at most at 2 GHz from the
GBT. A transmitter that moves past the telescope (a satellite, an aircraft)
changes its drift much faster. Here the line of a case is followed sample by
sample in each on-star scan, a quadratic is fitted to its frequency against
time, and the bend (change of drift, Hz/s^2) is compared with what the
Earth's motion gives a source at the star's position.

A strong bend is a flag, never a closure: a transmitter on a planet that
turns once in a few hours would bend as much.

From cached cutouts only; nothing is downloaded and the ledger is not
touched. Writes cases/evidence/<id>/curvature.json.

usage: python -m coldcase.curvature open|closed|controls|CASE_ID...
"""

import json
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np

from . import checks, investigate
from . import dedoppler as dd
from .remote import ROOT

SNR_MIN = 10.0  # samples used in the fit
MIN_SAMPLES = 8  # in one scan
SIGMAS = 5.0  # the bend must differ from the sky's by this many errors
TIMES_SKY = 10.0  # and be this many times the sky's
FLOOR = 1e-6  # Hz/s^2: the sky value below which TIMES_SKY uses this instead
# Added in quadrature to each scan's fit error. On the 272 running test
# signals that could be measured the pulls (bend minus injected over error)
# have a Gaussian core (MAD 0.9) but a tail to 6 sigma in scans of 8 to 13
# samples; without the term 3 of them were flagged. 3e-5 is the largest bend
# given to a test signal. Checked on independent fast-drifting test signals
# by tools/curvature_controls.py.
SYS_ERR = 3e-5


def sky_bend(ra_hours, dec_deg, mjd, site, freq_mhz, step_s=600.0):
    """Change of drift (Hz/s^2) the Earth's rotation and orbit give a source at
    rest relative to the solar system barycentre, at this site and time."""
    from astropy import units as u
    from astropy.time import Time

    _, _, loc, sc = checks._altaz(ra_hours, dec_deg, mjd, site)
    t = Time(mjd, format="mjd") + np.array([-step_s, 0.0, step_s]) * u.s
    v = [
        sc.radial_velocity_correction("barycentric", obstime=x, location=loc)
        .to(u.km / u.s)
        .value
        for x in t
    ]
    return float(freq_mhz * 1e6 * (v[2] - 2 * v[1] + v[0]) / step_s**2 / checks.C_KMS)


def _peak(row, k0, half, med, mad):
    """Strongest channel within half channels of k0, its S/N and the
    power-weighted centre of the channels around it."""
    nf = len(row)
    a, b = max(0, int(k0 - half)), min(nf, int(k0 + half) + 1)
    if b - a < 3:
        return None
    j = a + int(np.argmax(row[a:b]))
    snr = float((row[j] - med) / mad)
    w = max(2, int(np.ceil(half / 3)))
    lo, hi = max(0, j - w), min(nf, j + w + 1)
    p = np.clip(row[lo:hi] - med - 3 * mad, 0, None)
    k = (
        float(lo + np.sum(p * np.arange(hi - lo)) / p.sum())
        if p.sum() > 0
        else float(j)
    )
    return k, snr


def follow_scan(cu, f_start_mhz, drift):
    """Follow the line through the time samples of one scan, both ways from
    its strongest sample near the straight track that starts at f_start_mhz
    with drift (Hz/s). Returns one row per sample: time from the scan's start
    (s), S/N, frequency (MHz)."""
    flat = dd.flatten(cu["data"]).astype(np.float64)
    nt, nf = flat.shape
    fch1, foff, ts = float(cu["fch1"]), float(cu["foff"]), float(cu["tsamp"])
    v0 = drift * ts / (foff * 1e6)  # channels per sample along the track
    smear = abs(v0)
    meds = np.median(flat, axis=1)
    mads = 1.4826 * np.median(np.abs(flat - meds[:, None]), axis=1)
    mads = np.where(mads > 0, mads, 1.0)
    k_line = [(f_start_mhz - fch1) / foff + v0 * (i + 0.5) for i in range(nt)]
    best = None
    for i in range(nt):
        got = _peak(flat[i], k_line[i], smear + 8, meds[i], mads[i])
        if got and (best is None or got[1] > best[2]):
            best = (i, got[0], got[1])
    if best is None:
        return []
    rows = {best[0]: (best[1], best[2])}
    for step in (1, -1):
        k, v, i = best[1], v0, best[0]
        hist = [(i, k)]
        while 0 <= i + step < nt:
            i += step
            pred = k + step * v
            got = _peak(flat[i], pred, 0.3 * abs(v) + 5, meds[i], mads[i])
            if got and got[1] >= SNR_MIN / 2:
                k = got[0]
                hist.append((i, k))
                if len(hist) >= 3:  # local slope from the last three peaks
                    (i0, k0), (i2, k2) = hist[-3], hist[-1]
                    v = (k2 - k0) / (i2 - i0)  # channels per sample, forward in time
            else:
                k = pred
            rows[i] = got if got else (pred, 0.0)
    return [
        {
            "i": i,
            "t_s": round((i + 0.5) * ts, 3),
            "snr": round(rows[i][1], 1),
            "freq_mhz": fch1 + rows[i][0] * foff,
        }
        for i in sorted(rows)
    ]


def fit(rows):
    """Quadratic fit of frequency against time to the samples at SNR_MIN or
    more: bend (Hz/s^2), its error, drift at the middle, residual scatter."""
    use = [r for r in rows if r["snr"] >= SNR_MIN]
    if len(use) < MIN_SAMPLES:
        return None
    t = np.array([r["t_s"] for r in use])
    f = np.array([r["freq_mhz"] for r in use], dtype=np.float64) * 1e6
    tt = t - t.mean()
    c, cov = np.polyfit(tt, f - f.mean(), 2, cov=True)
    res = f - f.mean() - np.polyval(c, tt)
    return {
        "n_samples": len(use),
        "span_s": round(float(t.max() - t.min()), 1),
        "bend_hz_s2": float(2 * c[0]),
        "bend_err": float(2 * np.sqrt(cov[0, 0])),
        "drift_mid_hz_s": round(float(c[1]), 4),
        "rms_hz": round(float(res.std()), 2),
    }


def measure(case, b):
    """The bend of the case's line in each on-star scan where it is seen, their
    weighted mean, and the sky's value. None when the cutouts are not cached
    or the seed was not recovered."""
    if not b["seed"].get("recovered"):
        return None
    got = investigate._cached_cuts(case, b)
    if got is None:
        return None
    scans, cuts, _ = got
    return measure_cuts(case["id"], b["seed"], b["pointing"], b.get("site", "GBT"), scans, cuts)


def measure_cuts(cid, seed, pointing, site, scans, cuts):
    """measure() on cutouts in memory; seed gives the line's frequency at the
    start of the scan at seed_pos and its drift."""
    si = next(i for i, s in enumerate(scans) if s["pos"] == seed["seed_pos"])
    ts0 = cuts[si]["tstart"]
    d = seed["drift"]
    per = []
    for s, cu in zip(scans, cuts):
        if s["role"] != "ON" or "error" in cu:
            continue
        dt = (cu["tstart"] - ts0) * 86400.0
        r = fit(follow_scan(cu, seed["freq_mhz"] + d * dt * 1e-6, d))
        if r:
            r["pos"] = s["pos"]
            r["mjd_mid"] = cu["tstart"] + (cu["data"].shape[0] * cu["tsamp"] / 2) / 86400
            per.append(r)
    out = {"case": cid, "scans": per, "flag": False}
    if not per:
        return out
    w = np.array([1 / (r["bend_err"] ** 2 + SYS_ERR**2) for r in per])
    bend = float(np.sum(w * [r["bend_hz_s2"] for r in per]) / w.sum())
    err = float(1 / np.sqrt(w.sum()))
    mjd = float(np.mean([r["mjd_mid"] for r in per]))
    sky = sky_bend(pointing["ra_hours"], pointing["dec_deg"], mjd, site, seed["freq_mhz"])
    out.update(
        bend_hz_s2=bend,
        bend_err=err,
        sky_hz_s2=sky,
        sigmas=round(abs(bend - sky) / err, 1),
        times_sky=round(abs(bend) / max(abs(sky), FLOOR), 1),
    )
    out["flag"] = bool(out["sigmas"] > SIGMAS and out["times_sky"] > TIMES_SKY)
    return out


def rule(cv):
    """Flag text for a stored measurement, or None."""
    if not cv or not cv.get("flag"):
        return None
    return (
        f"the line's drift changes by {cv['bend_hz_s2']:+.1e} +- {cv['bend_err']:.0e} Hz/s^2 "
        f"within a scan, {cv['times_sky']:.0f} times what the Earth's motion gives a source "
        f"on the sky ({cv['sky_hz_s2']:+.1e}): a transmitter moving past the telescope, or "
        "one that turns fast; not enough to close a case"
    )


def _work(cid):
    from . import runner  # runner imports triage, which imports this module

    try:
        if cid.startswith("c"):
            case = json.loads((ROOT / "cases" / "controls" / f"{cid}.json").read_text())
        else:
            case = runner._case_from_queue(runner.queue_db(), cid)
        path = ROOT / "cases" / "evidence" / cid / "evidence.json"
        if not path.exists():
            return cid, None
        out = measure(case, json.loads(path.read_text()))
        if out is not None:
            (path.parent / "curvature.json").write_text(json.dumps(out, indent=1))
        return cid, out
    except Exception as e:
        return cid, f"{type(e).__name__}: {e}"[:200]


def ids_for(which):
    from . import runner

    if which in ("open", "closed"):
        q = runner.queue_db()
        st = "status like 'open%'" if which == "open" else "status = 'closed'"
        return [
            r[0]
            for r in q.execute(f"select case_id from queue where {st} order by case_id")
        ]
    if which == "controls":
        return sorted(p.stem for p in (ROOT / "cases" / "controls").glob("*.json"))
    return [which]


def main():
    ids = [c for a in sys.argv[1:] for c in ids_for(a)]
    n = flagged = none = 0
    with ProcessPoolExecutor(max_workers=6) as ex:
        for cid, out in ex.map(_work, ids, chunksize=4):
            if isinstance(out, str):
                print(cid, out)
                continue
            if not out or "bend_hz_s2" not in out:
                none += 1
                continue
            n += 1
            flagged += out["flag"]
            if out["flag"] or len(ids) <= 10:
                print(
                    f"{cid}  bend {out['bend_hz_s2']:+.2e} +- {out['bend_err']:.1e}  sky "
                    f"{out['sky_hz_s2']:+.1e}  {out['sigmas']} sigma  {out['times_sky']}x"
                    f"  {'FLAG' if out['flag'] else ''}"
                )
    print(
        f"{len(ids)} cases: {n} measured, {flagged} flagged, {none} without a measurable line"
    )


if __name__ == "__main__":
    main()
