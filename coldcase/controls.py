"""Blind controls: synthetic signals injected into each download group while
its data chunks are still cached, then put through the same evidence and
triage code as real cases. They never enter the queue and are never read by
the Sonnet readers. They give a running check on the code rules, and labelled
examples for judging any ranking.

  sky_persistent    in all three ON scans, nothing in OFF   -> should stay open
  sky_intermittent  in one or two ON scans, nothing in OFF  -> should stay open
  leak              in ON scans and weakly in OFF scans     -> should be closed
"""

import hashlib
import json
from datetime import datetime, timezone

import numpy as np

from . import db, investigate, ledger, remote, triage

KINDS = ("sky_persistent", "sky_intermittent", "leak")
PATTERNS = [[1, 0, 0], [0, 1, 0], [0, 0, 1], [1, 1, 0], [0, 1, 1], [1, 0, 1]]
DIR = remote.ROOT / "cases" / "controls"


def table(con):
    con.execute("""create table if not exists controls(
        case_id text primary key, grp text, kind text, band text, freq_mhz real, drift real,
        inj_snr real, spec text, recovered int, status text, verdict text, direct text,
        strongest_on real, created text)""")
    con.commit()


def make(grp, avoid_mhz, n=3):
    """Case dicts for n controls in one download group ('cadence:coarse').
    Needs the group's data chunks in the cache (the runner calls this right
    after the group's real cases)."""
    cad, coarse = (int(x) for x in grp.split(":"))
    hits = db.connect()
    scans = hits.execute(
        "select c.pos, s.file_id, s.mjd, s.name, s.source, s.band from cadences c "
        "join scans s using (file_id) where c.cadence_id=? order by c.pos",
        [cad],
    ).fetchall()
    if len(scans) != 6 or not avoid_mhz:
        return []
    pos0 = scans[0]
    url = remote.url_for_scan(pos0[3], pos0[4])
    ch = remote.read_chunk(url, avoid_mhz[0])  # cached: gives the exact chunk edges
    lo, hi = sorted([ch["fch1"], ch["fch1"] + (ch["data"].shape[1] - 1) * ch["foff"]])
    rng = np.random.default_rng(int(hashlib.sha1(grp.encode()).hexdigest()[:8], 16))
    kinds = list(KINDS) * (n // 3 + 1)
    out, taken = [], list(avoid_mhz)
    for i, kind in enumerate(kinds[:n]):
        for _ in range(200):
            f0 = float(rng.uniform(lo + 0.1, hi - 0.1))
            if all(abs(f0 - t) > 0.02 for t in taken):
                break
        taken.append(f0)
        drift = float(rng.uniform(0.03, 0.5) * rng.choice([-1, 1]))
        if kind == "leak":
            snr = float(np.exp(rng.uniform(np.log(25), np.log(80))))
            off = float(rng.uniform(0.12, 0.25))
        else:
            snr, off = float(np.exp(rng.uniform(np.log(12), np.log(60)))), 0.0
        present = (
            [1, 1, 1]
            if kind != "sky_intermittent"
            else PATTERNS[rng.integers(len(PATTERNS))]
        )
        seed_mjd = scans[(0, 2, 4)[present.index(1)]][2]
        dt = (seed_mjd - pos0[2]) * 86400.0
        curve = float(rng.uniform(-3e-5, 3e-5))
        out.append(
            {
                "id": f"c{cad}_{coarse}_{'abcdefgh'[i]}",
                "origin": "control",
                "kind": kind,
                "group": grp,
                "band": pos0[5],
                "target": pos0[4],
                "site": "GBT",
                "freq_mhz": round(f0 + (drift * dt + 0.5 * curve * dt * dt) * 1e-6, 6),
                "drift": round(drift + curve * dt, 4),
                "mjd": seed_mjd,
                "freq_exact": True,
                "inject": {
                    "f0_mhz": f0,
                    "drift": drift,
                    "snr": round(snr, 1),
                    "present_on": present,
                    "off_fraction": round(off, 3),
                    "scint": [round(float(x), 2) for x in rng.uniform(0.7, 1.3, 3)],
                    "flicker": round(float(rng.uniform(0.15, 0.4)), 2),
                    "curve": curve,
                    "seed": int(rng.integers(1 << 30)),
                },
            }
        )
    return out


def run_group(grp, avoid_mhz, n=3):
    """Inject, gather evidence, triage and record the controls of one group.
    Returns [(case, bundle, decision)] for those that completed."""
    led = ledger.connect()
    table(led)
    done = []
    for case in make(grp, avoid_mhz, n):
        if led.execute(
            "select 1 from controls where case_id=?", (case["id"],)
        ).fetchone():
            continue
        kind, band = case.pop("kind"), case.pop("band")
        try:
            b = investigate.run(case, verbose=False)
        except BaseException as e:
            if isinstance(e, KeyboardInterrupt):
                raise
            if type(e).__name__ == "BrokenProcessPool":  # workers left: a stop
                raise KeyboardInterrupt from e
            continue
        if any("error" in s for s in b["scans"]):
            continue
        dec = triage.decide(b)
        on = [s["track"]["snr"] or 0 for s in b["scans"] if s["role"] == "ON"]
        DIR.mkdir(parents=True, exist_ok=True)
        (DIR / f"{case['id']}.json").write_text(json.dumps(case))
        led.execute(
            "insert into controls values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                case["id"],
                grp,
                kind,
                band,
                case["freq_mhz"],
                case["drift"],
                case["inject"]["snr"],
                json.dumps(case["inject"]),
                int(b["seed"]["recovered"]),
                dec["status"],
                dec["verdict"],
                json.dumps(dec["direct_evidence"]),
                max(on, default=0),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        led.commit()
        done.append((case, b, dec))
    return done


def summary(con=None):
    """Counts for the dashboard: what the code rules did with the controls."""
    con = con or ledger.connect()
    table(con)
    out = {}
    for kind, band, n, rec, opened in con.execute(
        "select kind, band, count(*), sum(recovered), sum(status='open') from controls "
        "group by 1, 2"
    ).fetchall():
        for key in ((kind, "all"), (kind, band)):
            d = out.setdefault(
                key, {"n": 0, "recovered": 0, "open": 0, "closed_by_rule": 0}
            )
            d["n"] += n
            d["recovered"] += rec or 0
            d["open"] += opened or 0
    for kind, band, n in con.execute(
        "select kind, band, count(*) from controls where status='closed' "
        "and verdict not in ('not_recovered') group by 1, 2"
    ).fetchall():
        for key in ((kind, "all"), (kind, band)):
            out[key]["closed_by_rule"] += n
    return out
