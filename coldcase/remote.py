"""Remote access to Breakthrough Listen data: file lookup and narrow cutouts
read with HTTP range requests (HDF5 and sigproc filterbank)."""

import hashlib
import json
import struct
import time
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
API = "http://seti.berkeley.edu/opendata/api/query-files"


def api_files(target, limit=5000):
    """All files the open data API lists for a target name (cached on disk)."""
    cache = DATA / "cache" / "api" / f"{target.upper()}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    url = f"{API}?{urllib.parse.urlencode({'target': target, 'limit': limit})}"
    with urllib.request.urlopen(url, timeout=60) as r:
        rows = json.loads(r.read())["data"]
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(rows))
    return rows


def url_for_scan(dat_name, source):
    """Map a turboSETI .dat name to the URL of its fine-resolution HDF5 file."""
    want = dat_name.rsplit(".dat", 1)[0].lower() + ".h5"
    base = source.upper().removesuffix("_OFF")
    for row in api_files(base):
        if row["url"].rsplit("/", 1)[-1].lower() == want:
            return row["url"]
    return None


def _cache_path(url, f_center, half_khz):
    stem = url.rsplit("/", 1)[-1]
    tag = hashlib.sha1(url.encode()).hexdigest()[:8]
    return DATA / "cutouts" / f"{stem}__{f_center:.6f}__{half_khz:g}__{tag}.npz"


def _counted(url):
    import fsspec

    import aiohttp

    # no overall deadline: a 60 MB chunk can take minutes on a slow line, but
    # give up on a connection that goes silent
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=60, sock_read=180)
    raw = fsspec.open(url, "rb", block_size=256 * 1024, cache_type="bytes",
                      client_kwargs={"timeout": timeout}).open()
    net = {"bytes": 0, "requests": 0}
    fetch = raw.cache.fetcher

    def counting(start, end):
        b = fetch(start, end)
        net["bytes"] += len(b)
        net["requests"] += 1
        return b

    raw.cache.fetcher = counting
    return raw, net


def _read_h5(url, f_center, half_khz, whole_chunk=False):
    import h5py
    import hdf5plugin  # noqa: F401

    raw, net = _counted(url)
    with h5py.File(raw, "r") as h:
        d = h["data"]
        a = {k: (v.decode() if isinstance(v, bytes) else v) for k, v in d.attrs.items()}
        fch1, foff, nchans = float(a["fch1"]), float(a["foff"]), int(d.shape[2])
        k0, k1 = _chan_range(fch1, foff, nchans, f_center, half_khz)
        if whole_chunk and d.chunks:
            size = d.chunks[2]
            k0 = (int((f_center - fch1) / foff) // size) * size
            k1 = min(nchans, k0 + size)
        data = d[:, 0, k0:k1].astype(np.float32)
        hdr = {
            "tsamp": float(a["tsamp"]),
            "tstart": float(a["tstart"]),
            "source_name": str(a.get("source_name", "")),
            "ra_hours": float(a["src_raj"]) if "src_raj" in a else None,
            "dec_deg": float(a["src_dej"]) if "src_dej" in a else None,
        }
    return data, fch1 + k0 * foff, foff, hdr, net


def _sigproc_header(buf):
    """Parse a sigproc filterbank header; returns (dict, header_length)."""

    def s(off):
        n = struct.unpack_from("<i", buf, off)[0]
        return buf[off + 4 : off + 4 + n].decode(), off + 4 + n

    ints = {
        "telescope_id",
        "machine_id",
        "data_type",
        "barycentric",
        "pulsarcentric",
        "nbits",
        "nsamples",
        "nchans",
        "nifs",
        "nbeams",
        "ibeam",
    }
    strs = {"source_name", "rawdatafile"}
    key, off = s(0)
    if key != "HEADER_START":
        raise ValueError("not a sigproc filterbank file")
    h = {}
    while True:
        key, off = s(off)
        if key == "HEADER_END":
            return h, off
        if key in strs:
            h[key], off = s(off)
        elif key in ints:
            h[key] = struct.unpack_from("<i", buf, off)[0]
            off += 4
        else:
            h[key] = struct.unpack_from("<d", buf, off)[0]
            off += 8


def _sex(x):
    """sigproc packs angles as (d|h)dmmss.s in one float"""
    sign = -1 if x < 0 else 1
    x = abs(x)
    d = int(x // 10000)
    m = int((x - d * 10000) // 100)
    return sign * (d + m / 60 + (x - d * 10000 - m * 100) / 3600)


def _dec_parkes(x):
    """Breakthrough Listen Parkes filterbank headers pack a negative
    declination as -(D*10000) + M*100 + S: minutes and seconds are added with
    the wrong sign (-62d40m46s is stored as -615953.84). Verified on three
    sources of known position; one stored value has an impossible minute field."""
    d = int(np.ceil(abs(x) / 10000))
    rem = d * 10000 - abs(x)
    m = int(rem // 100)
    return -(d + m / 60 + (rem - m * 100) / 3600)


def _read_fil(url, f_center, half_khz):
    import fsspec

    fs = fsspec.filesystem("http")
    size = fs.size(url)
    h, hlen = _sigproc_header(fs.cat_file(url, start=0, end=4096))
    if h.get("nbits") != 32 or h.get("nifs", 1) != 1:
        raise ValueError(f"unsupported filterbank layout: nbits={h.get('nbits')}")
    fch1, foff, nchans = h["fch1"], h["foff"], h["nchans"]
    k0, k1 = _chan_range(fch1, foff, nchans, f_center, half_khz)
    nt = (size - hlen) // (nchans * 4)
    starts = [hlen + (i * nchans + k0) * 4 for i in range(nt)]
    ends = [s + (k1 - k0) * 4 for s in starts]
    parts = fs.cat_ranges([url] * nt, starts, ends)
    data = np.stack([np.frombuffer(p, "<f4") for p in parts]).astype(np.float32)
    hdr = {
        "tsamp": h["tsamp"],
        "tstart": h["tstart"],
        "source_name": h.get("source_name", ""),
        "ra_hours": _sex(h["src_raj"]) if "src_raj" in h else None,
        "dec_deg": _sex(h["src_dej"]) if "src_dej" in h else None,
        "band_mhz": sorted([fch1, fch1 + (nchans - 1) * foff]),
    }
    if h.get("telescope_id") == 4 and h.get("src_dej", 0) < 0:
        hdr["dec_deg"] = _dec_parkes(h["src_dej"])
        hdr["dec_note"] = "decoded with the Parkes header quirk"
    net = {"bytes": sum(len(p) for p in parts) + 4096, "requests": nt + 2}
    return data, fch1 + k0 * foff, foff, hdr, net


def _chan_range(fch1, foff, nchans, f_center, half_khz):
    k_c = (f_center - fch1) / foff
    half = abs(half_khz / 1e3 / foff)
    k0 = max(0, int(np.floor(k_c - half)))
    k1 = min(nchans, int(np.ceil(k_c + half)) + 1)
    if k1 <= k0:
        raise ValueError(f"{f_center} MHz is outside the file's band")
    return k0, k1


def _chunk_path(url, k0):
    stem = url.rsplit("/", 1)[-1]
    tag = hashlib.sha1(url.encode()).hexdigest()[:8]
    return DATA / "chunks" / f"{stem}__k{k0}__{tag}.npz"


def read_chunk(url, f_center):
    """The whole HDF5 storage chunk (one coarse channel) holding f_center,
    cached on disk so narrow cutouts and the wide twin search share one
    download. Call clear_chunks() when a batch is done: each is about 60 MB."""
    DATA.joinpath("chunks").mkdir(parents=True, exist_ok=True)
    for path in DATA.joinpath("chunks").glob(url.rsplit("/", 1)[-1] + "__k*"):
        z = np.load(path, allow_pickle=False)
        info = json.loads(str(z["info"]))
        lo, hi = sorted([info["fch1"], info["fch1"] + (z["data"].shape[1] - 1) * info["foff"]])
        if lo <= f_center <= hi:
            return {"data": z["data"], **info, "cached": True}
    t0 = time.time()
    for attempt in range(4):
        try:
            data, fch1, foff, hdr, net = _read_h5(url, f_center, 1.0, whole_chunk=True)
            break
        except Exception:
            if attempt == 3:
                raise
            time.sleep(20 * (attempt + 1))
    info = {"url": url, "fch1": fch1, "foff": foff, **hdr, "net_bytes": net["bytes"],
            "net_requests": net["requests"], "seconds": round(time.time() - t0, 1)}
    k0 = int(round((fch1 - 0) / abs(foff)))  # unique per chunk start frequency
    np.savez(_chunk_path(url, k0), data=data, info=json.dumps(info))
    return {"data": data, **info, "cached": False}


def clear_chunks():
    for path in DATA.joinpath("chunks").glob("*.npz"):
        path.unlink()


def read_cutout(url, f_center, half_khz):
    """Cutout of +-half_khz around f_center (MHz). Returns a dict with
    data[time, chan], fch1 (MHz of channel 0), foff (MHz), tsamp, tstart (MJD)."""
    path = _cache_path(url, f_center, half_khz)
    if path.exists():
        z = np.load(path, allow_pickle=False)
        return {"data": z["data"], **json.loads(str(z["info"])), "cached": True}
    t0 = time.time()
    if url.lower().endswith(".fil"):
        data, fch1, foff, hdr, net = _read_fil(url, f_center, half_khz)
    else:
        ch = read_chunk(url, f_center)
        n = ch["data"].shape[1]
        k_c = (f_center - ch["fch1"]) / ch["foff"]
        half = abs(half_khz / 1e3 / ch["foff"])
        k0, k1 = int(np.floor(k_c - half)), int(np.ceil(k_c + half)) + 1
        if k0 >= 0 and k1 <= n:
            data, fch1, foff = ch["data"][:, k0:k1].copy(), ch["fch1"] + k0 * ch["foff"], ch["foff"]
            hdr = {k: ch[k] for k in ("tsamp", "tstart", "source_name", "ra_hours", "dec_deg")}
            net = {"bytes": 0 if ch["cached"] else ch["net_bytes"],
                   "requests": 0 if ch["cached"] else ch["net_requests"]}
        else:  # cutout straddles a chunk boundary: read it directly
            data, fch1, foff, hdr, net = _read_h5(url, f_center, half_khz)
    info = {"url": url, "fch1": fch1, "foff": foff, **hdr,
            "net_bytes": net["bytes"], "net_requests": net["requests"],
            "seconds": round(time.time() - t0, 1)}
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, data=data, info=json.dumps(info))
    return {"data": data, **info, "cached": False}


def read_wide(url, f_center, fil_half_khz=500.0):
    """Wide view around f_center for same-drift twin searches. HDF5: the whole
    storage chunk holding f_center (from the chunk cache). Filterbank:
    +-fil_half_khz, read directly."""
    try:
        if url.lower().endswith(".fil"):
            data, fch1, foff, hdr, net = _read_fil(url, f_center, fil_half_khz)
            return {"data": data, "url": url, "fch1": fch1, "foff": foff, **hdr,
                    "net_bytes": net["bytes"]}
        ch = read_chunk(url, f_center)
        return {**ch, "net_bytes": 0 if ch["cached"] else ch["net_bytes"]}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "url": url}
