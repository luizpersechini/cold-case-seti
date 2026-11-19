"""Rule D2 across downloads: a comb whose lines sit in different downloads.

One transmitter can put out many carriers, spaced by a round clock rate. The
runner's comb test (triage.family_comb) sees only the lines of one download
group, a few MHz of band, that show up in the same scans. This module looks
at the lines measured in one scan whatever download they came from, in two
ways.

Join: a comb that was closed has a grid, fitted to its own lines. An open
line measured in the same scan that sits on a tooth of that grid, within the
distance the comb test allows inside one download, belongs to it.

New: the open lines of one scan are tested for a comb at the round clock
rates of triage._round_clock, with at least MIN_LINES teeth as in the runner.

Either closes only when the chance of an unrelated match, multiplied by the
number of comparisons made, is below triage.CHANCE_MAX. Nothing is
downloaded: the frequencies are the ones in the stored evidence.

usage: python -m coldcase.combs             dry run: combs, joins, chance
       python -m coldcase.combs --apply     close what passes in the queue
       python -m coldcase.combs --null [N]  the rule N times with every grid
                                            moved and every open line shifted
       python -m coldcase.combs --controls  the rule on the injected sky-only
                                            signals, which no comb can hold
       python -m coldcase.combs --catalogue the survey's own hits in the scan
                                            of each known comb: on its teeth?
"""

import json
import sys
from collections import defaultdict
from math import comb as choose

import numpy as np

from . import harmonics, ledger, triage
from .remote import ROOT

TOL_HZ = 120.0  # as triage.family_comb: how far a line may sit from its tooth
PPM = 2e-4  # as triage._round_clock: how far a spacing may be from a round rate
MIN_LINES = 4  # as the runner's comb test: the fewest lines that make a comb
SPAN = 60  # the catalogue check looks this many teeth either side of a comb's origin
RATES = sorted(
    r
    for r in [2.0**n for n in range(10, 25)]
    + [m * 10.0**e for e in range(3, 8) for m in (1, 2, 2.5, 5)]
    if r >= 10_000  # family_comb does not try spacings below 10 kHz
)
FAMILY = "spaced by multiples of"  # in the reason of every line closed on a comb
MEMBER = "; this line belongs"  # closed along with another line, not on its own


def _bundle(cid):
    p = ROOT / "cases" / "evidence" / cid / "evidence.json"
    return json.loads(p.read_text()) if p.exists() else None


def _tail(n, k, p):
    """Chance of k or more of n when each has chance p."""
    return sum(choose(n, i) * p**i * (1 - p) ** (n - i) for i in range(k, n + 1))


def fit_grid(freqs_hz):
    """The grid of a closed comb. freqs_hz: its lines, strongest first, as the
    runner tested them. Returns the grid's origin and spacing by least
    squares over the lines on it, and the largest distance of one of those
    lines from its tooth."""
    ref = freqs_hz[0]
    c = triage.family_comb([f - ref for f in freqs_hz[1:]])
    if not c or not c["round_clock"]:
        return None
    s0 = c["spacing_khz"] * 1e3
    on = [(round((f - ref) / s0), f - ref) for f in freqs_hz]
    on = [(k, x) for k, x in on if abs(x - k * s0) <= TOL_HZ]
    ks = np.array([k for k, _ in on], float)
    xs = np.array([x for _, x in on])
    sp, off = np.polyfit(ks, xs, 1)
    return {
        "f0": float(ref + off),
        "spacing": float(sp),
        "scatter": float(np.max(np.abs(xs - (off + ks * sp)))),
        "teeth": (int(ks.min()), int(ks.max())),
        "n": len(on),
        "round_clock": c["round_clock"],
    }


def known_combs():
    """Combs closed so far, each with the grid fitted to its lines."""
    from . import runner

    con, led = runner.queue_db(), ledger.connect()
    groups = defaultdict(list)
    for (cid,) in con.execute(
        "select case_id from queue where status='closed' and note='rfi_identified' "
        "order by case_id"
    ).fetchall():
        row = led.execute(
            "select rationale from verdicts where case_id=? and role='triage' "
            "order by rowid desc limit 1",
            (cid,),
        ).fetchone()
        why = row[0] if row else ""
        if FAMILY not in why or MEMBER in why:
            continue
        b = _bundle(cid)
        seed = b["seed"] if b else None
        sc = b and next((s for s in b["scans"] if s["pos"] == seed["seed_pos"]), None)
        if not sc or not sc.get("url"):
            continue
        groups[(why, sc["url"])].append(
            {
                "id": cid,
                "f": seed["freq_mhz"],
                "strength": runner._strength(b),
                "target": b["case"]["target"],
                "mjd": sc["mjd"],
                "pos": seed["seed_pos"],
            }
        )
    out = []
    for (_, scan), g in groups.items():
        if len(g) < MIN_LINES:
            continue
        g.sort(key=lambda x: -x["strength"])
        grid = fit_grid([x["f"] * 1e6 for x in g])
        if grid:
            out.append(
                {
                    **grid,
                    "scan": scan,
                    "target": g[0]["target"],
                    "mjd": round(g[0]["mjd"], 5),
                    "pos": g[0]["pos"],
                    "ids": sorted(x["id"] for x in g),
                }
            )
    return out


def joins(lines, combs):
    """Every open line measured in the scan of a known comb, with its place
    on that comb's grid. A line joins when it is within TOL_HZ of a tooth and
    the chance of an unrelated line landing that close, times the lines
    compared, is small. The comb's own lines usually sit closer than TOL_HZ
    to a grid fitted to them alone; the grid is less sure a few teeth beyond
    them, which is why their own scatter is not the limit."""
    by = harmonics.by_scan(lines)
    pairs = [(ln, c) for c in combs for ln in by.get(c["scan"], [])]
    out = []
    for ln, c in pairs:
        f = ln["f"] * 1e6
        k = round((f - c["f0"]) / c["spacing"])
        res = f - c["f0"] - k * c["spacing"]
        tol = TOL_HZ
        chance = min(1.0, 2 * tol / c["spacing"] * len(pairs))
        lo, hi = c["teeth"]
        where = (
            f"{lo - k} teeth below its lowest line"
            if k < lo
            else f"{k - hi} teeth above its highest line"
            if k > hi
            else "among its lines"
        )
        out.append(
            {
                "id": ln["id"],
                "target": ln["target"],
                "freq_mhz": ln["f"],
                "tooth": k,
                "off_grid_hz": round(res, 1),
                "tolerance_hz": round(tol, 1),
                "chance": round(chance, 5),
                "passes": abs(res) <= tol and chance < triage.CHANCE_MAX,
                "text": (
                    f"D2: this line is on the comb of one scan that {c['n']} lines closed "
                    f"earlier belong to ({', '.join(c['ids'])}): teeth "
                    f"{c['spacing'] / 1e3:.3f} kHz apart ({c['round_clock']}), a human clock "
                    f"rate. It sits {where}, {res:+.0f} Hz off the grid (the comb's own lines "
                    f"are within {c['scatter']:.0f} Hz, and {tol:.0f} Hz counts as on a "
                    f"tooth). Chance of an unrelated line that close {chance:.4f}"
                ),
            }
        )
    return out, len(pairs)


def _best_comb(group, trials):
    """The least likely comb among the lines of one scan. Two lines fix a
    grid: their distance must be a whole number of teeth of a round rate, to
    within PPM. The other lines either sit on that grid or do not."""
    g = sorted(group, key=lambda ln: ln["f"])
    fs = [ln["f"] * 1e6 for ln in g]
    best = None
    for i in range(len(g)):
        for j in range(i + 1, len(g)):
            d = fs[j] - fs[i]
            for rate in RATES:
                kb = round(d / rate)
                if kb < 1 or abs(d / (kb * rate) - 1) > PPM:
                    continue
                sp = d / kb
                teeth = defaultdict(list)
                for f, ln in zip(fs, g):
                    k = round((f - fs[i]) / sp)
                    if abs(f - fs[i] - k * sp) <= TOL_HZ:
                        teeth[k].append(ln)
                if len(teeth) < MIN_LINES:
                    continue
                # the second line lands within PPM of some tooth, and each tooth
                # beyond the two is one more line within TOL_HZ of the grid
                p = min(1.0, 2 * PPM * kb) * _tail(
                    len(g) - 2, len(teeth) - 2, min(1.0, 2 * TOL_HZ / sp)
                )
                chance = min(1.0, p * trials)
                key = (chance, -len(teeth), -sp)
                if best is None or key < best[0]:
                    best = (key, sp, rate, teeth)
    if best is None:
        return None
    (chance, _, _), sp, rate, teeth = best
    on = sorted((ln for v in teeth.values() for ln in v), key=lambda ln: ln["f"])
    return {
        "spacing": sp,
        "round_clock": triage._round_clock(rate),
        "teeth": len(teeth),
        "lines": on,
        "ids": [ln["id"] for ln in on],
        "target": on[0]["target"],
        "chance": round(chance, 5),
        "passes": chance < triage.CHANCE_MAX,
        "text": (
            f"D2: {len(on)} lines of one scan ({', '.join(ln['id'] for ln in on)}) are "
            f"spaced by multiples of {sp / 1e3:.3f} kHz ({triage._round_clock(rate)}), a "
            f"human clock rate; chance of an unrelated match {chance:.4f}"
        ),
    }


def new_combs(lines):
    """Combs among the open lines of each scan that holds MIN_LINES or more.
    Returns the best comb per scan, then the best among the lines left, for
    as long as one passes; the last entry of a scan may be one that fails."""
    groups = [g for g in harmonics.by_scan(lines).values() if len(g) >= MIN_LINES]
    trials = sum(len(RATES) * len(g) * (len(g) - 1) // 2 for g in groups)
    out = []
    for g in groups:
        left = list(g)
        while len(left) >= MIN_LINES:
            fam = _best_comb(left, trials)
            if fam is None:
                break
            out.append(fam)
            if not fam["passes"]:
                break
            gone = set(fam["ids"])
            left = [ln for ln in left if ln["id"] not in gone]
    return out


def survey():
    lines = harmonics.load_lines()
    combs = known_combs()
    js, compared = joins(lines, combs)
    taken = {x["id"] for x in js if x["passes"]}
    return {
        "lines": lines,
        "combs": combs,
        "joins": js,
        "compared": compared,
        "new": new_combs([ln for ln in lines if ln["id"] not in taken]),
    }


def apply(say=print):
    """Close every line that joins a known comb or stands on a new one, and
    the other lines of an event whose strongest line closes. Returns the case
    ids closed."""
    from . import runner

    res = survey()
    con = runner.queue_db()
    todo = [(x["id"], x["text"]) for x in res["joins"] if x["passes"]]
    todo += [(cid, f["text"]) for f in res["new"] if f["passes"] for cid in f["ids"]]
    closed = []

    def close(cid, why):
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
        runner.log(f"{cid} closed: rfi_identified (comb across downloads)")
        closed.append(cid)

    for cid, why in todo:
        row = con.execute("select status from queue where case_id=?", (cid,)).fetchone()
        if not row or not row[0].startswith("open"):
            continue
        rest = (
            []
            if row[0] == "open_member"
            else [
                r[0]
                for r in con.execute(
                    "select case_id from queue where status='open_member' and note=?",
                    (cid,),
                ).fetchall()
            ]
        )
        close(cid, why)
        own = dict(todo)
        for m in rest:
            close(m, own.get(m) or f"{why}; this line belongs to the event of {cid}")
    say(f"closed {len(closed)} lines on combs across downloads")
    return closed


def null_test(n=1000, seed=1):
    """The rule where no comb can be. Joins: every known grid is moved by a
    random part of its spacing. New combs: every open line is moved by up to
    2 MHz either way. Returns the share of tries in which each closes
    anything."""
    rng = np.random.default_rng(seed)
    lines, combs = harmonics.load_lines(), known_combs()
    j = w = 0
    for _ in range(n):
        moved = [
            {**c, "f0": c["f0"] + float(rng.uniform(0, c["spacing"]))} for c in combs
        ]
        j += any(x["passes"] for x in joins(lines, moved)[0])
        fake = [{**ln, "f": ln["f"] + float(rng.uniform(-2.0, 2.0))} for ln in lines]
        w += any(f["passes"] for f in new_combs(fake))
    return j / n, w / n


def controls_test(say=print):
    """The rule on the injected sky-only signals still open, by fixed half.
    They sit at drawn frequencies, so any that joins a comb or stands on a
    new one is a wrong closure."""
    from . import controls, runner

    con = runner.queue_db()
    lines, combs = harmonics.load_lines(), known_combs()
    ctl = []
    for cid, grp in con.execute(
        "select case_id, grp from controls where kind like 'sky%' and status='open' "
        "and recovered=1 and masked=0 order by case_id"
    ).fetchall():
        b = _bundle(cid)
        seed = b["seed"] if b else None
        sc = b and next((s for s in b["scans"] if s["pos"] == seed["seed_pos"]), None)
        if not sc or not sc.get("url") or not seed.get("recovered"):
            continue
        ctl.append(
            {
                "id": cid,
                "target": b["case"]["target"],
                "f": seed["freq_mhz"],
                "scan": sc["url"],
                "half": controls.half(grp),
            }
        )
    js, _ = joins(ctl, combs)
    fams = new_combs(lines + ctl)
    crowded = sum(
        1 for g in harmonics.by_scan(lines + ctl).values() if len(g) >= MIN_LINES
    )
    half_of = {c["id"]: c["half"] for c in ctl}
    out = {}
    for h in (0, 1):
        ids = {c["id"] for c in ctl if c["half"] == h}
        out[h] = {
            "controls": len(ids),
            "in_scan_of_known_comb": sum(1 for x in js if x["id"] in ids),
            "joined": sum(1 for x in js if x["id"] in ids and x["passes"]),
            "on_new_comb": sum(
                1 for f in fams if f["passes"] for cid in f["ids"] if cid in ids
            ),
        }
        say(
            f"half {h}: {out[h]['controls']} open sky-only controls; "
            f"{out[h]['in_scan_of_known_comb']} measured in the scan of a known comb, "
            f"{out[h]['joined']} joined; {out[h]['on_new_comb']} on a new comb"
        )
    say(
        f"scans holding {MIN_LINES} or more lines once controls are added: {crowded}; "
        f"combs found there that pass: "
        f"{sum(1 for f in fams if f['passes'])} "
        f"({sum(1 for f in fams if f['passes'] and any(i in half_of for i in f['ids']))} "
        "with a control)"
    )
    return out


def catalogue_test(say=print):
    """Free check in the survey's hit tables. In the scan of each known comb:
    of the catalogued hits within SPAN teeth of it, how many sit within
    TOL_HZ of a tooth, and how many do when the grid is moved by a part of
    its spacing. The comb's grid comes from the queue's lines alone, so the
    other hits are a test of it."""
    from . import db

    con = db.connect()
    out = []
    for c in known_combs():
        cad = db.cadence_for(con, c["target"], c["mjd"])
        fid = next((s["file_id"] for s in cad or [] if s["pos"] == c["pos"]), None)
        if fid is None:
            say(f"{c['target']} MJD {c['mjd']}: scan not in the hit tables")
            continue
        fr = (
            con.execute("select freq from hits_u where file_id=?", [fid]).fetchnumpy()[
                "freq"
            ]
            * 1e6
        )
        fr = fr[np.abs(fr - c["f0"]) < SPAN * c["spacing"]]

        def teeth(shift):
            x = fr - c["f0"] - shift
            k = np.round(x / c["spacing"])
            ok = np.abs(x - k * c["spacing"]) <= TOL_HZ
            return int(ok.sum()), len(set(k[ok].astype(int)))

        on, n_teeth = teeth(0.0)
        moved = [teeth(s * c["spacing"])[0] for s in np.linspace(0.05, 0.95, 19)]
        out.append(
            {
                "target": c["target"],
                "mjd": c["mjd"],
                "hits_within_span": int(len(fr)),
                "on_a_tooth": on,
                "teeth_with_a_hit": n_teeth,
                "on_a_tooth_moved_grids": moved,
            }
        )
        say(
            f"{c['target']} MJD {c['mjd']}: {len(fr)} catalogued hits within {SPAN} teeth "
            f"of the comb; {on} within {TOL_HZ:.0f} Hz of a tooth, on {n_teeth} "
            f"different teeth; on 19 moved grids: {sum(moved)} in all"
        )
    return out


def show(res, say=print):
    say(f"{len(res['lines'])} open lines; {len(res['combs'])} comb(s) closed so far")
    for c in res["combs"]:
        say(
            f"comb   {c['target']} MJD {c['mjd']}: {c['n']} lines, teeth "
            f"{c['spacing'] / 1e3:.4f} kHz apart ({c['round_clock']}), own lines within "
            f"{c['scatter']:.0f} Hz of the grid"
        )
    say(f"{res['compared']} open line(s) measured in the scan of a known comb")
    for x in res["joins"]:
        say(
            ("CLOSE  " if x["passes"] else "no     ")
            + f"{x['id']} {x['target']} {x['freq_mhz']:.6f} MHz: tooth {x['tooth']:+d}, "
            f"{x['off_grid_hz']:+.0f} Hz off the grid (allowed {x['tolerance_hz']:.0f}), "
            f"chance {x['chance']:.4f}"
        )
    for f in res["new"]:
        say(("CLOSE  " if f["passes"] else "weak   ") + f["text"])
    if not res["new"]:
        say("no new comb among the open lines of one scan")


def main():
    args = sys.argv[1:]
    if args[:1] == ["--apply"]:
        apply()
    elif args[:1] == ["--null"]:
        n = int(args[1]) if len(args) > 1 else 1000
        j, w = null_test(n)
        print(
            f"{n} tries: moved grids close a line in {j:.1%}; shifted lines make a "
            f"comb in {w:.1%}"
        )
    elif args[:1] == ["--controls"]:
        controls_test()
    elif args[:1] == ["--catalogue"]:
        catalogue_test()
    else:
        show(survey())


if __name__ == "__main__":
    main()
