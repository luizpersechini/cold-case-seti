"""Add a measurement that was introduced after a case was investigated to its
stored evidence, from the cutouts already in the cache. Nothing is downloaded:
a case whose cutouts are gone is skipped and counted. It covers the queue, the
blind controls and the known cases.

Three measurements so far:

  track_without_strongest_sample   on the seed scan (investigate.without_peak);
                                   the second part of rule D4 reads it
  at_least_track, window_hz        in every scan's control_windows
                                   (investigate.control_ranks); the crowded
                                   part of rule D1 reads them
  followed_line                    the line followed from scan to scan
                                   (investigate.follow_line); the
                                   followed-line part of rule D1 reads it

usage: python -m coldcase.backfill [--apply]     without --apply it only counts
"""

import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from . import investigate, runner
from .remote import ROOT

FIELD = "track_without_strongest_sample"
RANK = "at_least_track"


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


def _has_ranks(b):
    return all(
        RANK in s["control_windows"]
        for s in b["scans"]
        if s.get("control_windows") and s["track"].get("snr") is not None
    )


def _rank_work(job):
    """One case, in a worker: its ranks, or why there are none."""
    case, path = job
    try:
        got = investigate.control_ranks(case, json.loads(Path(path).read_text()))
    except Exception as e:
        return f"{type(e).__name__}: {e}"[:160]
    return got


def ranks(apply=False, say=print):
    """at_least_track and window_hz for every scan of every recovered case
    that lacks them. A scan is left alone when the track measured now is not
    the stored one."""
    n = {"added": 0, "already there": 0, "not recovered": 0, "no cutout": 0}
    jobs = []
    for case, path in cases():
        if not path.exists():
            continue
        b = json.loads(path.read_text())
        if not b["seed"].get("recovered"):
            n["not recovered"] += 1
        elif _has_ranks(b):
            n["already there"] += 1
        else:
            jobs.append((case, str(path)))
    differ = 0
    with ProcessPoolExecutor(max_workers=8) as ex:
        for (case, path), got in zip(jobs, ex.map(_rank_work, jobs, chunksize=4)):
            if not isinstance(got, dict):
                if got is not None:
                    say(f"{case['id']}: {got}")
                n["no cutout"] += 1
                continue
            n["added"] += 1
            b = json.loads(Path(path).read_text())
            for s in b["scans"]:
                x = got.get(s["pos"])
                if not x or not s.get("control_windows"):
                    continue
                if abs(x["track_snr"] - s["track"]["snr"]) > 0.1:
                    differ += 1
                    continue
                s["control_windows"][RANK] = x[RANK]
                s["control_windows"]["window_hz"] = x["window_hz"]
            if apply:
                Path(path).write_text(json.dumps(b, indent=1))
    say(
        "ranks among the control windows: "
        + ", ".join(f"{k} {v}" for k, v in n.items())
        + (f"; {differ} scans left alone, their track differs" if differ else "")
        + ("" if apply else " (dry run)")
    )


def _follow_work(job):
    case, path = job
    try:
        b = json.loads(Path(path).read_text())
        got = investigate._cached_cuts(case, b)
        if got is None:
            return "no cutout"
        scans, cuts, _ = got
        seed = next(i for i, s in enumerate(scans) if s["pos"] == b["seed"]["seed_pos"])
        return investigate.follow_line(scans, cuts, seed, b["seed"])
    except Exception as e:
        return f"{type(e).__name__}: {e}"[:160]


def follow(apply=False, say=print):
    """followed_line (investigate.follow_line) for every recovered case that
    lacks it; the followed-line part of rule D1 reads it."""
    n = {"added": 0, "already there": 0, "not recovered": 0, "no cutout": 0}
    jobs = []
    for case, path in cases():
        if not path.exists():
            continue
        b = json.loads(path.read_text())
        if not b["seed"].get("recovered"):
            n["not recovered"] += 1
        elif "followed_line" in b:
            n["already there"] += 1
        else:
            jobs.append((case, str(path)))
    with ProcessPoolExecutor(max_workers=6) as ex:
        for (case, path), got in zip(jobs, ex.map(_follow_work, jobs, chunksize=4)):
            if not isinstance(got, dict):
                n["no cutout"] += 1
                if got not in (None, "no cutout"):
                    say(f"{case['id']}: {got}")
                continue
            n["added"] += 1
            if apply:
                b = json.loads(Path(path).read_text())
                b["followed_line"] = got
                Path(path).write_text(json.dumps(b, indent=1))
    say("line followed from scan to scan: " + ", ".join(f"{k} {v}" for k, v in n.items()) + ("" if apply else " (dry run)"))


def main(apply=False, say=print):
    if apply and runner.LOCK.exists():
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
    say(
        "track without its strongest sample: "
        + ", ".join(f"{k} {v}" for k, v in n.items())
        + ("" if apply else " (dry run)")
    )
    ranks(apply, say)
    follow(apply, say)


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
