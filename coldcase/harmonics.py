"""Rule D6: harmonics of one clock, found by comparing open events.

Electronics that run on a crystal oscillator leak lines at whole multiples of
its rate. A crystal is never exactly on its nominal rate, and every harmonic
is off by the same fraction. So when two or more lines seen in the same scan
sit at different whole multiples of one clock rate, and all are the same
number of parts per million away from nominal, they come from one oscillator.
The lines can be hundreds of MHz apart, which is why the comb test inside one
download group (D2) cannot see them.

An open event's measured companions (coldcase/companions.py: lines elsewhere
in the band that rise and fall with it in the same scan) count as lines of
that scan; a family that holds an event and its companions closes the event.

A family closes its lines only when the chance of an unrelated match, summed
over every clock rate tried and multiplied by the number of line pairs
compared, is below triage.CHANCE_MAX. A single line near a multiple of a clock
that was identified in another scan is only flagged for the report.

usage: python -m coldcase.harmonics             dry run: families, flags, chance
       python -m coldcase.harmonics --apply     close the families in the queue
       python -m coldcase.harmonics --null [N]  the rule on N random redraws of
                                                the open lines' frequencies
       python -m coldcase.harmonics --predict   look for harmonics the queue
                                                never held (downloads data)
"""

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations

import numpy as np

from . import ledger, triage
from .remote import ROOT

CLOCKS = triage.CLOCKS_MHZ
PPM_MAX = triage.CLOCK_PPM_MAX
AGREE = triage.CLOCK_AGREE_PPM
FLAG_DAYS = 10.0  # a single line is flagged against clocks identified this near in time
STORE = ROOT / "cases" / "harmonics.json"
BANDS = {"L": (1100.0, 1900.0), "S": (1800.0, 2800.0)}  # GBT receivers, MHz


def load_lines():
    """Every open line of the queue (events and the other lines of an event)
    with its measured frequency and the scan it was measured in."""
    from . import runner

    rows = (
        runner.queue_db()
        .execute(
            "select case_id, target, status, note from queue "
            "where status in ('open','open_unread','open_member') order by case_id"
        )
        .fetchall()
    )
    lines = []
    for cid, target, status, note in rows:
        p = ROOT / "cases" / "evidence" / cid / "evidence.json"
        if not p.exists():
            continue
        b = json.loads(p.read_text())
        seed = b["seed"]
        sc = next((s for s in b["scans"] if s["pos"] == seed["seed_pos"]), None)
        if sc is None or not sc.get("url") or not seed.get("recovered"):
            continue
        lines.append(
            {
                "id": cid,
                "target": target,
                "status": status,
                "f": seed["freq_mhz"],
                "drift": seed["drift"],
                "scan": sc["url"],
                "mjd": sc["mjd"],
                "pos": seed["seed_pos"],
            }
        )
        # lines elsewhere in the band measured to rise and fall with this one
        # in the same scan (coldcase/companions.py) are lines of that scan too
        cp = ROOT / "cases" / "companions" / f"{cid}.json"
        if cp.exists():
            for x in json.loads(cp.read_text()).get("leads", []):
                if x.get("outcome") != "companion":
                    continue
                lines.append(
                    {
                        "id": f"{cid} companion {x['freq_mhz']:.4f} MHz",
                        "target": target,
                        "status": "companion",
                        "f": x["freq_mhz"],
                        "drift": x["drift"],
                        "scan": sc["url"],
                        "mjd": sc["mjd"],
                        "pos": seed["seed_pos"],
                        "companion_of": cid,
                    }
                )
    return lines


def by_scan(lines):
    out = defaultdict(list)
    for ln in lines:
        out[ln["scan"]].append(ln)
    return out


def n_pairs(lines):
    """How many pairs of lines share a scan: the comparisons the rule makes."""
    return sum(len(v) * (len(v) - 1) // 2 for v in by_scan(lines).values())


def harmonic(f_mhz, clock_mhz):
    """Nearest whole multiple of the clock, and how far the line is from it in
    parts per million of the clock rate."""
    n = round(f_mhz / clock_mhz)
    if n < 1:
        return 0, None
    return n, (f_mhz / (n * clock_mhz) - 1.0) * 1e6


def pair_chance(fa, fb):
    """Upper bound on the chance that two unrelated lines look like harmonics
    of one clock. For each clock rate: the first line must fall within PPM_MAX
    of some multiple, and the second within AGREE of the offset that fixes,
    at any other multiple. Clock rates that divide one another count the same
    coincidence more than once, so the sum overstates the chance."""
    return min(
        1.0,
        sum(
            min(1.0, 2 * PPM_MAX * 1e-6 * fa / c) * min(1.0, 2 * AGREE * 1e-6 * fb / c)
            for c in CLOCKS
        ),
    )


def scan_families(lines):
    """Families among the lines of one scan: the most lines that agree, at the
    largest clock rate that explains them; then the same among the rest."""
    left, out = list(lines), []
    while len(left) >= 2:
        best = None
        for clk in sorted(CLOCKS, reverse=True):
            cand = []
            for ln in left:
                n, ppm = harmonic(ln["f"], clk)
                if n and abs(ppm) <= PPM_MAX:
                    cand.append((ppm, n, ln))
            cand.sort(key=lambda c: c[0])
            for i in range(len(cand)):
                run = [c for c in cand[i:] if c[0] - cand[i][0] <= AGREE]
                if len({c[1] for c in run}) >= 2 and (
                    best is None or len(run) > len(best[1])
                ):
                    best = (clk, run)
        if best is None:
            break
        clk, run = best
        ppms = [c[0] for c in run]
        out.append(
            {
                "clock_mhz": clk,
                "ppm": round(float(np.mean(ppms)), 3),
                "spread_ppm": round(max(ppms) - min(ppms), 3),
                "target": run[0][2]["target"],
                "mjd": round(run[0][2]["mjd"], 5),
                "scan": run[0][2]["scan"],
                "pos": run[0][2]["pos"],
                "lines": [
                    {
                        "id": ln["id"],
                        "n": n,
                        "freq_mhz": ln["f"],
                        "ppm": round(p, 3),
                        "drift": ln["drift"],
                    }
                    for p, n, ln in sorted(run, key=lambda c: c[2]["f"])
                ],
            }
        )
        gone = {c[2]["id"] for c in run}
        left = [ln for ln in left if ln["id"] not in gone]
    return out


def judge(fam, pairs):
    """Add the chance figure and the decision to a family."""
    worst = max(
        pair_chance(a["freq_mhz"], b["freq_mhz"])
        for a, b in combinations(fam["lines"], 2)
        if a["n"] != b["n"]
    )
    fam["pairs_compared"] = pairs
    fam["chance"] = round(min(1.0, worst * max(1, pairs)), 5)
    fam["passes"] = fam["chance"] < triage.CHANCE_MAX
    ns = sorted({x["n"] for x in fam["lines"]})
    fam["text"] = (
        f"D6: {len(fam['lines'])} lines of one scan ({', '.join(x['id'] for x in fam['lines'])}) "
        f"sit at {', '.join(map(str, ns))} x {fam['clock_mhz']} MHz, each {fam['ppm']:+.2f} ppm "
        f"from nominal (spread {fam['spread_ppm']:.2f} ppm): harmonics of one oscillator; "
        f"chance of an unrelated match {fam['chance']:.4f}"
    )
    return fam


def find_families(lines):
    pairs = n_pairs(lines)
    fams = []
    for group in by_scan(lines).values():
        if len(group) >= 2:
            fams += [judge(f, pairs) for f in scan_families(group)]
    return sorted(fams, key=lambda f: (f["mjd"], f["scan"]))


def flag_singles(lines, families):
    """Open lines outside every family that sit near a multiple of a clock
    identified in a family within FLAG_DAYS. One line alone lands there by
    chance a few times in a hundred, so this never closes anything."""
    taken = {x["id"] for f in families for x in f["lines"]}
    known = [f for f in families if f.get("passes")]
    flags = []
    for ln in lines:
        if ln["id"] in taken:
            continue
        near = sorted(
            (f for f in known if abs(f["mjd"] - ln["mjd"]) <= FLAG_DAYS),
            key=lambda f: abs(f["mjd"] - ln["mjd"]),
        )
        for f in near:
            n, ppm = harmonic(ln["f"], f["clock_mhz"])
            if n and abs(ppm) <= PPM_MAX:
                p1 = min(1.0, 2 * PPM_MAX * 1e-6 * ln["f"] / f["clock_mhz"])
                flags.append(
                    {
                        "id": ln["id"],
                        "target": ln["target"],
                        "mjd": round(ln["mjd"], 5),
                        "freq_mhz": ln["f"],
                        "n": n,
                        "clock_mhz": f["clock_mhz"],
                        "ppm": round(ppm, 2),
                        "chance_one_line": round(p1, 3),
                        "text": (
                            f"at {n} x {f['clock_mhz']} MHz, {ppm:+.1f} ppm; a "
                            f"{f['clock_mhz']} MHz clock at {f['ppm']:+.1f} ppm was identified "
                            f"toward {f['target']} {abs(f['mjd'] - ln['mjd']):.1f} days away "
                            f"(one line alone does this by chance {p1:.0%} of the time)"
                        ),
                    }
                )
                break
    return flags


def _stored():
    return json.loads(STORE.read_text()) if STORE.exists() else {"families": []}


def survey():
    """Families among the open lines now, with those found on earlier runs
    (whose lines are closed by now), and the flags they give."""
    lines = load_lines()
    new = find_families(lines)
    seen = {f["scan"] for f in new}
    stored = _stored()["families"]
    old = [f for f in stored if f["scan"] not in seen]
    # a family found again keeps the tests already made on it
    kept = {f["scan"]: f for f in stored}
    for f in new:
        for key in ("catalogue_test", "prediction"):
            if key in kept.get(f["scan"], {}):
                f[key] = kept[f["scan"]][key]
    return {
        "lines": lines,
        "new": new,
        "families": old + new,
        "flags": flag_singles(lines, old + new),
        "pairs": n_pairs(lines),
    }


def _save(res):
    STORE.write_text(
        json.dumps(
            {
                "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "pairs_compared": res["pairs"],
                "families": res["families"],
                "flags": res["flags"],
            },
            indent=1,
        )
    )


def apply(say=print):
    """Close the lines of every family that passes, and the other lines of
    the same events. Returns the case ids closed."""
    from . import runner

    res = survey()
    con = runner.queue_db()
    closed = []
    for fam in res["new"]:
        if not fam["passes"]:
            continue
        ids = [x["id"] for x in fam["lines"]]
        rest = [
            r[0]
            for cid in ids
            for r in con.execute(
                "select case_id from queue where status='open_member' and note=?",
                (cid,),
            ).fetchall()
            if r[0] not in ids
        ]
        for cid in ids + rest:
            row = con.execute(
                "select status from queue where case_id=?", (cid,)
            ).fetchone()
            if not row or not row[0].startswith("open"):
                continue
            why = fam["text"] + (
                "" if cid in ids else "; this line belongs to one of those events"
            )
            ledger.add_verdict(
                ledger.connect(),
                {
                    "case_id": cid,
                    "role": "triage",
                    "agent": "code",
                    "verdict": "rfi_identified",
                    "confidence": 1.0,
                    "rationale": why,
                    "evidence_refs": [why],
                },
            )
            runner.set_status(con, cid, "closed", "rfi_identified")
            runner.log(
                f"{cid} closed: rfi_identified (harmonic of a {fam['clock_mhz']} MHz clock)"
            )
            closed.append(cid)
        fam["applied"] = True
    # flags are for what is still open
    gone = set(closed)
    res["flags"] = [x for x in res["flags"] if x["id"] not in gone]
    _save(res)
    say(
        f"closed {len(closed)} lines in {sum(1 for f in res['new'] if f.get('applied'))} families"
    )
    return closed


def null_test(lines, n=1000, mode="band", seed=1):
    """The rule on made-up frequencies. mode 'band': every line gets a
    frequency drawn evenly from its receiver band. mode 'jitter': every line
    is moved by up to 2 MHz either way, which keeps the crowding of the real
    list. Returns the share of redraws in which the rule closes anything."""
    rng = np.random.default_rng(seed)
    groups = [g for g in by_scan(lines).values() if len(g) >= 2]
    pairs = n_pairs(lines)
    closes = 0
    for _ in range(n):
        hit = False
        for g in groups:
            lo, hi = (
                BANDS["L"] if max(x["f"] for x in g) <= BANDS["L"][1] else BANDS["S"]
            )
            fake = [
                {
                    **x,
                    "f": float(rng.uniform(lo, hi))
                    if mode == "band"
                    else x["f"] + float(rng.uniform(-2.0, 2.0)),
                }
                for x in g
            ]
            if any(judge(f, pairs)["passes"] for f in scan_families(fake)):
                hit = True
                break
        closes += hit
    return closes / n


def _catalogue_test(con, fam, cad):
    """Free check in the survey's hit tables: do other multiples of the
    family's clock, at the family's offset, hold a catalogued hit in the same
    scan? The same count at shifted offsets gives the rate to expect anyway."""
    fid = next(s["file_id"] for s in cad if s["pos"] == fam["pos"])
    freqs = np.sort(
        con.execute("select freq from hits_u where file_id=?", [fid]).fetchnumpy()[
            "freq"
        ]
    )
    if len(freqs) == 0:
        return None
    clk, listed = fam["clock_mhz"], {x["n"] for x in fam["lines"]}
    lo, hi = float(freqs[0]), float(freqs[-1])

    def count(ppm):
        ns = [
            n
            for n in range(int(lo / clk), int(hi / clk) + 2)
            if n not in listed and lo <= n * clk * (1 + ppm * 1e-6) <= hi
        ]
        got = []
        for n in ns:
            f = n * clk * (1 + ppm * 1e-6)
            tol = AGREE * 1e-6 * f
            i = np.searchsorted(freqs, f - tol)
            if i < len(freqs) and freqs[i] <= f + tol:
                got.append(n)
        return ns, got

    ns, got = count(fam["ppm"])
    c_n = c_got = 0
    for k in list(range(-10, 0)) + list(range(1, 11)):
        a, b = count(fam["ppm"] + 3.0 * k)
        c_n, c_got = c_n + len(a), c_got + len(b)
    return {
        "file_hits": int(len(freqs)),
        "band_mhz": [round(lo, 1), round(hi, 1)],
        "other_multiples_in_band": len(ns),
        "with_catalogued_hit": got,
        "control_positions": c_n,
        "control_with_hit": c_got,
    }


def predict(say=print):
    """Test the families on what the queue never held. First in the hit tables
    (free); then in the data, at three other multiples per family, on the
    family's scan and on the off-star scan next to it."""
    from . import db, repeat

    res = survey()
    fams = [f for f in res["families"] if f.get("passes")]
    con = db.connect()
    plans = []
    for fam in fams:
        cad = db.cadence_for(con, fam["target"], fam["mjd"])
        cat = _catalogue_test(con, fam, cad) if cad else None
        fam["catalogue_test"] = cat
        head = f"{fam['target']} MJD {fam['mjd']:.3f}, {fam['clock_mhz']} MHz at {fam['ppm']:+.2f} ppm"
        if cat is None:
            say(f"{head}: scan not in the hit tables")
            continue
        say(
            f"{head}: catalogued hit at {len(cat['with_catalogued_hit'])} of "
            f"{cat['other_multiples_in_band']} other multiples "
            f"{cat['with_catalogued_hit']}; at shifted offsets {cat['control_with_hit']} of "
            f"{cat['control_positions']}"
        )
        b = json.loads(
            (
                ROOT / "cases" / "evidence" / fam["lines"][0]["id"] / "evidence.json"
            ).read_text()
        )
        scans = {s["pos"]: s for s in b["scans"]}
        off = scans.get(fam["pos"] + 1) or scans.get(fam["pos"] - 1)
        listed = sorted(x["n"] for x in fam["lines"])
        lo, hi = cat["band_mhz"]
        mid = listed[len(listed) // 2]
        cands = sorted(
            (
                n
                for n in range(
                    int(lo / fam["clock_mhz"]) + 1, int(hi / fam["clock_mhz"])
                )
                if n not in listed
            ),
            key=lambda n: abs(n - mid),
        )[:3]
        ref = fam["lines"][0]
        plans.append((fam, [scans[fam["pos"]], off], cands, ref))
    with repeat._turn():
        for fam, scans, cands, ref in plans:
            rows = []
            for n in cands:
                f = round(n * fam["clock_mhz"] * (1 + fam["ppm"] * 1e-6), 6)
                d = ref["drift"] * f / ref["freq_mhz"]  # same fraction per second
                dtol = 0.05 + 0.25 * abs(d)
                use = [s for s in scans if s and s.get("url")]
                cuts = repeat._fetch_all(
                    [(s["url"], f, repeat._half_khz(abs(d) + dtol)) for s in use]
                )
                got = repeat._measure(use, cuts, [f] * len(use), [d] * len(use), dtol)
                row = {"n": n, "predicted_mhz": f, "scans": got}
                rows.append(row)
                for r in got:
                    m = r.get("margin_over_control_max")
                    say(
                        f"  {n} x {fam['clock_mhz']} MHz = {f:.6f} MHz, {r['role']} scan: "
                        + (
                            f"error {r['error'][:60]}"
                            if "error" in r
                            else f"track {r['track_snr']}, margin over controls {m}, "
                            f"offset {r['offset_hz']} Hz"
                            + (
                                "  <- detected"
                                if m is not None and m >= triage.OFF_MARGIN
                                else ""
                            )
                        )
                    )
            fam["prediction"] = rows
    _save(res)


def show(res, say=print):
    say(
        f"{len(res['lines'])} open lines in {len(by_scan(res['lines']))} scans, "
        f"{res['pairs']} pairs compared"
    )
    for fam in res["new"]:
        say(("CLOSE  " if fam["passes"] else "weak   ") + fam["text"])
        for x in fam["lines"]:
            say(
                f"         {x['id']} {x['freq_mhz']:.6f} MHz = {x['n']} x {fam['clock_mhz']} MHz "
                f"{x['ppm']:+.3f} ppm"
            )
    if not res["new"]:
        say("no family among the open lines")
    for x in res["flags"]:
        say(f"flag   {x['id']} {x['target']} {x['freq_mhz']:.6f} MHz: {x['text']}")


def main():
    args = sys.argv[1:]
    if args[:1] == ["--apply"]:
        apply()
    elif args[:1] == ["--null"]:
        n = int(args[1]) if len(args) > 1 else 1000
        lines = load_lines()
        for mode in ("band", "jitter"):
            print(
                f"{mode}: the rule closes something in {null_test(lines, n, mode):.1%} of {n} redraws"
            )
    elif args[:1] == ["--predict"]:
        predict()
    else:
        show(survey())


if __name__ == "__main__":
    main()
