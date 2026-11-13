"""Rank open cases. Each case gets features between 0 and 1 where higher
means "more like a steady signal from the target's direction": some computed
by code from the evidence bundle, some from Jev's answers. Version 1 combines
them with the fixed weights below. The rank orders the open list for a human;
it never opens or closes a case.

usage: python -m coldcase.rank backfill   ask Jev about every cached open case
       python -m coldcase.rank eval       how well each ranking separates the
                                          known injected signals from real ones
"""

import json
import math
import sys

from . import brief, jev, ledger, triage
from .investigate import code_for
from .remote import ROOT

W_CODE = {
    "on_scans": 0.27,
    "steady": 0.20,
    "off_quiet": 0.20,
    "strength": 0.13,
    "band_quiet": 0.13,
    "twins_quiet": 0.07,
}
W_JEV = {
    "not_human": 0.40,
    "band_free": 0.20,
    "unstructured": 0.20,
    "line_visible": 0.12,
    "off_clear": 0.08,
}
JEV_SHARE = 0.25  # weight of the Jev block in the combined rank


def _presence(b):
    snrs = [
        s["track"].get("snr") or 0
        for s in b["scans"]
        if s["role"] == "ON" and "error" not in s
    ]
    top = max(snrs, default=0)
    return [x >= 6 and x >= 0.15 * top for x in snrs], top


def code_features(b):
    pres, top = _presence(b)
    row = next((s for s in b["scans"] if s["pos"] == b["seed"]["seed_pos"]), {})
    tp = row.get("track_time_profile") or {}
    even = tp.get("even_share_would_be", 0.06)
    share = tp.get("largest_single_sample_share", even)
    st = b.get("off_scans_stacked") or {}
    span = max(1e-6, st.get("control_sum_max", 1) - st.get("control_sum_median", 0))
    off_excess = (
        (st.get("sum_track_snr", 0) - st.get("control_sum_median", 0)) / span
        if st
        else 0.5
    )
    near = (b.get("lookalikes_10khz") or {}).get("other_targets", 0)
    tw = (b.get("same_drift_twins") or {}).get("n_twins_also_in_off") or 0
    return {
        "on_scans": sum(pres) / max(1, len(pres)),
        "steady": 1 - min(1, max(0, (share - even) / max(1e-6, 0.5 - even))),
        "off_quiet": 1 - min(1, max(0, off_excess)),
        "strength": min(1, max(0, math.log10(max(top, 6) / 6) / math.log10(50 / 6))),
        "band_quiet": 1 / (1 + near / 5),
        "twins_quiet": 1 - min(1, tw / 5),
    }


def jev_features(a):
    if not a:
        return None
    f = {
        "not_human": 1 - a["human_made"] / 3,
        "band_free": 1 - a["band_explains"],
        "unstructured": 1 - a["structure"] / 3,
    }
    if "line_visible" in a:
        f["line_visible"] = a["line_visible"] / 3
        f["off_clear"] = 1 - a["off_visible"]
    return f


def _blend(feats, weights):
    used = {k: w for k, w in weights.items() if k in feats}
    return sum(feats[k] * w for k, w in used.items()) / sum(used.values())


def rank(bundle, jev_answers=None):
    """Returns code rank, Jev rank (None without answers) and the combined rank."""
    c = _blend(code_features(bundle), W_CODE)
    jf = jev_features(jev_answers)
    if jf is None:
        return {"code": round(c, 3), "jev": None, "combined": round(c, 3)}
    j = _blend(jf, W_JEV)
    j_text = _blend(
        {k: v for k, v in jf.items() if k not in ("line_visible", "off_clear")}, W_JEV
    )
    return {
        "code": round(c, 3),
        "jev": round(j, 3),
        "jev_text_only": round(j_text, 3),
        "combined": round((1 - JEV_SHARE) * c + JEV_SHARE * j, 3),
    }


def rank_case(case_id, con=None):
    b = json.loads(
        (ROOT / "cases" / "evidence" / case_id / "evidence.json").read_text()
    )
    return rank(b, jev.answers(case_id, con))


def labelled():
    """Open cases with a known kind: (case_id, kind, case_json_path)."""
    out = []
    for name in ("pilot", "holdout"):
        tp = ROOT / "cases" / name / "TRUTH.json"
        if tp.exists():
            for cid, t in json.loads(tp.read_text()).items():
                out.append((cid, t["kind"], ROOT / "cases" / name / f"{cid}.json"))
    con = ledger.connect()
    try:
        for cid, kind in con.execute("select case_id, kind from controls").fetchall():
            out.append((cid, kind, ROOT / "cases" / "controls" / f"{cid}.json"))
        for (cid,) in con.execute(
            "select case_id from queue where status in ('open', 'open_unread')"
        ).fetchall():
            out.append((cid, "real", ROOT / "cases" / "queue" / f"{cid}.json"))
    except Exception:
        pass
    keep = []
    for cid, kind, path in out:
        ev = ROOT / "cases" / "evidence" / cid / "evidence.json"
        if (
            ev.exists()
            and path.exists()
            and triage.decide(json.loads(ev.read_text()))["status"] == "open"
        ):
            keep.append((cid, kind, path))
    return keep


def backfill():
    cfg_cap = None
    try:
        from . import runner

        j = runner.config().get("jev", {})
        cfg_cap, use_plot = j.get("tokens_per_month"), j.get("use_plot", True)
    except Exception:
        use_plot = True
    if not jev.api_key():
        sys.exit("no TYPESAFE_API_KEY in .env; add it and run again")
    from typesafe_sdk import TypeSafeClient

    client = TypeSafeClient(api_key=jev.api_key(), model=jev.MODEL)
    keyp = ROOT / "cases" / "briefs" / "KEY.json"
    asked = 0
    for cid, kind, path in labelled():
        if not (ROOT / "cases" / "briefs" / code_for(cid) / "brief.json").exists():
            code, _ = brief.build(path)
            k = json.loads(keyp.read_text()) if keyp.exists() else {}
            k[code] = str(path)
            keyp.write_text(json.dumps(k, indent=1))
        try:
            a = jev.score_case(cid, client=client, use_plot=use_plot, cap=cfg_cap)
        except Exception as e:
            print(f"{cid} failed: {type(e).__name__}: {str(e)[:160]}")
            continue
        asked += a is not None
    print(
        f"asked Jev about {asked} cases; Jev tokens this month: {jev.month_tokens():,}"
    )


def _auc(pos, neg):
    """Chance that a random positive outranks a random negative."""
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def sonnet_rank(case_id):
    """Priority and suspicion from the two readers, as one number (or None)."""
    vals = []
    for role in ("investigator", "challenger"):
        p = ROOT / "cases" / "verdicts" / f"{code_for(case_id)}_{role}.json"
        if p.exists():
            v = json.loads(p.read_text())
            pr = {"high": 1.0, "medium": 0.6, "low": 0.2}.get(
                str(v.get("followup_priority") or "").lower()
            )
            cs = v.get("context_suspicion")
            if (
                v.get("verdict") in ("unresolved", "known_spacecraft")
                and pr is not None
            ):
                vals.append(pr - 0.3 * (cs if isinstance(cs, (int, float)) else 0.5))
            elif v.get("verdict"):
                vals.append(-0.5)  # the reader closed it
    return sum(vals) / len(vals) if vals else None


def evaluate():
    con = ledger.connect()
    rows = []
    for cid, kind, _ in labelled():
        r = rank_case(cid, con)
        rows.append((kind, cid, r, sonnet_rank(cid)))
    groups = {
        "sky": [x for x in rows if x[0].startswith("sky")],
        "real": [x for x in rows if x[0] == "real"],
        "leak": [x for x in rows if x[0] == "leak"],
    }
    print(
        "open cases with a known kind: "
        + ", ".join(f"{k} {len(v)}" for k, v in groups.items())
    )
    for name, get in (
        ("code features only", lambda x: x[2]["code"]),
        ("Jev only (with plot)", lambda x: x[2]["jev"]),
        ("Jev only (text)", lambda x: x[2].get("jev_text_only")),
        ("combined", lambda x: x[2]["combined"] if x[2]["jev"] is not None else None),
        ("Sonnet readers", lambda x: x[3]),
    ):
        line = f"  {name:22}"
        for neg in ("real", "leak"):
            p = [get(x) for x in groups["sky"] if get(x) is not None]
            n = [get(x) for x in groups[neg] if get(x) is not None]
            a = _auc(p, n)
            line += f"  sky above {neg}: " + (
                "n/a" if a is None else f"{a:.2f} ({len(p)} vs {len(n)})"
            )
        print(line)
    print(
        "  (1.00 = every injected sky-only signal ranks above every case of the other kind; "
        "0.50 = no better than chance)"
    )
    for k in ("sky", "real", "leak"):
        vals = sorted(x[2]["combined"] for x in groups[k])
        if vals:
            print(
                f"  combined rank, {k}: median {vals[len(vals) // 2]:.2f}, "
                f"range {vals[0]:.2f}-{vals[-1]:.2f}"
            )


if __name__ == "__main__":
    {"backfill": backfill, "eval": evaluate}[sys.argv[1]]()
