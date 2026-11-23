"""Ask Jev (TypeSafe's System One model) what shape a case's plot shows.

A pilot, 2026-11-22. In November Jev was tried as a ranker of the open list
and dropped: it did no better than a score computed by code. Here it is
asked something narrower that code does badly: which of a few shapes the
waterfall plot shows. Jev returns one label with a probability for each.
Labels sort the open list for a person to review; they never open or close
a case, and they do not change the rank.

It is tested first on cases whose shape is known: injected test signals
(steady tones), and closed cases whose shape the measurements settle
(bursts, busy panels, wandering lines, no signal). Jev is kept only if it
names each shape correctly at least 80% of the time.

The key is read from .env (TYPESAFE_API_KEY=...) inside this process only.

usage: python -m coldcase.jev test [--per-class N] [--max-tokens N]
       python -m coldcase.jev open [--max-tokens N]     label the open events
"""

import json
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import ledger, readers, runner
from .remote import ROOT

MODEL = "jev-latest"
QSET = "shape-v1"
EV = ROOT / "cases" / "evidence"
SHAPES = {
    "steady tone": (
        "One narrow line, thin and continuous, crossing the ON panel at the place "
        "the red triangles mark, from the top of the panel to the bottom or for "
        "most of it. Its brightness may vary, but it is there in most rows."
    ),
    "brief burst": (
        "A short bright mark near the red triangles in an ON panel: bright in "
        "one or two rows only, with nothing or only faint signal in the other "
        "rows of that panel."
    ),
    "busy panel": (
        "The ON panel is crowded: many lines side by side, or bright horizontal "
        "bands that run across the whole width of the panel, so that no single "
        "line at the red triangles stands out from the rest."
    ),
    "wandering line": (
        "A clear line visible in several panels, including panels labelled OFF, "
        "whose slope changes from panel to panel or which runs away from where "
        "the red triangles predict it."
    ),
    "nothing distinct": (
        "Nothing at the red triangles stands out from the speckled background "
        "in any ON panel."
    ),
}


def api_key():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.strip().startswith("TYPESAFE_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    return None


def table(con):
    con.execute("""create table if not exists jev_labels(
        case_id text, qset text, label text, probs text, confidence real,
        model text, tokens int, ts text, primary key (case_id, qset))""")
    con.commit()


def known(per_class=40, seed=7):
    """Cases whose shape the measurements settle, per shape. Injected
    sky-only test signals are steady tones by construction; the others are
    closed queue cases. Returns {shape: [case_id]}."""
    con, led = runner.queue_db(), ledger.connect()
    out = defaultdict(list)
    for (cid,) in led.execute(
        "select case_id from controls where kind='sky_persistent' and recovered=1 "
        "and masked=0 and status='open' order by case_id"
    ):
        out["steady tone"].append(cid)
    for cid, note in con.execute(
        "select case_id, note from queue where status='closed'"
    ):
        if not (EV / cid / "waterfall.png").exists():
            continue
        b = json.loads((EV / cid / "evidence.json").read_text())
        s = b["seed"]
        if note == "not_recovered":
            out["nothing distinct"].append(cid)
            continue
        if not s.get("recovered"):
            continue
        row = next(r for r in b["scans"] if r["pos"] == s["seed_pos"])
        med = (row.get("control_windows") or {}).get("median_snr") or 0
        share = (row.get("track_time_profile") or {}).get(
            "largest_single_sample_share"
        ) or 0
        if note == "not_a_tone" and share >= 0.5 and med < 5:
            out["brief burst"].append(cid)
        elif med >= 8:
            out["busy panel"].append(cid)
        elif note == "rfi_probable" and abs(s["drift"]) >= 1:
            moved = 0
            for r in b["scans"]:
                if r["pos"] == s["seed_pos"] or "error" in r:
                    continue
                hs = [
                    h
                    for h in r.get("strongest_in_cutout") or []
                    if abs(h["offset_from_prediction_hz"]) < 1500 and h["snr"] >= 15
                ]
                if hs:
                    h = max(hs, key=lambda h: h["snr"])
                    if h["drift"] * s["drift"] < 0 or abs(
                        h["drift"] - s["drift"]
                    ) > 0.25 * abs(s["drift"]):
                        moved += 1
            if moved >= 2:
                out["wandering line"].append(cid)
    rng = random.Random(seed)
    for k in out:
        rng.shuffle(out[k])
        out[k] = sorted(out[k][:per_class])
    return dict(out)


def ask(case_id, client):
    """One label for one case. Returns (label, probabilities, confidence,
    tokens)."""
    from typesafe_sdk import Choice, Image

    b = json.loads((EV / case_id / "evidence.json").read_text())
    state = {
        "plot": Image(Path(EV / case_id / "waterfall.png")),
        "how_to_read_the_plot": (
            "Six panels, one per five-minute scan, in time order. Each panel shows "
            "frequency across (kHz from the candidate) and time down. Panels are "
            "labelled ON when the telescope pointed at the target star and OFF when "
            "it pointed elsewhere. Red triangles on each panel's top and bottom "
            "edges mark where the candidate is predicted to start and end in that "
            "scan; they are annotation, not data."
        ),
        "candidate_drift_hz_per_s": round(b["seed"]["drift"], 3),
    }
    q = {
        "shape": Choice(
            instructions=(
                "What does `plot` show at the place the red triangles mark, judging "
                "mainly from the ON panels?"
            ),
            criteria=SHAPES,
        )
    }
    resp = client.system_one(
        state=state, questions=q, extra_headers={"TypeSafe-Danger-Zone-Images": "true"}
    )
    a = resp.choices["shape"]
    usage = getattr(resp, "usage", None)
    tokens = int(getattr(usage, "input_tokens", 0) or 0) + int(
        getattr(usage, "output_tokens", 0) or 0
    )
    return (
        a.choice,
        dict(a.probabilities),
        float(a.confidence),
        tokens,
        str(getattr(resp, "model", MODEL)),
    )


def _record(con, case_id, label, probs, conf, model, tokens):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    con.execute(
        "insert or replace into jev_labels values (?,?,?,?,?,?,?,?)",
        (case_id, QSET, label, json.dumps(probs), conf, model, tokens, now),
    )
    con.commit()
    with open(readers.SPEND, "a") as fh:
        fh.write(
            json.dumps(
                {
                    "ts": now,
                    "code": case_id,
                    "role": "jev-shape",
                    "model": model,
                    "source": "jev",
                    "input_tokens": tokens,
                    "output_tokens": 0,
                    "cache_read": 0,
                    "cache_write": 0,
                    "total_tokens": tokens,
                }
            )
            + "\n"
        )


def label_all(ids, max_tokens, say=print):
    from typesafe_sdk import TypeSafeClient

    key = api_key()
    if not key:
        raise SystemExit("no TYPESAFE_API_KEY in .env")
    con = ledger.connect()
    table(con)
    used = 0
    out = {}
    with TypeSafeClient(api_key=key, model=MODEL) as client:
        for cid in ids:
            row = con.execute(
                "select label, probs, confidence from jev_labels where case_id=? and qset=?",
                (cid, QSET),
            ).fetchone()
            if row:
                out[cid] = (row[0], json.loads(row[1]), row[2])
                continue
            if used >= max_tokens:
                say(f"stopped at the token cap of {max_tokens:,}")
                break
            try:
                label, probs, conf, tokens, model = ask(cid, client)
            except Exception as e:
                say(f"{cid}: {type(e).__name__}: {str(e)[:200]}")
                break
            used += tokens
            _record(con, cid, label, probs, conf, model, tokens)
            out[cid] = (label, probs, conf)
    say(f"tokens used this run: {used:,}")
    return out


def test(per_class=40, max_tokens=2_000_000, say=print):
    sets = known(per_class)
    say("known shapes: " + ", ".join(f"{k} {len(v)}" for k, v in sets.items()))
    truth = {cid: k for k, v in sets.items() for cid in v}
    got = label_all(sorted(truth), max_tokens, say)
    conf = defaultdict(Counter)
    for cid, (label, _, _) in got.items():
        conf[truth[cid]][label] += 1
    names = list(SHAPES)
    say("\nknown shape -> Jev's label (counts); share right")
    say(" " * 18 + " ".join(f"{n[:11]:>11}" for n in names))
    ok_all = True
    for k in names:
        row = conf.get(k, Counter())
        n = sum(row.values())
        right = row[k] / n if n else float("nan")
        ok_all &= bool(n) and right >= 0.8
        say(
            f"{k:>17} "
            + " ".join(f"{row[x]:>11}" for x in names)
            + f"   {right:.0%} of {n}"
        )
    say(
        "\nkept: every shape right 80% of the time or more"
        if ok_all
        else "\nnot kept: some shape under 80%"
    )
    return conf


def main():
    args = sys.argv[1:]

    def opt(name, default):
        return type(default)(args[args.index(name) + 1]) if name in args else default

    if args[:1] == ["test"]:
        test(opt("--per-class", 40), opt("--max-tokens", 2_000_000))
    elif args[:1] == ["open"]:
        con = runner.queue_db()
        ids = [
            r[0]
            for r in con.execute(
                "select case_id from queue where status in ('open','open_unread') order by case_id"
            )
        ]
        got = label_all(ids, opt("--max-tokens", 1_000_000))
        for cid, (label, probs, conf) in sorted(got.items()):
            print(f"{cid}: {label} ({probs.get(label, 0):.2f})")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
