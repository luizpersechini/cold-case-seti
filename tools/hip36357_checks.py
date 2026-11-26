"""Checks on the event toward HIP 36357 at 2051.617 MHz (q00671) that a
review of the paper asked for.

  lightcurve   the line's S/N in each 18 s sample along its track, in all six
               scans, and a quadratic fit to its frequency in the samples
               where it is on, against the bend the Earth's rotation gives a
               source on the sky. From cached cutouts.
  scaled       lines in the hit tables of the first two on-star scans whose
               drift is the event's scaled by frequency (one transmitter
               moving), on the same track in both scans and absent from the
               other four; with control rates for comparison.
  downlink     a blind search of the six scans within 120 kHz of 240/221 times
               the event's frequency, the downlink a transponder would pair
               with it. Downloads one chunk per scan (about 360 MB).

usage: python tools/hip36357_checks.py lightcurve|scaled|downlink
Writes cases/evidence/q00671/lightcurve.json and downlink_2228.json.
"""

import bisect
import json
import sys

import numpy as np

from coldcase import checks, db, investigate, remote, runner
from coldcase import dedoppler as dd

EV = "cases/evidence/q00671/"


def lightcurve():
    b = json.load(open(EV + "evidence.json"))
    case = runner._case_from_queue(runner.queue_db(), "q00671")
    scans, cuts, half = investigate._cached_cuts(case, b)
    d = b["seed"]["drift"]
    t0 = min(c["tstart"] for c in cuts)
    rows = []
    for s, bs, cu in zip(scans, b["scans"], cuts):
        flat = dd.flatten(cu["data"])
        fch1, foff = float(cu["fch1"]), float(cu["foff"])
        nt, nf = flat.shape
        fo = foff * 1e6
        f_start = bs["predicted_freq_mhz"]  # at scan start, on the event's track
        for i in range(nt):
            tmid = (i + 0.5) * cu["tsamp"]
            fp = f_start + d * tmid * 1e-6
            k = (fp - fch1) / foff
            k0 = int(round(k))
            row = flat[i].astype(np.float64)
            med = np.median(row); mad = 1.4826 * np.median(np.abs(row - med))
            w = row[k0 - 6:k0 + 7]
            j = int(np.argmax(w)); kk = k0 - 6 + j
            y0, y1, y2 = (float(v) for v in (row[kk - 1], row[kk], row[kk + 1]))
            den = y0 - 2 * y1 + y2
            delta = 0.5 * (y0 - y2) / den if den != 0 else 0.0
            fpk = fch1 + (kk + delta) * foff
            rows.append({
                "pos": s["pos"], "role": s["role"], "i": i,
                "t_s": round((cu["tstart"] - t0) * 86400 + tmid, 2),
                "mjd": cu["tstart"] + tmid / 86400,
                "snr": round(float((y1 - med) / mad), 1),
                "freq_mhz": fpk, "offset_hz": round((fpk - fp) * 1e6, 2),
            })
    # light curve
    for pos in range(6):
        r = [x for x in rows if x["pos"] == pos]
        print(pos, r[0]["role"], " ".join(f"{x['snr']:5.1f}" for x in r))
    # curvature: samples where the line is clearly on
    on = [x for x in rows if x["role"] == "ON" and x["snr"] >= 15]
    t = np.array([x["t_s"] for x in on]); f = np.array([x["freq_mhz"] for x in on]) * 1e6
    tm = t.mean(); tt = t - tm
    for deg in (1, 2):
        c, cov = np.polyfit(tt, f, deg, cov=True)
        res = f - np.polyval(c, tt)
        print(f"deg {deg}: coeffs {c}, rms residual {res.std():.2f} Hz, n {len(t)}")
    c2, cov2 = np.polyfit(tt, f, 2, cov=True)
    curv = 2 * c2[0]; ecurv = 2 * np.sqrt(cov2[0, 0])
    p = b["pointing"]
    m0 = np.mean([x["mjd"] for x in on])
    e0 = checks.expected_drift(p["ra_hours"], p["dec_deg"], m0 - 600 / 86400, "GBT", 2051.6167)["drift_hz_s"]
    e1 = checks.expected_drift(p["ra_hours"], p["dec_deg"], m0 + 600 / 86400, "GBT", 2051.6167)["drift_hz_s"]
    exp_curv = (e1 - e0) / 1200.0
    span = t.max() - t.min()
    print(f"measured d(drift)/dt {curv:.2e} +- {ecurv:.2e} Hz/s^2; expected for a sky source {exp_curv:.2e}; span {span:.0f} s")
    print(f"expected bend over the span: {0.5*exp_curv*(span/2)**2*2:.2f} Hz (quadratic term at the ends)")
    json.dump({"rows": rows, "curvature_hz_s2": curv, "curvature_err": ecurv, "expected_sky_hz_s2": exp_curv,
               "n_samples": len(t), "span_s": span}, open(EV + "lightcurve.json", "w"), indent=1, default=float)


def scaled():
    b = json.load(open(EV + "evidence.json"))
    con = db.connect()
    names = [s["url"].rsplit("/", 1)[-1].replace(".h5", "") for s in b["scans"]]
    F, T = [], []
    for n in names:  # every scan is listed twice in the hit tables
        rows = con.execute("select file_id, mjd from scans where name like ?", [f"%{n}%"]).fetchall()
        F.append([r[0] for r in rows])
        T.append(rows[0][1])
    H = [
        con.execute(
            f"select distinct freq, -drift, snr from hits where file_id in ({','.join('?' * len(F[i]))})",
            F[i],
        ).fetchall()
        for i in range(6)
    ]
    fe, de, tol = b["seed"]["freq_mhz"], b["seed"]["drift"], 0.03
    others = {k: sorted(x[0] for x in H[k]) for k in (1, 3, 4, 5)}

    def near(lst, f, w):
        i = bisect.bisect_left(lst, f - w)
        return i < len(lst) and lst[i] <= f + w

    h2 = sorted(H[2])
    f2 = [x[0] for x in h2]
    dt = (T[2] - T[0]) * 86400

    def count(rate):
        out = []
        for f, d, s in H[0]:
            dexp = rate * f / fe
            if abs(d - dexp) > tol:
                continue
            fp = f + dexp * dt * 1e-6
            i = bisect.bisect_left(f2, fp - 40e-6)
            while i < len(h2) and f2[i] <= fp + 40e-6:
                g, d2, s2 = h2[i]
                if abs(d2 - dexp) <= tol and not any(
                    near(others[k], f + dexp * ((T[k] - T[0]) * 86400) * 1e-6, 300e-6)
                    for k in (1, 3, 4, 5)
                ):
                    out.append((round(f, 6), round(s, 1), round(s2, 1)))
                i += 1
        return out

    print("at the event's drift, scaled by frequency:", count(de))
    rates = (-0.30, -0.25, -0.20, -0.16, -0.09, -0.06, 0.06, 0.09, 0.12, 0.16, 0.20, 0.25)
    print("control rates:", [len(count(r)) for r in rates])


def downlink():
    b = json.load(open(EV + "evidence.json"))
    F_UP = b["seed"]["freq_mhz"]
    F_DN = F_UP * 240 / 221
    HALF = 120.0  # kHz either side
    out = {"uplink_mhz": F_UP, "predicted_downlink_mhz": round(F_DN, 6), "half_khz": HALF, "scans": []}
    mb = 0.0
    for s in b["scans"]:
        cu = remote.read_cutout(s["url"], F_DN, HALF)
        mb += 0 if cu.get("cached") else cu.get("net_bytes", 0) / 1e6
        fo = cu["foff"] * 1e6
        hits = dd.search(cu["data"], fo, cu["tsamp"], max_drift=4.0, snr_min=10.0, top=15)
        rows = []
        for h in hits:
            f = cu["fch1"] + h["chan"] * cu["foff"]
            rows.append({"offset_khz": round((f - F_DN) * 1e3, 3), "snr": round(h["snr"], 1), "drift": round(h["drift"], 4)})
        flat = dd.flatten(cu["data"])
        out["scans"].append({"pos": s["pos"], "role": s["role"], "source": s["source"], "hits": rows})
        print(s["pos"], s["role"], s["source"], len(rows), rows[:6])
        remote.clear_chunks()
    out["net_mb"] = round(mb, 1)
    json.dump(out, open(EV + "downlink_2228.json", "w"), indent=1)
    print("MB", round(mb, 1), "predicted", F_DN)


if __name__ == "__main__":
    {"lightcurve": lightcurve, "scaled": scaled, "downlink": downlink}[sys.argv[1]]()
