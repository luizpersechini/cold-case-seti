"""Summarise the queue: how cases were closed, and the open cases ranked for a
human. Writes report/open_cases.md and prints the same text.

usage: python -m coldcase.report
"""

import json
from collections import Counter
from pathlib import Path

from . import ledger, triage
from .remote import ROOT

PRIORITY = {"high": 0, "medium": 1, "low": 2}


def main():
    con = ledger.connect()
    try:
        rows = con.execute(
            "select case_id, target, freq_mhz, drift, cls, n_on, snr, status, note "
            "from queue"
        ).fetchall()
    except Exception:
        rows = []
    by_status = Counter(r[7] for r in rows)
    closed = Counter(r[8] for r in rows if r[7] == "closed")
    lines = [
        "# Cold Case queue",
        "",
        f"Cases: {len(rows)}. "
        + ", ".join(f"{k} {v}" for k, v in by_status.most_common()),
        "",
        "Closed by code, by reason: "
        + (", ".join(f"{k} {v}" for k, v in closed.most_common()) or "none yet"),
        "",
        "## Open cases, ranked",
        "",
        "| Case | Target | MHz | Drift Hz/s | ON scans | Priority (two readers) | "
        "Context suspicion | Other days | Plot |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    head = len(lines) - 4  # flagged repeats go above the table
    open_rows = []
    # verdict files are named by blind code; KEY.json says which case each is
    keyp = ROOT / "cases" / "briefs" / "KEY.json"
    codes = {}
    for code, path in (json.loads(keyp.read_text()) if keyp.exists() else {}).items():
        codes.setdefault(Path(path).stem, []).append(code)
    for cid, target, f, d, cls, n_on, snr, status, note in rows:
        if status not in ("open", "open_unread"):
            continue
        vs = dict(
            (r[0], json.loads(r[1]) if r[1] else {})
            for r in con.execute(
                "select role, evidence_refs from verdicts where case_id=? and role in "
                "('investigator','challenger')",
                (cid,),
            ).fetchall()
        )
        pr, cs = [], []
        for role in ("investigator", "challenger"):
            for code in codes.get(cid, []):
                path = ROOT / "cases" / "verdicts" / f"{code}_{role}.json"
                if not path.exists():
                    continue
                v = json.loads(path.read_text())
                pr.append(
                    (
                        str(v.get("followup_priority", "")).lower()
                        + (
                            ""
                            if v.get("verdict") == "unresolved"
                            else f" ({v.get('verdict')})"
                        )
                    ).strip()
                )
                if isinstance(v.get("context_suspicion"), (int, float)):
                    cs.append(v["context_suspicion"])
        best = min((PRIORITY.get(x.split(" ")[0], 3) for x in pr), default=3)
        ev = json.loads(
            (ROOT / "cases" / "evidence" / cid / "evidence.json").read_text()
        )
        on = [
            s["track"]["snr"]
            for s in ev["scans"]
            if s["role"] == "ON" and "error" not in s
        ]
        # repeat check on other days (coldcase/repeat.py), if it was run
        rp_path = ROOT / "cases" / "evidence" / cid / "repeat.json"
        rp = json.loads(rp_path.read_text())["summary"] if rp_path.exists() else None
        again = bool(rp) and rp["outcome"] == "sky_frame_repeat"
        open_rows.append(
            (
                0 if again else 1,
                best,
                -max(on, default=0),
                cid,
                target,
                f,
                d,
                on,
                " / ".join(pr) or "not read",
                cs,
                rp["text"] if rp else "not checked",
            )
        )
    flagged = []
    for first, best, _, cid, target, f, d, on, pr, cs, other in sorted(open_rows):
        lines.append(
            f"| {cid} | {target} | {f:.6f} | {d:+.3f} | {on} | {pr} | "
            f"{'-' if not cs else f'{sum(cs) / len(cs):.2f}'} | {other} | "
            f"cases/evidence/{cid}/waterfall.png |"
        )
        if first == 0:
            flagged.append(
                f"- {cid} {target} {f:.6f} MHz: {other}. Plots: "
                f"cases/evidence/{cid}/repeat_*.png"
            )
    if flagged:
        lines[head:head] = [
            "## Look at these first: seen again at the Earth-shifted frequency",
            "",
            *flagged,
            "",
        ]
    if not open_rows:
        lines.append("| none yet | | | | | | | | |")
    # harmonics of one clock across events (coldcase/harmonics.py), if it was run
    hp = ROOT / "cases" / "harmonics.json"
    hz = json.loads(hp.read_text()) if hp.exists() else {}
    fams = [f for f in hz.get("families", []) if f.get("applied")]
    if fams:
        lines += ["", "## Closed as harmonics of one clock (D6)", ""] + [
            f"- {f['target']} MJD {f['mjd']:.3f}: "
            + ", ".join(
                f"{x['id']} at {x['n']} x {f['clock_mhz']} MHz" for x in f["lines"]
            )
            + f", all {f['ppm']:+.2f} ppm from nominal"
            for f in fams
        ]
    # evidence that fell short of closing a case (one other star at the same
    # frequency, a line near the predicted place in an OFF scan of a crowded
    # band): worth a look
    short = []
    for r in sorted(open_rows):
        try:
            for fl in triage.decide_case(r[3]).get("flags", []):
                short.append(f"- {r[3]} {r[4]} {r[5]:.6f} MHz: {fl}")
        except Exception:
            pass
    if short:
        lines += [
            "",
            "## Flagged, not closed: evidence that fell short",
            "",
            *short,
        ]
    still_open = {r[3] for r in open_rows}
    flags = [x for x in hz.get("flags", []) if x["id"] in still_open]
    if flags:
        lines += [
            "",
            "## Flagged, not closed: one line near a multiple of an identified clock",
            "",
        ] + [
            f"- {x['id']} {x['target']} {x['freq_mhz']:.6f} MHz: {x['text']}"
            for x in flags
        ]
    text = "\n".join(lines) + "\n"
    out = ROOT / "report"
    out.mkdir(exist_ok=True)
    (out / "open_cases.md").write_text(text)
    print(text)


if __name__ == "__main__":
    main()
