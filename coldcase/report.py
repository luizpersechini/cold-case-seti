"""Summarise the queue: how cases were closed, and the open cases ranked for a
human. Writes report/open_cases.md and prints the same text.

usage: python -m coldcase.report
"""

import json
from collections import Counter

from . import ledger
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
        "Context suspicion | Plot |",
        "|---|---|---|---|---|---|---|---|",
    ]
    open_rows = []
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
            p = ROOT / "cases" / "verdicts"
            for path in p.glob(f"*_{role}.json"):
                v = json.loads(path.read_text())
                if v.get("case_id") == cid:
                    pr.append(
                        str(v.get("followup_priority", "")).lower()
                        + (
                            ""
                            if v.get("verdict") == "unresolved"
                            else f" ({v.get('verdict')})"
                        )
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
        open_rows.append(
            (
                best,
                -max(on, default=0),
                cid,
                target,
                f,
                d,
                on,
                " / ".join(pr) or "not read",
                cs,
            )
        )
    for best, _, cid, target, f, d, on, pr, cs in sorted(open_rows):
        lines.append(
            f"| {cid} | {target} | {f:.6f} | {d:+.3f} | {on} | {pr} | "
            f"{'-' if not cs else f'{sum(cs) / len(cs):.2f}'} | "
            f"cases/evidence/{cid}/waterfall.png |"
        )
    if not open_rows:
        lines.append("| none yet | | | | | | | |")
    text = "\n".join(lines) + "\n"
    out = ROOT / "report"
    out.mkdir(exist_ok=True)
    (out / "open_cases.md").write_text(text)
    print(text)


if __name__ == "__main__":
    main()
