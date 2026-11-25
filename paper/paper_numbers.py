"""The numbers quoted in the paper, recomputed from the ledger, the stored
evidence and the hit database. Read-only.

usage: uv run python paper/paper_numbers.py
"""

import collections
import json
import re
from math import comb, sqrt
from pathlib import Path

import numpy as np

from coldcase import checks, db, ledger

ROOT = Path(__file__).resolve().parent.parent
EV = ROOT / "cases" / "evidence"


def wilson(k, n, z=1.96):
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return f"{k}/{n} = {100 * p:.2f}% (95% {100 * (c - h):.1f}-{100 * (c + h):.1f}%)"


def bundle(cid):
    p = EV / cid / "evidence.json"
    return json.loads(p.read_text()) if p.exists() else None


def queue_closures(led):
    print("== queue: what closed each case (latest code verdict)")
    rows = led.execute(
        """select q.case_id, v.verdict, v.evidence_refs from queue q join verdicts v
        on v.case_id = q.case_id where q.status = 'closed' and v.agent = 'code' and
        v.created = (select max(created) from verdicts where case_id = q.case_id
        and agent = 'code')"""
    ).fetchall()
    cnt = collections.Counter()
    share_only = []
    for cid, verd, refs in rows:
        t = " | ".join(json.loads(refs) if refs else [])
        if verd != "not_a_tone":
            cnt[verd] += 1
            continue
        share = "of the track's power is in one time sample" in t
        fl = "nothing is left of the track" in t
        if t.startswith("D8"):
            cnt["burst: via a companion (D8)"] += 1
        elif "no tone at the catalogue position" in t:
            cnt["burst: no line recovered, a burst in the cutout"] += 1
        elif fl:
            cnt["burst: nothing left without the strongest sample"] += 1
        elif share:
            cnt["burst: half or more of the power in one sample, only"] += 1
            share_only.append(cid)
        else:
            cnt["burst: other"] += 1
    for k, v in sorted(cnt.items()):
        print(f"  {v:4d}  {k}")
    print("  total closed", len(rows))
    stands, shares = 0, []
    for cid in share_only:
        b = bundle(cid)
        row = next(s for s in b["scans"] if s["pos"] == b["seed"]["seed_pos"])
        shares.append(row["track_time_profile"]["largest_single_sample_share"])
        w = row.get("track_without_strongest_sample")
        if w and w["snr"] > w["control_max_snr"]:
            stands += 1
    sh = np.array(shares)
    print(
        f"  of the {len(share_only)} closed on the share alone: {stands} still stand above "
        f"their controls without the strongest sample; share 50-60% in {int((sh < 0.6).sum())}, "
        f"median {np.median(sh):.2f}"
    )


def open_presence(led):
    print("== open events: ON scans in which the track stands above its controls")
    ids = [
        r[0]
        for r in led.execute(
            "select case_id from queue where status in ('open','open_unread')"
        )
    ]
    for thr in (0.0, 1.5):
        cnt = collections.Counter()
        for cid in ids:
            b = bundle(cid)
            n = 0
            for s in b["scans"]:
                tr, cw = s.get("track") or {}, s.get("control_windows") or {}
                if (
                    s["role"] == "ON"
                    and tr.get("snr") is not None
                    and cw.get("max_snr") is not None
                ):
                    n += (
                        tr["snr"] - cw["max_snr"] > thr
                        if thr == 0
                        else tr["snr"] - cw["max_snr"] >= thr
                    )
            cnt[n] += 1
        print(
            f"  margin {'> 0' if thr == 0 else '>= 1.5'}:",
            dict(sorted(cnt.items())),
            "of",
            len(ids),
        )
    dr = [
        abs(r[0])
        for r in led.execute(
            "select drift from queue where status in ('open','open_unread')"
        )
    ]
    print(
        "  open events with |drift| > 1:",
        sum(d > 1 for d in dr),
        " > 0.5:",
        sum(d > 0.5 for d in dr),
    )
    qd = np.array([abs(r[0]) for r in led.execute("select drift from queue")])
    print(
        f"  queue |drift|: median {np.median(qd):.2f}; <= 0.5 in {int((qd <= 0.5).sum())} of {len(qd)}"
    )
    print(
        "  stars in the queue:",
        led.execute("select count(distinct upper(target)) from queue").fetchone()[0],
    )


def controls(led):
    print("== hidden test signals of the queue")
    rows = led.execute(
        "select kind, abs(drift), inj_snr, recovered, status, masked, spec from controls"
    ).fetchall()
    sky = [r for r in rows if r[0] != "leak"]
    leak = [r for r in rows if r[0] == "leak"]
    rec = [r for r in sky if r[3] and not r[5]]
    print(
        "  sky-only",
        len(sky),
        "recovered and not masked",
        len(rec),
        "left open",
        sum(r[4] == "open" for r in rec),
        "| unrecovered",
        sum(not r[3] for r in sky),
        "masked",
        sum(bool(r[5]) for r in sky),
    )
    closed_rec = sum(r[4] == "closed" for r in rec)
    closed_unrec_burst = 5 - closed_rec
    print("  closed by a rule among recovered, unmasked:", wilson(closed_rec, len(rec)))
    print(
        "  the same plus the unrecovered one closed as a burst:",
        wilson(5, len(rec) + closed_unrec_burst),
    )
    msk = [r for r in sky if r[5]]
    print(
        "  counting masked ones too:",
        wilson(closed_rec + sum(r[4] == "closed" for r in msk), len(rec) + len(msk)),
    )
    lu = [r for r in leak if not r[5]]
    print(
        "  leaks caught, unmasked:",
        wilson(sum(r[4] == "closed" for r in lu), len(lu)),
        "| all",
        len(leak),
    )
    print(
        "  injected S/N: sky",
        min(r[2] for r in sky),
        max(r[2] for r in sky),
        "leak",
        min(r[2] for r in leak),
        max(r[2] for r in leak),
        "| |drift|",
        min(r[1] for r in rows),
        max(r[1] for r in rows),
    )
    print(
        "  groups with controls:",
        led.execute("select count(distinct grp) from controls").fetchone()[0],
        "of",
        led.execute("select count(distinct grp) from queue").fetchone()[0],
    )
    edges = [0, 0.1, 0.2, 0.3, 0.4, 0.52]
    print(
        "  by |drift| bin: sky-only recovered | leaks caught | leak off-source S/N bins below"
    )
    for lo, hi in zip(edges[:-1], edges[1:]):
        s = [r for r in sky if lo <= r[1] < hi and not r[5]]
        le = [r for r in lu if lo <= r[1] < hi]
        print(
            f"    {lo:.1f}-{hi:.2f}: {sum(bool(r[3]) for r in s)}/{len(s)} recovered; "
            f"{sum(r[4] == 'closed' for r in le)}/{len(le)} leaks caught"
        )
    for lo, hi in ((0, 4), (4, 6), (6, 8), (8, 12), (12, 99)):
        le = [r for r in lu if lo <= r[2] * json.loads(r[6])["off_fraction"] < hi]
        if le:
            print(
                f"    nominal off-source S/N {lo}-{hi}: {sum(r[4] == 'closed' for r in le)}/{len(le)} caught"
            )


def hms(s):
    a = [float(x) for x in re.findall(r"-?\d+\.?\d*", s)]
    v = abs(a[0]) + a[1] / 60 + a[2] / 3600
    return -v if s.strip().startswith("-") else v


def belt(led):
    print("== angle from the geostationary belt, all survey cadences (first ON scan)")
    con = db.connect()
    rows = con.execute(
        """select distinct upper(s.source), s.band, s.ra, s.dec, s.mjd from cadences c
        join scans s using (file_id) where c.pos = 0"""
    ).fetchall()
    per = {}
    for src, band, ra, dec, mjd in rows:
        try:
            g = checks.geometry(hms(ra), hms(dec), mjd, "GBT")
        except Exception:
            continue
        if g["geo_belt_separation_deg"] is not None:
            per.setdefault((band, src), []).append(
                (g["geo_belt_separation_deg"], hms(dec), mjd)
            )
    fu = {}
    for cid, tgt, band, status in led.execute(
        "select case_id, target, band, status from followup"
    ):
        fu.setdefault((band, tgt.upper()), []).append(status)
    for band in ("L", "S"):
        allv = np.array([v[0][0] for k, v in per.items() if k[0] == band])
        fv = np.array([per[k][0][0] for k in fu if k[0] == band and k in per])
        print(
            f"  {band}: {len(allv)} stars, median {np.median(allv):.1f} deg; follow-up stars {len(fv)}"
            + (f", median {np.median(fv):.1f}" if len(fv) else "")
        )
        for cut in (5, 8, 10, 13, 15, 20):
            p = float(np.mean(allv < cut))
            k, n = int(np.sum(fv < cut)), len(fv)
            tail = (
                sum(comb(n, j) * p**j * (1 - p) ** (n - j) for j in range(k, n + 1))
                if n
                else float("nan")
            )
            print(
                f"    within {cut:2d} deg: all {100 * p:.1f}%; follow-up {k}/{n}; binomial tail {tail:.1e}"
            )
    lf = [per[k][0] for k in fu if k[0] == "L" and k in per]
    mj = np.array([v[2] for v in lf])
    la = np.array([v[0][2] for k, v in per.items() if k[0] == "L"])
    lo, hi = 57510, 57600
    print(
        f"  L-band follow-up stars observed in MJD {lo}-{hi}: {int(((mj > lo) & (mj < hi)).sum())}/{len(mj)}; "
        f"all L-band stars in that range: {100 * np.mean((la > lo) & (la < hi)):.1f}%"
    )
    dec = np.array([v[0][1] for k, v in per.items()])
    sep = np.array([v[0][0] for k, v in per.items()])
    print(
        f"  declination of pointings within 13 deg: {dec[sep < 13].min():.1f} to {dec[sep < 13].max():.1f}"
    )


if __name__ == "__main__":
    led = ledger.connect()
    queue_closures(led)
    open_presence(led)
    controls(led)
    belt(led)
