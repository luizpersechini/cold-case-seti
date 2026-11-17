"""Rank open cases. Each case gets features between 0 and 1 where higher
means "more like a steady signal from the target's direction", computed by
code from the evidence bundle and combined with the fixed weights below. The
rank orders the open list for a human; it never opens or closes a case.

Until November 2026 a second block of features came from Jev, a small
typed-judgment model that answered fixed questions about each open case. On
399 injected sky-only signals and 27 real open events it ranked no better
than the code features (0.80 against 0.84) and added 0.02 when combined, so
it was removed. Commit f0c8a04 holds that version.

usage: python -m coldcase.rank eval       how well each ranking separates the
                                          known injected signals from real ones
"""

import json
import math
import sys

from . import ledger, triage
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


def _blend(feats, weights):
    used = {k: w for k, w in weights.items() if k in feats}
    return sum(feats[k] * w for k, w in used.items()) / sum(used.values())


def rank(bundle):
    """The rank of one case, between 0 and 1."""
    return round(_blend(code_features(bundle), W_CODE), 3)


def rank_case(case_id):
    b = json.loads(
        (ROOT / "cases" / "evidence" / case_id / "evidence.json").read_text()
    )
    return rank(b)


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
    rows = []
    for cid, kind, _ in labelled():
        rows.append((kind, cid, rank_case(cid), sonnet_rank(cid)))
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
        ("code features", lambda x: x[2]),
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
        vals = sorted(x[2] for x in groups[k])
        if vals:
            print(
                f"  rank, {k}: median {vals[len(vals) // 2]:.2f}, "
                f"range {vals[0]:.2f}-{vals[-1]:.2f}"
            )


if __name__ == "__main__":
    {"eval": evaluate}[sys.argv[1]]()
