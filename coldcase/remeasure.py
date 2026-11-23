"""Measure every stored case again from the cutouts in the cache, and say
what today's rules make of the new measurements.

Until 2026-11-19 the seed search followed the drift sign of the survey's hit
tables, which is the opposite of the data's (tools/drift_sign.py). A case
that drifts was then followed the wrong way: a steady line looked like a
burst where the wrong path crossed it, and the places predicted for the
other five scans were off. The seed search now takes the stronger of the two
signs. This module makes the measurements again for the queue, the blind
controls and the known cases. Nothing is downloaded: a case whose cutouts
are no longer in the cache is left as it is and counted.

It was used a second time on 2026-11-21, when the seed search was narrowed
to the catalogue position (investigate._seed_half_hz). A new staging starts
a new round; what an earlier round set aside is kept under a numbered name.

The same-half-hour check (session.py) was measured along the earlier seed.
For a case whose seed moved to another track it is made again from the
neighbouring cutouts in the cache. Other-day and satellite results of such a
case are not used until they are made again.

usage: python -m coldcase.remeasure stage    measure into cases/remeasure/;
                                             nothing else changes
       python -m coldcase.remeasure report   what would change, from the
                                             staged measurements
       python -m coldcase.remeasure apply    put them in place: evidence,
                                             queue, controls, plots
"""

import json
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from . import backfill, combs, controls, db, harmonics, investigate, ledger, triage
from .remote import ROOT

STAGE = ROOT / "cases" / "remeasure"
TOGETHER = ("lines of this event", "this line belongs", "of one scan", "a tooth of a comb of stronger lines")
_con = None


def _work(job):
    """One case, in a worker: stored bundle in, staged bundle out."""
    global _con
    case, path = job
    try:
        b = json.loads(Path(path).read_text())
        use = case.get("use_hit_tables", True)
        if use and _con is None:
            _con = db.connect()
        new = investigate.remeasure(case, b, _con if use else None)
    except Exception as e:
        return case["id"], f"error: {type(e).__name__}: {e}"[:160]
    if new is None:
        return case["id"], "no cutout"
    (STAGE / f"{case['id']}.json").write_text(json.dumps(new, indent=1))
    return case["id"], "done"


def _spare(path):
    """`path`, or the first numbered variant of it that does not exist yet,
    so that what an earlier round set aside is never written over."""
    out, i = path, 1
    while out.exists():
        i += 1
        out = path.with_name(f"{path.stem}_{i}{path.suffix}")
    return out


def stage(say=print):
    """Measure into cases/remeasure/. It reads cached cutouts and writes only
    there, so it does not wait for a runner batch or a check to end."""
    STAGE.mkdir(parents=True, exist_ok=True)
    jobs = [(case, str(path)) for case, path in backfill.cases() if path.exists()]
    n = Counter()
    for p in STAGE.glob("*.json"):  # nothing from an earlier staging is reused
        p.unlink()
    (STAGE / "APPLIED").unlink(missing_ok=True)
    with ProcessPoolExecutor(max_workers=8) as ex:
        for i, (cid, res) in enumerate(ex.map(_work, jobs, chunksize=4), 1):
            n[res if res in ("done", "no cutout") else "error"] += 1
            if res.startswith("error"):
                say(f"{cid}: {res}")
            if i % 200 == 0:
                say(f"{i} of {len(jobs)}", flush=True)
    say(", ".join(f"{k} {v}" for k, v in n.items()))
    stage_sessions(say)


def stage_sessions(say=print):
    """The session check again, from cached cutouts, for every queue case
    that has one and whose seed moved."""
    from . import runner, session

    n = Counter()
    for (cid,) in (
        runner.queue_db()
        .execute("select case_id from queue order by case_id")
        .fetchall()
    ):
        b = _staged(cid)
        if (
            b is None
            or not b["remeasured"]["seed_moved"]
            or not (ROOT / "cases" / "evidence" / cid / session.NAME).exists()
        ):
            continue
        out = session.run(
            cid, save=False, close=False, verbose=False, bundle=b, cached_only=True
        )
        (STAGE / f"{cid}.session.json").write_text(json.dumps(out, indent=1))
        n[out["summary"]["outcome"]] += 1
        missing = sum(1 for r in out["scans"] if "error" in r)
        say(
            f"{cid}: {out['summary']['text']}"
            + (f" ({missing} scan(s) not in the cache)" if missing else "")
        )
    say("session check made again: " + ", ".join(f"{k} {v}" for k, v in n.items()))


def _staged(cid):
    p = STAGE / f"{cid}.json"
    return json.loads(p.read_text()) if p.exists() else None


def _extra(cid, moved):
    """The stored other-day, session and satellite results of a case. They
    were measured along the earlier seed, so they are not used once the seed
    has moved to another track."""
    out = {}
    if not moved:
        for key in ("repeat", "session", "satellites"):
            p = ROOT / "cases" / "evidence" / cid / f"{key}.json"
            if p.exists():
                out[key] = json.loads(p.read_text())
    elif (STAGE / f"{cid}.session.json").exists():
        out["session"] = json.loads((STAGE / f"{cid}.session.json").read_text())
    comp = ROOT / "cases" / "companions" / f"{cid}.json"
    if not moved and comp.exists():
        out["companions"] = json.loads(comp.read_text())
    return out


def _kinds(dec):
    return "+".join(sorted({x[:2] for x in dec["direct_evidence"]})) or dec["verdict"]


def plan():
    """What each queue case comes to with the staged measurements. Cases
    closed together with other lines (a comb, clock harmonics, the strongest
    line of their event) keep that closure. Returns case_id -> dict."""
    from . import runner

    con, led = runner.queue_db(), ledger.connect()
    out = {}
    for cid, grp, target, status, note in con.execute(
        "select case_id, grp, target, status, note from queue order by case_id"
    ).fetchall():
        was = "open" if status.startswith("open") else f"closed: {note}"
        row = {"grp": grp, "target": target, "status": status, "was": was}
        if status == "no_data":
            row["was"] = row["now"] = "no data"
        b = _staged(cid)
        if "now" not in row and b is None:
            row["now"], row["how"] = was, "not measured again"
        if "now" not in row:
            last = led.execute(
                "select rationale from verdicts where case_id=? and role='triage' "
                "order by rowid desc limit 1",
                (cid,),
            ).fetchone()
            why = last[0] if last else ""
            row["moved"] = b["remeasured"]["seed_moved"]
            if status == "closed" and (
                why.startswith("D6:") or any(t in why for t in TOGETHER)
            ):
                row["now"], row["how"] = was, "closed with other lines"
            else:
                dec = triage.decide(b, **_extra(cid, row["moved"]))
                row["dec"] = dec
                row["now"] = (
                    "open" if dec["status"] == "open" else f"closed: {dec['verdict']}"
                )
                row["how"] = _kinds(dec)
        out[cid] = row
    return out


def _line(cid, row, b):
    seed = b["seed"]
    sc = next((s for s in b["scans"] if s["pos"] == seed["seed_pos"]), None)
    if sc is None or not sc.get("url") or not seed.get("recovered"):
        return None
    return {
        "id": cid,
        "target": row["target"],
        "status": "open",
        "f": seed["freq_mhz"],
        "drift": seed["drift"],
        "scan": sc["url"],
        "mjd": sc["mjd"],
        "pos": seed["seed_pos"],
    }


def _bundle(cid):
    return _staged(cid) or json.loads(
        (ROOT / "cases" / "evidence" / cid / "evidence.json").read_text()
    )


def families(pl):
    """Events among the cases that come out open, grouped as the runner does
    (same download group, same seed scan, seen in the same ON scans). The
    other-day and session checks are made on the strongest line of an event,
    so a strongest line they close takes the event's weaker lines with it.
    Returns (events as lists of (case_id, bundle), strongest first;
    case_id -> the closed line it goes with)."""
    from . import runner

    groups = defaultdict(list)
    for cid, row in pl.items():
        if row["now"] == "open" or {"D5", "D7"} & set(row.get("how", "").split("+")):
            groups[row["grp"]].append((cid, _bundle(cid)))
    fams, goes_with = [], {}
    for items in groups.values():
        for m in runner.make_families(items):
            rep = m[0][0]
            still = [(cid, b) for cid, b in m if pl[cid]["now"] == "open"]
            if pl[rep]["now"] != "open":
                goes_with.update({cid: rep for cid, _ in still})
            elif still:
                fams.append(still)
    return fams, goes_with


def apply(say=print):
    """Put the staged measurements in place and bring the queue, the controls
    and the plots in line with them. The ledger is copied first; the earlier
    evidence of every case stays next to the new one."""
    import shutil
    from datetime import datetime, timezone

    from . import repeat, runner

    if (STAGE / "APPLIED").exists():
        raise SystemExit("the staged measurements were applied already")
    if not (STAGE / "plan.json").exists():
        raise SystemExit("run `stage` and `report` first")
    with repeat._turn():
        shutil.copy2(ledger.PATH, _spare(STAGE / "ledger_before.sqlite"))
        pl = plan()
        fams, goes_with = families(pl)
        con = runner.queue_db()
        ev = ROOT / "cases" / "evidence"

        def verdict(cid, name, why, refs):
            ledger.add_verdict(
                con,
                {
                    "case_id": cid,
                    "role": "triage",
                    "agent": "code",
                    "verdict": name,
                    "confidence": 1.0,
                    "rationale": why,
                    "evidence_refs": refs,
                },
            )

        # 1. the evidence, and the results that went with the earlier seed
        for p in sorted(STAGE.glob("*.json")):
            if p.name == "plan.json" or p.name.endswith(".session.json"):
                continue
            cid, folder = p.stem, ev / p.stem
            b = json.loads(p.read_text())
            now = folder / "evidence.json"
            if now.exists():
                now.rename(_spare(folder / "evidence_before_remeasure.json"))
            now.write_text(p.read_text())
            ledger.add_evidence(con, cid, now, b["seed"])
            if b["remeasured"]["seed_moved"]:
                for name in (
                    "repeat.json",
                    "satellites.json",
                    "session.json",
                    "session.png",
                ):
                    f = folder / name
                    if f.exists():
                        f.rename(
                            _spare(folder / name.replace(".", "_before_remeasure.", 1))
                        )
                s = STAGE / f"{cid}.session.json"
                if s.exists():
                    (folder / "session.json").write_text(s.read_text())
        # 2. cases decided one at a time
        n = Counter()
        for cid, row in pl.items():
            dec = row.get("dec")
            if dec is None:
                continue
            if row["now"] != row["was"]:
                verdict(
                    cid,
                    dec["verdict"],
                    "; ".join(dec["direct_evidence"]) or "no direct evidence",
                    dec["direct_evidence"],
                )
                n[f"{row['was']} -> {row['now']}"] += 1
                runner.log(f"{cid} measured again: {row['was']} -> {row['now']}")
            if dec["status"] == "closed":
                runner.set_status(con, cid, "closed", dec["verdict"])
        for cid, rep in goes_with.items():
            dec = pl[rep]["dec"]
            why = "; ".join(dec["direct_evidence"])
            verdict(
                cid,
                dec["verdict"],
                f"{why}; this line belongs to the event of {rep}",
                dec["direct_evidence"],
            )
            runner.set_status(con, cid, "closed", dec["verdict"])
            runner.log(f"{cid} closed with {rep}, the strongest line of its event")
        # 3. events among what is open; a reading made on other measurements or
        #    for another set of lines is set aside
        kept = 0
        for members in fams:
            ids = [cid for cid, _ in members]
            rep = ids[0]
            was_members = {
                r[0]
                for r in con.execute(
                    "select case_id from queue where status='open_member' and note=?",
                    (rep,),
                ).fetchall()
            }
            rated = (
                pl[rep]["status"] == "open"
                and not pl[rep].get("moved")
                and was_members == set(ids[1:])
            )
            for cid in ids:
                if cid != rep or not rated:
                    runner.set_status(con, cid, "open_unread", "measured again")
            cases = {cid: runner._case_from_queue(con, cid) for cid in ids}
            if runner._set_family(con, members, cases) is None:
                continue
            if rated:
                kept += 1
                continue
            code = investigate.code_for(rep)
            for role in ("investigator", "challenger"):
                f = ROOT / "cases" / "verdicts" / f"{code}_{role}.json"
                if f.exists():
                    f.rename(
                        _spare(f.with_name(f"{code}_{role}_before_remeasure.json"))
                    )
        # 4. the rules that compare lines
        harmonics.apply(say=say)
        combs.apply(say=say)
        # 5. the controls table
        for cid, drift, snr in con.execute(
            "select case_id, drift, inj_snr from controls"
        ).fetchall():
            p = ev / cid / "evidence.json"
            if not p.exists():
                continue
            b = json.loads(p.read_text())
            dec = triage.decide(b)
            on = [
                s["track"]["snr"] or 0
                for s in b["scans"]
                if s["role"] == "ON" and "error" not in s
            ]
            con.execute(
                "update controls set recovered=?, status=?, verdict=?, direct=?, "
                "strongest_on=?, masked=? where case_id=?",
                (
                    int(b["seed"]["recovered"]),
                    dec["status"],
                    dec["verdict"],
                    json.dumps(dec["direct_evidence"]),
                    max(on, default=0),
                    int(controls.is_masked(b, drift, snr)),
                    cid,
                ),
            )
        con.commit()
        # 6. the plots of what is open
        drawn = 0
        for (cid,) in con.execute(
            "select case_id from queue where status like 'open%' order by case_id"
        ).fetchall():
            b = json.loads((ev / cid / "evidence.json").read_text())
            drawn += investigate.replot(runner._case_from_queue(con, cid), b)
        (STAGE / "APPLIED").write_text(
            datetime.now(timezone.utc).isoformat(timespec="seconds")
        )
    for k, v in sorted(n.items(), key=lambda x: -x[1]):
        say(f"  {v:4}  {k}")
    say(
        f"{sum(n.values())} cases changed; {len(goes_with)} closed with the strongest "
        f"line of their event; {kept} events keep their readings; {drawn} plots drawn"
    )


def events(pl, say=print):
    """Group the cases that would be open into events, as the runner does,
    and try the rules that compare lines: the comb of one event, clock
    harmonics, and combs across downloads. Returns the lines left open and
    the number of events they form."""
    fams, goes_with = families(pl)
    lines, n_events = [], 0
    closed = {cid: "with the strongest line of its event" for cid in goes_with}
    for members in fams:
        if len(members) >= 4:
            f0 = members[0][1]["seed"]["freq_mhz"]
            c = triage.family_comb(
                [(b["seed"]["freq_mhz"] - f0) * 1e6 for _, b in members[1:]]
            )
            if c and c["round_clock"]:
                closed.update({cid: "D2 comb of one event" for cid, _ in members})
                continue
        n_events += 1
        lines += [
            ln for cid, b in members if (ln := _line(cid, pl[cid], b)) is not None
        ]
    rep_of = {cid: m[0][0] for m in fams for cid, _ in m}
    for fam in harmonics.find_families(lines):
        if fam["passes"]:
            closed.update({x["id"]: "D6 clock harmonics" for x in fam["lines"]})
    left = [ln for ln in lines if ln["id"] not in closed]
    js, _ = combs.joins(left, combs.known_combs())
    closed.update({x["id"]: "D2 comb across downloads" for x in js if x["passes"]})
    left = [ln for ln in left if ln["id"] not in closed]
    for fam in combs.new_combs(left):
        if fam["passes"]:
            closed.update({cid: "D2 comb across downloads" for cid in fam["ids"]})
    # an event goes with its strongest line
    gone_reps = {cid for cid in closed if rep_of.get(cid) == cid}
    for cid, rep in rep_of.items():
        if rep in gone_reps and cid not in closed:
            closed[cid] = "with the strongest line of its event"
    open_ids = [
        cid for cid, row in pl.items() if row["now"] == "open" and cid not in closed
    ]
    n_events = len({rep_of.get(cid, cid) for cid in open_ids})
    return open_ids, n_events, closed


def _control_score(rows):
    """rows: (kind, recovered, masked, status, verdict). Counts as in
    controls.summary, for sky-only signals and for leaks."""
    out = {}
    for kind, rec, masked, status, verdict in rows:
        d = out.setdefault(
            "leak" if kind == "leak" else "sky_only",
            {"n": 0, "masked": 0, "recovered": 0, "open": 0, "closed_by_rule": 0},
        )
        d["n"] += 1
        d["masked"] += bool(masked)
        d["recovered"] += bool(rec and not masked)
        d["open"] += bool(status == "open" and not masked)
        d["closed_by_rule"] += bool(
            status == "closed" and verdict != "not_recovered" and not masked
        )
    return out


def control_scores():
    """(today's score, the score with the staged measurements, how many
    controls were measured again)."""
    con = ledger.connect()
    old, new, n = [], [], 0
    for cid, kind, rec, masked, status, verdict, drift, snr in con.execute(
        "select case_id, kind, recovered, masked, status, verdict, drift, inj_snr "
        "from controls order by case_id"
    ).fetchall():
        old.append((kind, rec, masked, status, verdict))
        b = _staged(cid)
        if b is None:
            new.append(old[-1])
            continue
        n += 1
        dec = triage.decide(b)
        new.append(
            (
                kind,
                b["seed"]["recovered"],
                controls.is_masked(b, drift, snr),
                dec["status"],
                dec["verdict"],
            )
        )
    return _control_score(old), _control_score(new), n


def report(say=print):
    pl = plan()
    moves = Counter((r["was"], r["now"]) for r in pl.values())
    say("queue: what each case was, and what it comes to")
    for (was, now), n in sorted(moves.items(), key=lambda x: (x[0][0], -x[1])):
        say(f"  {n:4}  {was:24} -> {now}" + ("" if was != now else "  (no change)"))
    how = Counter((r["now"], r["how"]) for r in pl.values() if r["was"] != r["now"])
    say("changed cases by the rule that now decides them")
    for (now, h), n in sorted(how.items(), key=lambda x: -x[1]):
        say(f"  {n:4}  {now:24} {h}")
    say(
        f"seed moved to another track: "
        f"{sum(1 for r in pl.values() if r.get('moved'))} of "
        f"{sum(1 for r in pl.values() if 'moved' in r)} measured again; "
        f"not measured again: "
        f"{sum(1 for r in pl.values() if r.get('how') == 'not measured again')}"
    )
    open_ids, n_events, closed = events(pl)
    say(
        f"would be open before the rules that compare lines: "
        f"{sum(1 for r in pl.values() if r['now'] == 'open')}; those rules close "
        f"{len(closed)} ({dict(Counter(closed.values()))}); left open: "
        f"{len(open_ids)} lines in {n_events} events"
    )
    was_open = {cid for cid, r in pl.items() if r["was"] == "open"}
    say(
        f"of the {len(was_open)} lines open today: {len(was_open & set(open_ids))} stay "
        f"open, {len(was_open - set(open_ids))} close; newly open: "
        f"{len(set(open_ids) - was_open)}"
    )
    old, new, n = control_scores()
    say(f"controls ({n} measured again): today -> with the new measurements")
    for k in ("sky_only", "leak"):
        say(f"  {k}: {old.get(k)} -> {new.get(k)}")
    say("known cases: today -> with the new measurements")
    for p in sorted((ROOT / "cases" / "known").glob("*.json")):
        cid = json.loads(p.read_text())["id"]
        b = _staged(cid)
        was = triage.decide_case(cid)
        now = triage.decide(b) if b else None
        say(
            f"  {cid}: {was['status']} ({_kinds(was)}) -> "
            + (f"{now['status']} ({_kinds(now)})" if now else "not measured again")
        )
    (STAGE / "plan.json").write_text(
        json.dumps(
            {
                "cases": {
                    cid: {k: v for k, v in r.items() if k != "dec"}
                    | ({"why": r["dec"]["direct_evidence"]} if "dec" in r else {})
                    for cid, r in pl.items()
                },
                "closed_together": closed,
                "open": open_ids,
            },
            indent=1,
        )
    )
    return pl


def main():
    args = sys.argv[1:]
    if args[:1] == ["stage"]:
        stage()
    elif args[:1] == ["sessions"]:
        stage_sessions()
    elif args[:1] == ["report"]:
        report()
    elif args[:1] == ["apply"]:
        apply()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
