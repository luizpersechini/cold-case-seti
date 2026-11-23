"""The survey's own "Requires follow up" events, run through the same first
pass as the queue.

The 2019 nearby-star survey published its inspected events with a label for
each (data/bl_static/extracted/static/{l,s}band2019/*_events.csv). 34 carry
"Requires follow up": 32 at L band and 2 at S band. None is in the queue.
Each becomes a case f001 to f034 at its strongest hit (frequency, drift,
S/N and file as the table lists them), and is measured, checked and decided
with the same code and thresholds as the queue: seed, six scans, off-star
stack, followed line, rules D1 to D8, the comb-tooth test, the same half
hour and other days. Three hidden test signals ride on each event's
downloads. Results go to their own tables (followup, followup_controls), so
the queue and its control scores are untouched.

usage: python -m coldcase.followup plan          the 34 and their cadences
       python -m coldcase.followup run [--max-gb 15]
       python -m coldcase.followup report
"""

import csv
import json
import re
import sys
from datetime import datetime, timezone

from . import controls, db, investigate, ledger, remote, repeat, spurs, triage
from .remote import ROOT

TABLES = ROOT / "data" / "bl_static" / "extracted" / "static"
LABEL = "requires follow up"


def events():
    """The 34 events as case dicts, at the strongest hit of each."""
    out = []
    for band in ("lband2019", "sband2019"):
        with open(TABLES / band / f"{band}_events.csv") as fh:
            for r in csv.DictReader(fh):
                if LABEL not in r["HitCategory"].lower():
                    continue
                num = lambda s: [float(x) for x in s.strip("[]").split()]  # noqa: E731
                f, d, s = num(r["Freqs"]), num(r["DriftRates"]), num(r["SNR"])
                i = max(range(len(s)), key=lambda k: s[k])
                m = re.search(r"guppi_(\d{5})_(\d{5})_", r["FileID"])
                out.append(
                    {
                        "id": f"f{len(out) + 1:03d}",
                        "origin": f"survey event {band} #{r['ID']}: {r['HitCategory']}",
                        "target": r["Source"].upper(),
                        "site": "GBT",
                        "freq_mhz": f[i],
                        "drift": d[i],
                        "mjd": int(m.group(1)) + int(m.group(2)) / 86400.0,
                        "freq_exact": True,
                        "band": band[0].upper(),
                        "snr": s[i],
                        "n_hits": len(f),
                        "file": r["FileID"],
                    }
                )
    return out


def table(con):
    con.execute("""create table if not exists followup(
        case_id text primary key, target text, band text, freq_mhz real, drift real,
        mjd real, snr real, grp text, status text, verdict text, why text,
        session text, other_days text, net_mb real, updated text)""")
    con.execute("""create table if not exists followup_controls(
        case_id text primary key, event text, kind text, recovered int,
        status text, verdict text, why text)""")
    con.commit()


def _grp(hits, cad, case):
    sp = min(
        (s for s in cad if s["pos"] % 2 == 0),
        key=lambda s: abs(s["mjd"] - case["mjd"]),
    )
    row = hits.execute(
        "select coarse from hits where file_id=? order by abs(freq-?) limit 1",
        [sp["file_id"], case["freq_mhz"]],
    ).fetchone()
    return f"{cad[0]['cadence_id']}:{int(row[0])}" if row else None


def plan(say=print):
    hits = db.connect()
    n = 0
    for c in events():
        cad = db.cadence_for(hits, c["target"], c["mjd"])
        g = _grp(hits, cad, c) if cad else None
        n += cad is not None
        say(
            f"{c['id']} {c['band']} {c['target']:<10} {c['freq_mhz']:.6f} MHz "
            f"drift {c['drift']:+.4f} S/N {c['snr']:.1f}  "
            + (f"cadence {g}" if cad else "no six-scan cadence in the tables")
        )
    say(f"{n} of {len(events())} have a cadence")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _controls(led, case, grp, say):
    """Three hidden test signals in this event's downloads, scored apart."""
    try:
        made = controls.make(grp, [case["freq_mhz"]], 3)
    except Exception as e:
        say(f"  controls not made: {type(e).__name__}: {e}"[:160])
        return
    for k, cc in enumerate(made):
        cc["id"] = f"{case['id']}_t{k + 1}"
        kind = cc.pop("kind")
        cc.pop("band", None)
        try:
            b = investigate.run(cc, verbose=False)
        except Exception as e:
            say(f"  {cc['id']}: failed: {type(e).__name__}"[:160])
            continue
        if any("error" in s for s in b["scans"]):
            continue
        dec = triage.decide(b)
        led.execute(
            "insert or replace into followup_controls values (?,?,?,?,?,?,?)",
            (
                cc["id"],
                case["id"],
                kind,
                int(b["seed"]["recovered"]),
                dec["status"],
                dec["verdict"],
                json.dumps(dec["direct_evidence"]),
            ),
        )
        led.commit()
        say(f"  test signal {cc['id']} ({kind}): {dec['status']} ({dec['verdict']})")


def one(led, hits, case, say=print):
    from . import session as sm

    cad = db.cadence_for(hits, case["target"], case["mjd"])
    row = [
        case["id"],
        case["target"],
        case["band"],
        case["freq_mhz"],
        case["drift"],
        case["mjd"],
        case["snr"],
        None,
        "",
        "",
        "",
        None,
        None,
        0.0,
        _now(),
    ]
    if cad is None:
        row[8:11] = ["no_data", "no_data", "no six-scan cadence in the hit tables"]
        return row
    grp = row[7] = _grp(hits, cad, case)
    c = {
        k: case[k]
        for k in (
            "id",
            "origin",
            "target",
            "site",
            "freq_mhz",
            "drift",
            "mjd",
            "freq_exact",
        )
    }
    try:
        b = investigate.run(c, verbose=False)
    except investigate.NoData as e:
        row[8:11] = ["no_data", "no_data", str(e)]
        return row
    row[13] = sum(s.get("net_mb", 0) for s in b["scans"] if "error" not in s)
    if any("error" in s for s in b["scans"]):
        row[8:11] = ["retry", "", "a scan could not be read"]
        return row
    sess = days = None
    if b["seed"].get("recovered"):
        try:
            sess = sm.run(c["id"], save=False, close=False, verbose=False, bundle=b)
        except Exception as e:
            say(f"  same half hour failed: {type(e).__name__}: {e}"[:160])
        try:
            days = repeat.run(c["id"], save=False, verbose=False, bundle=b)
        except Exception as e:
            say(f"  other days failed: {type(e).__name__}: {e}"[:160])
    dec = triage.decide(b, session=sess, repeat=days)
    why = list(dec["direct_evidence"])
    status, verdict = dec["status"], dec["verdict"]
    if status == "open":
        t = spurs.test(hits, b)
        if t and t["tooth"]:
            status, verdict = "closed", "rfi_identified"
            why.append(spurs.text_for(t))
    row[8:13] = [
        status,
        verdict,
        json.dumps(why),
        json.dumps((sess or {}).get("summary")),
        json.dumps((days or {}).get("summary")),
    ]
    row[13] += sum(
        x.get("net_mb", 0) or 0
        for x in ((sess or {}).get("summary") or {}, (days or {}).get("summary") or {})
    )
    if grp:
        _controls(led, c, grp, say)
    return row


def run(max_gb=15.0, say=print):
    led, hits = ledger.connect(), db.connect()
    table(led)
    done = {
        r[0]
        for r in led.execute(
            "select case_id from followup where status not in ('retry')"
        )
    }
    total = sum(r[0] or 0 for r in led.execute("select net_mb from followup")) / 1000
    with repeat._turn():
        for case in events():
            if case["id"] in done:
                continue
            if total > max_gb:
                say(f"stopping: {total:.1f} GB downloaded, the cap is {max_gb:g}")
                break
            say(f"{case['id']} {case['target']} {case['freq_mhz']:.6f} MHz ...")
            try:
                row = one(led, hits, case, say)
            except Exception as e:
                if type(e).__name__ == "BrokenProcessPool":
                    raise
                say(f"  failed: {type(e).__name__}: {e}"[:200])
                continue
            finally:
                remote.clear_chunks()
            led.execute(
                "insert or replace into followup values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
            led.commit()
            total += (row[13] or 0) / 1000
            why = json.loads(row[10]) if row[10].startswith("[") else [row[10]]
            say(
                f"  {row[8]} ({row[9]}) {'; '.join(why)[:200]}  [{total:.2f} GB so far]"
            )


def report(say=print):
    led = ledger.connect()
    table(led)
    rows = led.execute(
        "select case_id, band, target, freq_mhz, drift, snr, status, verdict, why "
        "from followup order by case_id"
    ).fetchall()
    for cid, band, tgt, f, d, s, st, v, why in rows:
        w = json.loads(why) if why and why.startswith("[") else [why or ""]
        say(
            f"{cid} {band} {tgt:<10} {f:.6f} {d:+.4f} S/N {s:5.1f}  {st:<8} {v:<16} {'; '.join(w)[:150]}"
        )
    cnt = {}
    for r in rows:
        cnt[r[6]] = cnt.get(r[6], 0) + 1
    say(f"{len(rows)} of 34 done: {cnt}")
    # events of one download group get the same three test signals; count once
    ctl = led.execute(
        "select kind, count(*), sum(recovered), sum(recovered and status='open'), "
        "sum(recovered and status='closed') from ("
        "  select c.kind, c.recovered, c.status from followup_controls c "
        "  join followup f on f.case_id = c.event group by f.grp, c.kind"
        ") group by kind"
    ).fetchall()
    for k, n, rec, op, cl in ctl:
        say(f"test signals {k}: {n}, found {rec}, left open {op}, closed {cl}")


def main():
    a = sys.argv[1:]
    if a[:1] == ["plan"]:
        plan()
    elif a[:1] == ["run"]:
        run(float(a[a.index("--max-gb") + 1]) if "--max-gb" in a else 15.0)
    elif a[:1] == ["report"]:
        report()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
