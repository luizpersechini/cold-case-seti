"""Ask Jev (TypeSafe's System One model) a fixed set of narrow questions about
an open case. Jev returns typed answers with probabilities and no prose; the
answers are stored and combined in code (see rank.py). They only help order
the open list. They never open or close a case.

Numeric facts are left to code. Jev is asked only what needs reading: what
the band's listed use implies, how structured the surroundings look, and what
the plot shows.

The key is read from .env (TYPESAFE_API_KEY=...) inside this process only.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from . import ledger, readers
from .investigate import code_for
from .remote import ROOT

QSET = "v1"
MODEL = "jev-latest"


def api_key():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.strip().startswith("TYPESAFE_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    return None


def table(con):
    con.execute("""create table if not exists jev_answers(
        case_id text, qset text, qid text, kind text, value real, confidence real,
        probs text, model text, ts text, primary key (case_id, qset, qid))""")
    con.commit()


def month_tokens():
    if not readers.SPEND.exists():
        return 0
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    return sum(
        r["total_tokens"]
        for r in map(
            json.loads, filter(str.strip, readers.SPEND.read_text().splitlines())
        )
        if r.get("source") == "jev" and r["ts"].startswith(month)
    )


def questions(use_plot=True):
    from typesafe_sdk import Noul, Score

    q = {
        "band_explains": Noul(
            instructions=(
                "According to `brief.allocation`, is this frequency band used by transmitters "
                "that move across the sky or switch on and off, such as satellites or aircraft, "
                "so that the band's use alone could explain a signal seen only while the "
                "telescope points at its target?"
            )
        ),
        "structure": Score(
            instructions=(
                "How structured is the emission around the candidate, judging from "
                "`brief.family`, `brief.same_drift_twins` and `brief.neighbourhood`?"
            ),
            criteria=[
                "A single isolated narrow line with nothing related nearby.",
                "A few other lines nearby with no clear relation to the candidate.",
                "Several lines that share the candidate's drift or switch on in the same scans.",
                "A regular group of many lines that clearly belongs to one transmitter.",
            ],
        ),
        "crowded": Score(
            instructions=(
                "How busy is this part of the spectrum with signals toward other targets, "
                "judging from `brief.lookalikes_1khz` and `brief.lookalikes_10khz`?"
            ),
            criteria=[
                "Quiet: almost no signals toward any other target.",
                "Some unrelated signals toward a few other targets.",
                "Busy: signals toward many other targets on several days.",
                "Saturated: signals toward dozens of targets on many days.",
            ],
        ),
        "human_made": Score(
            instructions=(
                "Taking everything in `brief` together, how strong is the case that the "
                "candidate comes from a human-made transmitter and not from the direction "
                "of the target?"
            ),
            criteria=[
                "Nothing in the measurements points to a human-made transmitter.",
                "Weak hints only, such as a band that has some human use.",
                "Several independent hints, none of them decisive.",
                "Strong: the pattern matches a known kind of human transmitter.",
            ],
        ),
    }
    if use_plot:
        q["line_visible"] = Score(
            instructions=(
                "`plot` shows frequency across and time down for each scan; panels are "
                "labelled ON or OFF, and red triangles on the panel edges mark where the "
                "candidate is predicted. How clearly can a narrow line be seen at that "
                "position in the ON panels?"
            ),
            criteria=[
                "No line can be seen in any ON panel.",
                "A faint line can just be made out in one ON panel.",
                "A clear line is visible in one ON panel only.",
                "A clear line is visible in two or more ON panels.",
            ],
        )
        q["off_visible"] = Noul(
            instructions=(
                "In `plot`, is a narrow line visible in any panel labelled OFF at the position "
                "marked by the red triangles on that panel's edges?"
            )
        )
    return q


def score_case(case_id, client=None, use_plot=True, cap=None):
    """Ask the question set for one case that has a blinded brief. Returns the
    answers as {qid: value}, or None when nothing was asked (no key, no brief,
    over the cap, or already answered)."""
    con = ledger.connect()
    table(con)
    if con.execute(
        "select 1 from jev_answers where case_id=? and qset=?", (case_id, QSET)
    ).fetchone():
        return None
    folder = ROOT / "cases" / "briefs" / code_for(case_id)
    if not (folder / "brief.json").exists():
        return None
    if cap is not None and month_tokens() + 15_000 > cap:
        return None
    if client is None:
        key = api_key()
        if not key:
            return None
        from typesafe_sdk import TypeSafeClient

        client = TypeSafeClient(api_key=key, model=MODEL)
    state = {"brief": json.loads((folder / "brief.json").read_text())}
    plot = folder / "waterfall.png"
    use_plot = use_plot and plot.exists()
    headers = None
    if use_plot:
        from typesafe_sdk import Image

        state["plot"] = Image(Path(plot))
        headers = {"TypeSafe-Danger-Zone-Images": "true"}
    resp = client.system_one(
        state=state, questions=questions(use_plot), extra_headers=headers
    )
    usage = getattr(resp, "usage", None)
    tokens = int(getattr(usage, "input_tokens", 0) or 0)
    with open(readers.SPEND, "a") as fh:
        fh.write(
            json.dumps(
                {
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "code": code_for(case_id),
                    "role": "jev",
                    "model": str(getattr(resp, "model", MODEL)),
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
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = {}
    for qid, a in resp.nouls.items():
        out[qid] = float(a.noul)
        con.execute(
            "insert or replace into jev_answers values (?,?,?,?,?,?,?,?,?)",
            (
                case_id,
                QSET,
                qid,
                "noul",
                float(a.noul),
                None,
                None,
                str(getattr(resp, "model", MODEL)),
                now,
            ),
        )
    for qid, a in resp.scores.items():
        out[qid] = float(a.score)
        con.execute(
            "insert or replace into jev_answers values (?,?,?,?,?,?,?,?,?)",
            (
                case_id,
                QSET,
                qid,
                "score",
                float(a.score),
                None if a.confidence is None else float(a.confidence),
                json.dumps({str(k): v for k, v in (a.probabilities or {}).items()}),
                str(getattr(resp, "model", MODEL)),
                now,
            ),
        )
    con.commit()
    return out


def answers(case_id, con=None):
    con = con or ledger.connect()
    table(con)
    return dict(
        con.execute(
            "select qid, value from jev_answers where case_id=? and qset=?",
            (case_id, QSET),
        ).fetchall()
    )
