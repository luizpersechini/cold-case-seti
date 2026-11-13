"""Incoherent de-Doppler on a cutout: blind search and forced photometry
along a predicted track. Code measures; nothing is interpreted."""

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view


def flatten(data, win=257):
    """Divide out the bandpass estimated from the time-averaged spectrum with
    a median along frequency. Narrow lines (even non-drifting ones) survive."""
    spec = data.mean(axis=0)
    nf = len(spec)
    if nf > 20000:  # wide data: block medians, linearly interpolated
        nb = nf // win
        mids = (np.arange(nb) + 0.5) * win
        meds = np.median(spec[: nb * win].reshape(nb, win), axis=1)
        base = np.interp(np.arange(nf), mids, meds)
    else:
        w = min(win, (nf // 2) * 2 - 1)
        padded = np.pad(spec, w // 2, mode="reflect")
        base = np.median(sliding_window_view(padded, w), axis=1)
    return data / np.clip(base, 1e-30, None)


def _dedrift(data, shifts):
    acc = np.zeros(data.shape[1], dtype=np.float64)
    for i, s in enumerate(shifts):
        acc += np.roll(data[i], -int(s))
    return acc


def _snr(acc):
    med = np.median(acc)
    mad = 1.4826 * np.median(np.abs(acc - med))
    return (acc - med) / max(mad, 1e-30)


def drift_step(foff_hz, tsamp, nt):
    return abs(foff_hz) / (nt * tsamp)


def search(
    data,
    foff_hz,
    tsamp,
    max_drift=4.0,
    snr_min=10.0,
    top=10,
    drift_center=None,
    drift_half=None,
):
    """Blind search. Returns hits as dicts with chan, snr, drift (Hz/s).
    With drift_center/drift_half only drifts in that window are tried.
    A weaker hit whose path touches a stronger hit's path inside the scan is
    dropped: it is the strong signal seen through a slightly wrong drift."""
    nt, nf = data.shape
    flat = flatten(data)
    t = np.arange(nt) * tsamp
    step = drift_step(foff_hz, tsamp, nt)
    if drift_center is None:
        n = int(np.ceil(max_drift / step))
        drifts = np.arange(-n, n + 1) * step
    else:
        n = max(1, int(np.ceil(drift_half / step)))
        drifts = (round(drift_center / step) + np.arange(-n, n + 1)) * step
    pad = int(np.ceil(np.abs(drifts).max() * t[-1] / abs(foff_hz))) + 1
    best = np.full(nf, -np.inf, dtype=np.float32)
    bdr = np.zeros(nf, dtype=np.float32)
    for dr in drifts:
        snr = _snr(_dedrift(flat, np.rint(dr * t / foff_hz))).astype(np.float32)
        snr[:pad] = -np.inf
        snr[-pad:] = -np.inf
        m = snr > best
        best[m] = snr[m]
        bdr[m] = dr
    hits = []
    taken = np.zeros(nf, dtype=bool)
    order = np.argsort(best)[::-1]
    for k in order[: max(2000, 50 * top)]:
        if best[k] < snr_min or len(hits) >= top:
            break
        if taken[k]:
            continue
        d = float(bdr[k])
        ghost = False
        for h in hits:
            # separation (Hz) between this path and a stronger hit's path at
            # the start and end of the scan; touching paths are one signal
            s0 = (k - h["chan"]) * foff_hz
            s1 = s0 + (d - h["drift"]) * t[-1]
            closest = 0.0 if s0 * s1 <= 0 else min(abs(s0), abs(s1))
            if closest <= 3 * abs(foff_hz):
                ghost = True
                break
        taken[max(0, k - 3) : k + 4] = True
        if not ghost:
            hits.append({"chan": int(k), "snr": float(best[k]), "drift": d})
    return hits


def track_multi(data, foff_hz, tsamp, chans, drift, chan_tol=3, drift_tol=0.03):
    """Forced photometry at several predicted start channels at once: for each,
    the strongest S/N for a path starting within chan_tol channels with drift
    within drift_tol of `drift`. A measurement at a predicted place, so it is
    meaningful for OFF scans. drift_lo/drift_hi bound the drifts that reach
    95% of the best S/N."""
    nt, nf = data.shape
    flat = flatten(data)
    t = np.arange(nt) * tsamp
    step = drift_step(foff_hz, tsamp, nt)
    nd = max(1, int(np.ceil(drift_tol / step)))
    span = int(np.ceil((abs(drift) + drift_tol) * t[-1] / abs(foff_hz))) + 1
    tol = int(np.ceil(chan_tol))
    wins, res, per = [], [], []
    for c in chans:
        c0, c1 = int(np.floor(c - tol)), int(np.ceil(c + tol)) + 1
        ok = c0 - span >= 0 and c1 + span <= nf
        wins.append((c0, c1, ok))
        res.append({"in_cutout": ok, "snr": -np.inf if ok else None, "chan": None,
                    "drift": None})
        per.append([])
    for j in range(-nd, nd + 1):
        dr = (round(drift / step) + j) * step  # same grid as the blind search
        snr = _snr(_dedrift(flat, np.rint(dr * t / foff_hz)))
        for r, pr, (c0, c1, ok) in zip(res, per, wins):
            if not ok:
                continue
            k = c0 + int(np.argmax(snr[c0:c1]))
            pr.append((dr, float(snr[k])))
            if snr[k] > r["snr"]:
                r.update(snr=float(snr[k]), chan=int(k), drift=float(dr))
    for r, pr, (c0, c1, ok) in zip(res, per, wins):
        if ok:
            near = [d for d, s in pr if s >= r["snr"] - 0.05 * abs(r["snr"])]
            r["drift_lo"], r["drift_hi"] = float(min(near)), float(max(near))
            r["window_channels"] = c1 - c0
            r["window_drifts"] = 2 * nd + 1
    return res


def track(data, foff_hz, tsamp, chan, drift, chan_tol=3, drift_tol=0.03):
    return track_multi(data, foff_hz, tsamp, [chan], drift, chan_tol, drift_tol)[0]


def time_profile(data, foff_hz, tsamp, chan, drift):
    """How the power along a track is spread over time. A steady transmitter
    gives every time sample a similar share (1/n each); a single burst puts
    nearly all of it in one sample. Returns the largest single-sample share of
    the excess over the noise level, and when it happened."""
    nt, nf = data.shape
    flat = flatten(data)
    med = float(np.median(flat))
    t = np.arange(nt) * tsamp
    ks = np.clip(int(chan) + np.rint(drift * t / foff_hz).astype(int), 1, nf - 2)
    # take the strongest of three neighbouring channels in each sample
    vals = np.array([flat[i, k - 1:k + 2].max() for i, k in enumerate(ks)]) - med
    pos = np.clip(vals, 0, None)
    if pos.sum() <= 0:
        return None
    i = int(np.argmax(pos))
    return {"largest_single_sample_share": round(float(pos[i] / pos.sum()), 2),
            "even_share_would_be": round(1 / nt, 2), "at_time_s": round(float(t[i]), 1),
            "n_samples": int(nt)}


def brightest_sample(data):
    """The single brightest sample of a cutout in units of the noise spread."""
    flat = flatten(data)
    med = np.median(flat)
    mad = 1.4826 * np.median(np.abs(flat - med))
    i, k = np.unravel_index(int(np.argmax(flat)), flat.shape)
    return int(i), int(k), float((flat[i, k] - med) / max(mad, 1e-30))
