"""A second reading of a probe (coldcase/probe.py) that does not lean on the
null frequencies or on the control windows.

The first reading asks, per scan, whether the event's window beats the
strongest of about 45 control windows, and takes the chance of that from
null frequencies chosen where the hit tables list nothing. A review found
three faults in it: the nulls are chosen clean while the event is not, so
the chance comes out too low; where the control windows hold strong lines
neither the event nor a null can ever match; and the nulls' control windows
overlap the event and each other.

This reading uses the cutouts already in the cache and compares the event's
frequency with every other frequency of the same cutouts, taken as they are:

  per scan     the strongest line of the event's drift in each window of
               half-width W, for windows laid side by side over the cutout;
               the event's window is ranked among them (p_s, smaller the
               stronger it is next to the others)
  per window   T = sum over scans of -ln p_s
  the chance   P = share of the other windows whose T is at least the
               event's, counting the event itself

A persistent unrelated line gets a high T at its own window, as it should,
so scans need not be independent. Nothing is conditioned on the hit tables.
P cannot be smaller than one over the number of windows. Three widths are
tried (PERM_W) and the smallest P is multiplied by three.

Nothing is downloaded and nothing is closed here: the result goes to
cases/evidence/<id>/probe_perm.json, and `python -m coldcase.probe close`
applies rule D9 from it to cases named by hand.

usage: python -m coldcase.probe_perm ID...
"""

import hashlib
import json
import sys

import numpy as np

from . import db, probe, remote
from . import dedoppler as dd

PERM_W = (50.0, 500.0, 2000.0)  # window half-widths, Hz
SKIP = 2  # windows next to the event's that are not used as comparisons
TOP = 0.01  # a scan "stands out" when its p_s is at most this
NAME = "probe_perm.json"


def cached(url, f_center):
    """The widest cached cutout of this scan centred on f_center, or None."""
    stem = url.rsplit("/", 1)[-1]
    tag = hashlib.sha1(url.encode()).hexdigest()[:8]
    best = None
    for p in (remote.DATA / "cutouts").glob(f"*__{tag}.npz"):
        parts = p.name[: -len(".npz")].rsplit("__", 3)
        if len(parts) != 4 or parts[0] != stem:
            continue
        if abs(float(parts[1]) - f_center) * 1e6 > 1.0:
            continue
        if best is None or float(parts[2]) > best[0]:
            best = (float(parts[2]), p)
    if best is None:
        return None
    z = np.load(best[1], allow_pickle=False)
    return {"data": z["data"], **json.loads(str(z["info"])), "half_khz": best[0]}


def best_snr(cu, drift, dtol):
    """For every start channel: the strongest S/N over drifts within
    drift+-dtol. Channels a drifting path would leave the cutout from are nan."""
    fo = cu["foff"] * 1e6
    data = cu["data"]
    nt, nf = data.shape
    flat = dd.flatten(data)
    t = np.arange(nt) * cu["tsamp"]
    step = dd.drift_step(fo, cu["tsamp"], nt)
    nd = max(1, int(np.ceil(dtol / step)))
    best = np.full(nf, -np.inf)
    for j in range(-nd, nd + 1):
        dr = (round(drift / step) + j) * step
        best = np.maximum(best, dd._snr(dd._dedrift(flat, np.rint(dr * t / fo))))
    pad = int(np.ceil((abs(drift) + dtol) * t[-1] / abs(fo))) + 1
    best[:pad] = np.nan
    best[-pad:] = np.nan
    return best


def windows(best, k_e, w):
    """{j: strongest S/N in the window of w channels centred j windows from
    channel k_e}, for the windows that lie wholly inside the valid channels."""
    nf = len(best)
    out = {}
    lo_j = -int((k_e - w / 2) // w)
    hi_j = int((nf - k_e - w / 2) // w)
    for j in range(lo_j, hi_j + 1):
        a = int(round(k_e + j * w - w / 2))
        b = a + w
        if a < 0 or b > nf:
            continue
        seg = best[a:b]
        if np.isnan(seg).any():
            continue
        out[j] = (float(seg.max()), int(a + int(np.argmax(seg))))
    return out


def rank_p(vals):
    """Leave-one-out rank of each value among the others, largest first:
    (1 + number of others at least as large) / number of values."""
    v = np.asarray(vals, float)
    order = np.sort(v)
    ge = len(v) - np.searchsorted(order, v, side="left")  # counts itself
    return ge / len(v)


def one_width(per_scan, half_hz):
    """per_scan: list of (best, k_e, |foff| Hz). Returns the event's P and the
    per-scan figures for windows of this half-width."""
    tables = []
    for best, k_e, fo in per_scan:
        w = max(3, int(round(2 * half_hz / fo)))
        tables.append((windows(best, k_e, w), w, fo, k_e))
    common = set.intersection(*[set(t[0]) for t in tables])
    nulls = sorted(j for j in common if abs(j) >= SKIP)
    if 0 not in common or len(nulls) < 20:
        return None
    js = [0] + nulls
    T = np.zeros(len(js))
    rows = []
    for tab, w, fo, k_e in tables:
        m = np.array([tab[j][0] for j in js])
        p = np.empty(len(js))
        p[1:] = rank_p(m[1:])
        p[0] = (1 + np.sum(m[1:] >= m[0])) / len(js)
        T += -np.log(p)
        rows.append(
            {
                "snr": round(float(m[0]), 1),
                "offset_hz": round((tab[0][1] - k_e) * fo, 1),
                "p_scan": round(float(p[0]), 5),
                "median_window_snr": round(float(np.median(m[1:])), 1),
            }
        )
    P = float((1 + np.sum(T[1:] >= T[0])) / len(js))
    return {
        "half_hz": half_hz,
        "windows": len(nulls),
        "T_event": round(float(T[0]), 2),
        "T_null_median": round(float(np.median(T[1:])), 2),
        "T_null_max": round(float(T[1:].max()), 2),
        "T_null_q": [round(float(q), 2) for q in np.percentile(T[1:], [50, 90, 99, 99.9])],
        "P": P,
        "scans_standing_out": int(sum(r["p_scan"] <= TOP for r in rows)),
        "scans": rows,
    }


def run(case_id, say=print):
    folder = remote.ROOT / "cases" / "evidence" / case_id
    week = json.loads((folder / probe.NAME).read_text())
    b = json.loads((folder / "evidence.json").read_text())
    ev = week["event"]
    f_e, d_e, dtol = ev["freq_mhz"], ev["drift"], ev["drift_tol"]
    con = db.connect()
    cad = db.cadence_for(con, b["case"]["target"], ev["mjd"])
    listed = {
        (s["source"].upper(), round(s["mjd"], 5)): s
        for s in probe.scans(con, cad, b["case"]["target"], ev["mjd"], probe.DAYS)
    }
    per_scan, meta, missing = [], [], 0
    for r in week["scans"]:
        s = listed.get((r["source"].upper(), round(r["mjd"], 5)))
        if s is None or "error" in r:
            missing += 1
            continue
        cu = cached(remote.url_for_scan(s["name"], s["source"]), f_e)
        if cu is None:
            missing += 1
            continue
        best = best_snr(cu, d_e, dtol)
        k_e = (f_e - cu["fch1"]) / cu["foff"]
        per_scan.append((best, k_e, abs(cu["foff"]) * 1e6))
        meta.append({"source": r["source"], "hours_from_event": r["hours_from_event"]})
    out = {
        "case_id": case_id,
        "event": ev,
        "scans_used": len(per_scan),
        "scans_not_in_cache": missing,
        "widths": [],
    }
    for half in PERM_W:
        res = one_width(per_scan, half) if per_scan else None
        if res is None:
            continue
        for row, m in zip(res["scans"], meta):
            row.update(m)
        out["widths"].append(res)
        say(
            f"{case_id}  +-{half:g} Hz: P {res['P']:.2g} over {res['windows']} windows; "
            f"{res['scans_standing_out']} of {len(per_scan)} scans stand out; "
            f"T {res['T_event']} against median {res['T_null_median']}, max {res['T_null_max']}"
        )
    if out["widths"]:
        k = min(out["widths"], key=lambda r: r["P"])
        out["P_corrected"] = min(1.0, k["P"] * len(out["widths"]))
        out["best_half_hz"] = k["half_hz"]
        out["scans_standing_out"] = k["scans_standing_out"]
        say(
            f"{case_id}  corrected P {out['P_corrected']:.2g} at +-{k['half_hz']:g} Hz, "
            f"{k['scans_standing_out']} scans standing out"
        )
    out["text"] = closure(out) or describe(out)
    (folder / NAME).write_text(json.dumps(out, indent=1))
    return out


MIN_SCANS = 2  # scans in which the event's window must stand out
CHANCE_MAX = 0.05


def _best(out):
    return next(w for w in out["widths"] if w["half_hz"] == out["best_half_hz"])


def describe(out):
    """One line on a probe that does not meet rule D9."""
    if not out.get("widths"):
        return "no scan of another star in the cache"
    w = _best(out)
    return (
        f"toward other stars the event's frequency does not stand out from the rest of "
        f"the same spectra: {w['scans_standing_out']} of {out['scans_used']} scans, "
        f"chance {out['P_corrected']:.2g} (window +-{w['half_hz']:g} Hz)"
    )


def closure(out):
    """Rule D9: toward other stars in the days around, the event's telescope
    frequency holds a line more consistently than the other frequencies of
    the same spectra do (chance below CHANCE_MAX after the three widths), and
    stands out in at least MIN_SCANS scans. Returns the evidence text, or None."""
    if not out.get("widths"):
        return None
    w = _best(out)
    p = out["P_corrected"]
    if p >= CHANCE_MAX or w["scans_standing_out"] < MIN_SCANS:
        return None
    as_good = round(w["P"] * (w["windows"] + 1)) - 1
    hours = [abs(r["hours_from_event"]) for r in w["scans"] if r["p_scan"] <= TOP]
    return (
        f"D9: the line at the event's telescope frequency stands out in "
        f"{w['scans_standing_out']} of {out['scans_used']} scans of other stars within "
        f"{max(hours):.0f} h of the event (window +-{w['half_hz']:g} Hz); {as_good} of "
        f"{w['windows']} other windows of the same spectra do as well; chance {p:.2g}"
    )


if __name__ == "__main__":
    ids = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not ids:
        print(__doc__)
    for cid in ids:
        run(cid)
