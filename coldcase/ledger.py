"""SQLite ledger: one row per case, evidence bundle and verdict.

usage: python -m coldcase.ledger verdict VERDICT_JSON
       python -m coldcase.ledger show [CASE_ID]
"""

import json
import sqlite3
import sys
from datetime import datetime, timezone

from .remote import ROOT

PATH = ROOT / "cases" / "ledger.sqlite"
VERDICTS = (
    "known_spacecraft",
    "rfi_identified",
    "rfi_probable",
    "instrumental",
    "not_recovered",
    "not_a_tone",
    "unresolved",
)
SCHEMA = """
create table if not exists cases(
  id text primary key, origin text, target text, site text,
  freq_mhz real, drift real, mjd real, status text, created text);
create table if not exists evidence(
  case_id text, created text, path text, summary text);
create table if not exists verdicts(
  case_id text, created text, agent text, role text, verdict text,
  confidence real, mechanism text, rationale text, evidence_refs text);
"""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(PATH)
    con.executescript(SCHEMA)
    return con


def add_case(con, case):
    con.execute(
        "insert or ignore into cases values (?,?,?,?,?,?,?,?,?)",
        (
            case["id"],
            case.get("origin"),
            case["target"],
            case.get("site"),
            case["freq_mhz"],
            case.get("drift"),
            case.get("mjd"),
            "new",
            _now(),
        ),
    )
    con.commit()


def add_evidence(con, case_id, path, summary):
    con.execute(
        "insert into evidence values (?,?,?,?)",
        (case_id, _now(), str(path), json.dumps(summary)),
    )
    con.execute("update cases set status='evidence' where id=?", (case_id,))
    con.commit()


def add_verdict(con, v):
    if v["verdict"] not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}")
    con.execute(
        "insert into verdicts values (?,?,?,?,?,?,?,?,?)",
        (
            v["case_id"],
            _now(),
            v.get("agent", ""),
            v["role"],
            v["verdict"],
            float(v["confidence"]),
            v.get("mechanism", ""),
            v["rationale"],
            json.dumps(v.get("evidence_refs", [])),
        ),
    )
    rows = con.execute(
        "select role, verdict from verdicts where case_id=?", (v["case_id"],)
    ).fetchall()
    latest = dict(rows)  # last verdict per role
    if {"investigator", "challenger"} <= set(latest):
        # both closing the case, or both leaving it open, counts as agreement;
        # anything else goes to a human
        fam = {v: "closed" for v in ("rfi_identified", "rfi_probable", "instrumental",
                                     "not_recovered", "not_a_tone")}
        fam.update({v: "open" for v in ("unresolved", "known_spacecraft")})
        a, b = latest["investigator"], latest["challenger"]
        agree = fam.get(a, a) == fam.get(b, b)
        con.execute("update cases set status=? where id=?",
                    ("adjudicated" if agree else "disputed", v["case_id"]))
    con.commit()


def main():
    con = connect()
    if sys.argv[1] == "verdict":
        with open(sys.argv[2]) as f:
            v = json.load(f)
        for item in v if isinstance(v, list) else [v]:
            if "case_id" not in item:  # blinded verdicts carry only the case code
                key = json.loads((ROOT / "cases" / "briefs" / "KEY.json").read_text())
                with open(key[item["case_code"]]) as cf:
                    item["case_id"] = json.load(cf)["id"]
            add_verdict(con, item)
        print("recorded")
    elif sys.argv[1] == "show":
        q = "select id, target, freq_mhz, status from cases"
        args = ()
        if len(sys.argv) > 2:
            q, args = q + " where id=?", (sys.argv[2],)
        for cid, target, f, status in con.execute(q + " order by created", args):
            print(f"{cid:10} {target:12} {f:12.6f} MHz  {status}")
            for role, verdict, conf, mech in con.execute(
                "select role, verdict, confidence, mechanism from verdicts "
                "where case_id=? order by created",
                (cid,),
            ):
                print(f"    {role:12} {verdict:18} conf={conf:.2f}  {mech}")


if __name__ == "__main__":
    main()
