"""Add a measurement that was introduced after a case was investigated to its
stored evidence, from the cutouts already in the cache. Nothing is downloaded:
a case whose cutouts are gone is skipped and counted. It covers the queue, the
blind controls and the known cases.

So far one measurement: track_without_strongest_sample on the seed scan (see
investigate.without_peak), which the second part of rule D4 reads.

usage: python -m coldcase.backfill [--apply]     without --apply it only counts
"""

import json
import sys

from . import investigate, runner
from .remote import ROOT

FIELD = "track_without_strongest_sample"


def cases():
    """(case, path of its evidence) for every case with stored evidence."""
    ev = ROOT / "cases" / "evidence"
    con = runner.queue_db()
    for (cid,) in con.execute("select case_id from queue order by case_id").fetchall():
        yield runner._case_from_queue(con, cid), ev / cid / "evidence.json"
    for folder in ("controls", "known"):
        for p in sorted((ROOT / "cases" / folder).glob("*.json")):
            case = json.loads(p.read_text())
            yield case, ev / case["id"] / "evidence.json"


def main(apply=False, say=print):
    if runner.LOCK.exists():
        raise SystemExit("a runner batch is in progress; try again when it ends")
    n = {"added": 0, "already there": 0, "not recovered": 0, "no cutout": 0}
    for case, path in cases():
        if not path.exists():
            continue
        b = json.loads(path.read_text())
        if not b["seed"].get("recovered"):
            n["not recovered"] += 1
            continue
        row = next(s for s in b["scans"] if s["pos"] == b["seed"]["seed_pos"])
        if FIELD in row:
            n["already there"] += 1
            continue
        try:
            got = investigate.backfill_without_peak(case, b)
        except Exception as e:
            say(f"{case['id']}: {type(e).__name__}: {e}")
            got = None
        if got is None:
            n["no cutout"] += 1
            continue
        n["added"] += 1
        if apply:
            row[FIELD] = got
            path.write_text(json.dumps(b, indent=1))
    say(", ".join(f"{k} {v}" for k, v in n.items()) + ("" if apply else " (dry run)"))


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
