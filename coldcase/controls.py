"""Blind controls: synthetic signals injected into each download group while
its data chunks are still cached, then put through the same evidence and
triage code as real cases. They never enter the queue and are never read by
the Sonnet readers. They give a running check on the code rules, and labelled
examples for judging any ranking.

  sky_persistent    in all three ON scans, nothing in OFF   -> should stay open
  sky_intermittent  in one or two ON scans, nothing in OFF  -> should stay open
  leak              in ON scans and weakly in OFF scans     -> should be closed

A control is "masked" when what was measured at its position is a stronger
real signal and not the injection; masked controls are counted on their own.

usage: python -m coldcase.controls                    the running score
       python -m coldcase.controls rescore [--apply]  today's rules on the
                                                      stored evidence
       python -m coldcase.controls halves             the same, by fixed half
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
        strongest_on real, created text, masked int default 0)""")
    if "masked" not in [r[1] for r in con.execute("pragma table_info(controls)")]:
        con.execute("alter table controls add column masked int default 0")
    con.commit()


def is_masked(bundle, drift, inj_snr):
    """True when what was measured at the injected position is not the
    injection: more than three times its strength, or a different drift. The
    injection then sits under a stronger real signal, and what the rules do
    with that signal says nothing about the injection."""
    seed = bundle["seed"]
    if not seed.get("recovered"):
        return False
    return bool(
        seed["snr"] > 3 * inj_snr
        or abs(seed["drift"] - drift) > 0.05 + 0.25 * abs(drift)
    )


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
            "insert into controls values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                int(is_masked(b, case["drift"], case["inject"]["snr"])),
            ),
        )
        led.commit()
        done.append((case, b, dec))
    return done


def summary(con=None):
    """Counts for the dashboard: what the code rules did with the controls.
    Masked controls (see is_masked) are counted on their own and left out of
    the recovered, open and closed counts."""
    con = con or ledger.connect()
    table(con)
    out = {}
    for kind, band, n, masked, rec, opened, closed in con.execute(
        "select kind, band, count(*), sum(masked), sum(recovered and not masked), "
        "sum(status='open' and not masked), "
        "sum(status='closed' and verdict <> 'not_recovered' and not masked) "
        "from controls group by 1, 2"
    ).fetchall():
        for key in ((kind, "all"), (kind, band)):
            d = out.setdefault(
                key,
                {"n": 0, "masked": 0, "recovered": 0, "open": 0, "closed_by_rule": 0},
            )
            d["n"] += n
            d["masked"] += masked or 0
            d["recovered"] += rec or 0
            d["open"] += opened or 0
            d["closed_by_rule"] += closed or 0
    return out


def rescore(apply=False, say=print):
    """Run today's rules over the stored evidence of every control and say
    what changes. With apply, write the new decisions to the controls table."""
    con = ledger.connect()
    table(con)
    changed = n = 0
    for cid, kind, status, direct, drift, inj_snr, masked in con.execute(
        "select case_id, kind, status, direct, drift, inj_snr, masked from controls"
    ).fetchall():
        p = remote.ROOT / "cases" / "evidence" / cid / "evidence.json"
        if not p.exists():
            continue
        b = json.loads(p.read_text())
        dec = triage.decide(b)
        m = int(is_masked(b, drift, inj_snr))
        n += 1
        if dec["status"] != status or m != (masked or 0):
            changed += 1
            say(
                f"{cid} {kind}: {status} -> {dec['status']}"
                + (" (masked)" if m else "")
                + (f"; was {direct}" if dec["status"] != status else "")
            )
        if apply:
            con.execute(
                "update controls set status=?, verdict=?, direct=?, masked=? where case_id=?",
                (
                    dec["status"],
                    dec["verdict"],
                    json.dumps(dec["direct_evidence"]),
                    m,
                    cid,
                ),
            )
    if apply:
        con.commit()
    say(f"{n} controls rescored, {changed} changed" + ("" if apply else " (dry run)"))
    for (kind, band), d in sorted(summary(con).items()):
        if band == "all" and apply:
            say(f"  {kind}: {d}")


def half(grp):
    """Which half of the controls a download group falls in, 0 or 1. The split
    is fixed, so a threshold can be set on half 0 and checked on half 1."""
    return int(hashlib.sha1(("split:" + grp).encode()).hexdigest()[:8], 16) % 2


def halves(say=print):
    """Today's rules on the stored evidence, half by half: of the recovered,
    unmasked controls, how many are closed, how many the wide part of D1
    (triage.off_wide) fires on, and how many only it closes."""
    con = ledger.connect()
    table(con)
    out = {}
    for cid, grp, kind, drift, inj_snr in con.execute(
        "select case_id, grp, kind, drift, inj_snr from controls"
    ).fetchall():
        p = remote.ROOT / "cases" / "evidence" / cid / "evidence.json"
        if not p.exists():
            continue
        b = json.loads(p.read_text())
        if not b["seed"].get("recovered") or is_masked(b, drift, inj_snr):
            continue
        dec = triage.decide(b)
        wide = bool(triage.off_wide(b)[0])
        seed_row = next(s for s in b["scans"] if s["pos"] == b["seed"]["seed_pos"])
        text, flag = triage.flash(seed_row)
        d = out.setdefault(
            (half(grp), "leak" if kind == "leak" else "sky_only"),
            {
                "n": 0,
                "closed": 0,
                "wide": 0,
                "wide_only": 0,
                "flash": 0,
                "flash_flag": 0,
            },
        )
        d["n"] += 1
        d["closed"] += dec["status"] == "closed"
        d["wide"] += wide
        d["wide_only"] += wide and len(dec["direct_evidence"]) == 1
        d["flash"] += bool(text)
        d["flash_flag"] += bool(flag)
    for key, d in sorted(out.items()):
        say(f"half {key[0]} {key[1]}: {d}")
    return out


if __name__ == "__main__":
    import sys

    if sys.argv[1:2] == ["rescore"]:
        rescore(apply="--apply" in sys.argv)
    elif sys.argv[1:2] == ["halves"]:
        halves()
    else:
        for key, d in sorted(summary().items()):
            print(key, d)
