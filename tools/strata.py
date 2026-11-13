"""Group scans into ON/OFF cadences and count near-miss hit tracks.

Definitions (ours, phase 0; to be checked against the published pipelines):
  track      a hit in an ON scan followed along its drift into the other scans.
             Predicted frequency in scan j: f + drift * (t_j - t_seed).
  tol_track  20 Hz + 0.02 Hz/s*|dt| + 0.1*|drift|*|dt|      (same-signal test)
  crude      +-1200 Hz around the seed frequency, any OFF scan, no drift match.
             This is turboSETI find_event's rule: max_drift (4 Hz/s) * scan
             length (~300 s). Checked against turbo_seti master, Nov 2026.
  n_on       number of ON scans (1..3) where the track has a drift-consistent hit
  off_track  any OFF hit within tol_track of the track       -> RFI
  off_crude  any OFF hit within the crude window
Classes:
  RFI   off_track
  S2    n_on == 3, off_crude but not off_track   (killed by a non-matching OFF hit)
  S2w   n_on in 1..2, off_crude but not off_track
  E3    n_on == 3, no OFF hit at all             (standard clean event; control)
  S1    n_on in 1..2, no OFF hit at all          (intermittent)
"isolated": no other target in the whole survey has any hit within about
+-1 kHz of the track's frequency (cheap stand-in for the lookalike search).

usage: strata.py HITS_PARQUET_DIR WORK_DIR
"""

import os
import sys
import time
from collections import Counter

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from coldcase import db

KILL_HZ = 1200.0
CLASSES = ("RFI", "S2", "S2w", "E3", "S1")


def tol_track(dt, drift):
    return 20.0 + 0.02 * np.abs(dt) + 0.1 * np.abs(drift) * np.abs(dt)


def any_in(F, lo, hi):
    return np.searchsorted(F, hi, "right") > np.searchsorted(F, lo, "left")


def nearest(F, x):
    """index of the nearest element of sorted F to each x, and the distance"""
    if len(F) == 0:
        return np.zeros(len(x), int), np.full(len(x), np.inf)
    i = (
        np.clip(np.searchsorted(F, x), 1, len(F) - 1)
        if len(F) > 1
        else np.zeros(len(x), int)
    )
    if len(F) > 1:
        left = np.abs(F[i - 1] - x) <= np.abs(F[i] - x)
        i = np.where(left, i - 1, i)
    return i, np.abs(F[i] - x)


def classify(cad, hits):
    """hits: {file_id: (freq_MHz sorted, drift, snr)}. Returns rows of tracks."""
    t = [(s["mjd"] - cad[0]["mjd"]) * 86400.0 for s in cad]
    on, off = (0, 2, 4), (1, 3, 5)
    H = [hits.get(s["file_id"], (np.empty(0), np.empty(0), np.empty(0))) for s in cad]
    consumed = {j: np.zeros(len(H[j][0]), bool) for j in on}
    rows = []
    for s in on:
        F, D, S = H[s]
        keep = ~consumed[s]
        if not keep.any():
            continue
        f, d, snr = F[keep], D[keep], S[keep]
        n_on = np.ones(len(f), int)
        for j in on:
            if j <= s:
                continue
            dt = t[j] - t[s]
            fp = f + d * dt * 1e-6
            idx, dist = nearest(H[j][0], fp)
            if len(H[j][0]) == 0:
                continue
            m = (dist * 1e6 <= tol_track(dt, d)) & (
                np.abs(H[j][1][idx] - d) <= 0.1 + 0.25 * np.abs(d)
            )
            n_on += m
            consumed[j][idx[m]] = True
        off_track = np.zeros(len(f), bool)
        off_crude = np.zeros(len(f), bool)
        for j in off:
            dt = t[j] - t[s]
            fp = f + d * dt * 1e-6
            tt = tol_track(dt, d) * 1e-6
            off_track |= any_in(H[j][0], fp - tt, fp + tt)
            off_crude |= any_in(H[j][0], f - KILL_HZ * 1e-6, f + KILL_HZ * 1e-6)
        cls = np.where(
            off_track,
            0,
            np.where(off_crude, np.where(n_on == 3, 1, 2), np.where(n_on == 3, 3, 4)),
        )
        rows.append((np.full(len(f), s), f, d, snr, n_on, cls))
    if not rows:
        return None
    return [np.concatenate(c) for c in zip(*rows)]


def main():
    pq_dir, work = sys.argv[1], sys.argv[2]
    os.makedirs(work, exist_ok=True)
    t0 = time.time()
    con = duckdb.connect(os.path.join(work, "hits.duckdb"))
    con.execute(
        f"create or replace table hits as select * from '{pq_dir}/hits_*.parquet' "
        "order by file_id, freq"
    )
    con.execute(f"""create or replace table scans as
        select s.*, h.fmed, case when h.fmed < 1950 then 'L' else 'S' end as band
        from '{pq_dir}/scans.parquet' s
        left join (select file_id, median(freq) fmed from hits group by 1) h using (file_id)""")
    n_hits = con.execute("select count(*) from hits").fetchone()[0]
    scans = con.execute("select file_id from scans").fetchall()
    uniq = db.unique_scans(con)
    cads = db.find_cadences(uniq)
    db.write_cadences(con, uniq, cads)
    in_cad = {s["file_id"] for c in cads for s in c}
    print(
        f"hits={n_hits} scans={len(scans)} unique_scans={len(uniq)} "
        f"cadences={len(cads)} scans_unassigned={len(uniq) - len(in_cad)}"
    )

    # how many distinct targets ever have a hit in each 1 kHz bin (lookalike proxy)
    base = "regexp_replace(upper(s.source), '_OFF$', '')"
    b = con.execute(f"""select cast(floor(h.freq*1000) as bigint) bin, count(distinct {base}) n
                    from hits h join scans s using (file_id) group by 1""").fetchnumpy()
    b0 = int(b["bin"].min()) - 2
    nsrc = np.zeros(int(b["bin"].max()) - b0 + 3, np.int32)
    nsrc[b["bin"] - b0] = b["n"]

    counts, iso_counts = Counter(), Counter()
    keep = {
        k: []
        for k in (
            "cadence",
            "target",
            "mjd",
            "band",
            "seed_scan",
            "freq",
            "drift",
            "snr",
            "n_on",
            "cls",
            "nsrc",
        )
    }
    for ci, cad in enumerate(cads):
        ids = [s["file_id"] for s in cad]
        r = con.execute(
            "select file_id, freq, drift, snr from hits where file_id in "
            f"({','.join(map(str, ids))}) order by file_id, freq"
        ).fetchnumpy()
        hits = {}
        for fid in ids:
            m = r["file_id"] == fid
            hits[fid] = (
                r["freq"][m],
                r["drift"][m].astype(np.float64),
                r["snr"][m].astype(np.float64),
            )
        res = classify(cad, hits)
        if res is None:
            continue
        seed, f, d, snr, n_on, cls = res
        bins = np.floor(f * 1000).astype(np.int64) - b0
        crowd = np.maximum.reduce([nsrc[bins - 1], nsrc[bins], nsrc[bins + 1]])
        zero = d == 0
        sb = np.digitize(snr, [25, 100])
        for c in range(5):
            for z in (False, True):
                for k in range(3):
                    m = (cls == c) & (zero == z) & (sb == k)
                    counts[(CLASSES[c], z, k)] += int(m.sum())
                    iso_counts[(CLASSES[c], z, k)] += int((m & (crowd <= 1)).sum())
        m = (cls != 0) & ~zero
        if m.any():
            n = int(m.sum())
            keep["cadence"].append(np.full(n, ci))
            keep["target"].extend([cad[0]["source"]] * n)
            keep["mjd"].append(np.full(n, cad[0]["mjd"]))
            keep["band"].extend([cad[0]["band"]] * n)
            keep["seed_scan"].append(seed[m])
            keep["freq"].append(f[m])
            keep["drift"].append(d[m])
            keep["snr"].append(snr[m])
            keep["n_on"].append(n_on[m])
            keep["cls"].append(cls[m])
            keep["nsrc"].append(crowd[m])

    def cat(k):
        return np.concatenate(keep[k]) if keep[k] else np.empty(0)

    tbl = pa.table(
        {
            "cadence": cat("cadence").astype(np.int32),
            "target": keep["target"],
            "mjd": cat("mjd"),
            "band": keep["band"],
            "seed_scan": cat("seed_scan").astype(np.int8),
            "freq": cat("freq"),
            "drift": cat("drift").astype(np.float32),
            "snr": cat("snr").astype(np.float32),
            "n_on": cat("n_on").astype(np.int8),
            "cls": [CLASSES[int(c)] for c in cat("cls")],
            "nsrc": cat("nsrc").astype(np.int32),
        }
    )
    pq.write_table(
        tbl, os.path.join(work, "tracks_nonrfi_drifting.parquet"), compression="zstd"
    )

    print("\ntracks by class (non-zero drift only), SNR bins 10-25 / 25-100 / 100+")
    print(
        f"{'class':6} {'all':>26}    {'isolated (no other target within ~1 kHz)':>40}"
    )
    for c in CLASSES:
        a = [counts[(c, False, k)] for k in range(3)]
        i = [iso_counts[(c, False, k)] for k in range(3)]
        print(f"{c:6} {a[0]:>9} {a[1]:>8} {a[2]:>7}    {i[0]:>12} {i[1]:>8} {i[2]:>7}")
    print("\nzero-drift tracks by class (all SNR):")
    print(
        "  "
        + "  ".join(
            f"{c}={sum(counts[(c, True, k)] for k in range(3))}" for c in CLASSES
        )
    )
    multi = con.execute(f"""select count(*) from (select {base} t, count(distinct floor(s.mjd)) d
                        from scans s where upper(s.source) not like '%_OFF' group by 1
                        having d > 1)""").fetchone()[0]
    print(f"\ntargets seen on more than one day (ON or OFF role): {multi}")
    print(
        f"cadence targets: {len({c[0]['source'] for c in cads})}  seconds={time.time() - t0:.0f}"
    )


if __name__ == "__main__":
    main()
