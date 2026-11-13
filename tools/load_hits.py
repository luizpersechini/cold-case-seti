"""Stream turboSETI .dat hit tables out of the Zenodo tar.gz into parquet.

Nothing is extracted to disk; members are parsed in memory. A truncated
archive (download still running) is tolerated: whatever is complete is loaded.

usage: load_hits.py ARCHIVE_TAR_GZ OUT_DIR
"""

import io
import os
import re
import sys
import tarfile
import time
import warnings

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

PART_ROWS = 4_000_000
RE_SOURCE = re.compile(rb"#\s*Source:\s*(\S+)")
RE_MJD = re.compile(rb"#\s*MJD:\s*([0-9.]+)\s+RA:\s*(\S+)\s+DEC:\s*(\S+)")
RE_DT = re.compile(rb"#\s*DELTAT:\s*([-0-9.eE+]+)\s+DELTAF\(Hz\):\s*([-0-9.eE+]+)")


def parse(body):
    head = body[:2000]
    src = RE_SOURCE.search(head)
    mjd = RE_MJD.search(head)
    dt = RE_DT.search(head)
    meta = {
        "source": src.group(1).decode() if src else None,
        "mjd": float(mjd.group(1)) if mjd else None,
        "ra": mjd.group(2).decode() if mjd else None,
        "dec": mjd.group(3).decode() if mjd else None,
        "deltat": float(dt.group(1)) if dt else None,
        "deltaf": float(dt.group(2)) if dt else None,
    }
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        arr = np.loadtxt(io.BytesIO(body), comments="#", ndmin=2)
    return meta, arr


def flush(cols, out_dir, part):
    if not cols["file_id"]:
        return
    t = pa.table(
        {
            "file_id": pa.array(np.concatenate(cols["file_id"]), pa.int32()),
            "drift": pa.array(np.concatenate(cols["drift"]), pa.float32()),
            "snr": pa.array(np.concatenate(cols["snr"]), pa.float32()),
            "freq": pa.array(np.concatenate(cols["freq"]), pa.float64()),
            "chan": pa.array(np.concatenate(cols["chan"]), pa.int64()),
            "coarse": pa.array(np.concatenate(cols["coarse"]), pa.int32()),
        }
    )
    pq.write_table(
        t, os.path.join(out_dir, f"hits_{part:04d}.parquet"), compression="zstd"
    )
    for v in cols.values():
        v.clear()


def main():
    archive, out_dir = sys.argv[1], sys.argv[2]
    os.makedirs(out_dir, exist_ok=True)
    cols = {k: [] for k in ("file_id", "drift", "snr", "freq", "chan", "coarse")}
    scans, pending, part, total, bad, t0 = [], 0, 0, 0, 0, time.time()
    truncated = False
    try:
        with tarfile.open(archive, "r|gz") as tar:
            for m in tar:
                if not (m.isfile() and m.name.endswith(".dat")):
                    continue
                body = tar.extractfile(m).read()
                try:
                    meta, arr = parse(body)
                except Exception:
                    bad += 1
                    continue
                fid = len(scans)
                n = 0 if arr.size == 0 else arr.shape[0]
                if n and arr.shape[1] < 11:
                    bad += 1
                    continue
                scans.append(
                    {
                        "file_id": fid,
                        "name": os.path.basename(m.name),
                        "dir": os.path.dirname(m.name),
                        "n_hits": n,
                        **meta,
                    }
                )
                if n:
                    cols["file_id"].append(np.full(n, fid, np.int32))
                    cols["drift"].append(arr[:, 1])
                    cols["snr"].append(arr[:, 2])
                    cols["freq"].append(arr[:, 3])
                    cols["chan"].append(arr[:, 5])
                    cols["coarse"].append(arr[:, 10])
                    pending += n
                    total += n
                if pending >= PART_ROWS:
                    flush(cols, out_dir, part)
                    part, pending = part + 1, 0
    except (EOFError, tarfile.ReadError, OSError) as e:
        truncated = True
        print(f"archive ended early ({type(e).__name__}); loaded what was complete")
    flush(cols, out_dir, part)
    pq.write_table(pa.Table.from_pylist(scans), os.path.join(out_dir, "scans.parquet"))
    print(
        f"scans={len(scans)} hits={total} unparsed_files={bad} "
        f"truncated={truncated} seconds={time.time() - t0:.0f}"
    )


if __name__ == "__main__":
    main()
