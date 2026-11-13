"""Score the phase 2 pilot against the hidden truth.

Code level (no readers needed): was each injection recovered, and what did
the OFF measurements say. Reader level: what the two readers concluded.

usage: python -m coldcase.score
"""

import json
from collections import Counter, defaultdict

from .investigate import code_for
from .remote import ROOT

INTERFERENCE = {"rfi_identified", "rfi_probable", "instrumental"}
KINDS = ("sky_persistent", "sky_intermittent", "leak", "real")


def outcome(inv, cha):
    """kept: both leave the case open (unresolved or known spacecraft).
    dismissed: both close it (interference, not recovered, not a tone).
    disputed: one of each, which goes to a human."""
    if inv is None or cha is None:
        return "not read"
    keep = {"unresolved", "known_spacecraft"}
    a, b = inv in keep, cha in keep
    return "kept" if a and b else "dismissed" if not a and not b else "disputed"


def main():
    truth = json.loads((ROOT / "cases" / "pilot" / "TRUTH.json").read_text())
    code_lvl, read_lvl = defaultdict(Counter), defaultdict(Counter)
    verdicts = defaultdict(Counter)
    tokens, n_read = 0, 0
    for cid, t in sorted(truth.items()):
        kind = t["kind"]
        ev = ROOT / "cases" / "evidence" / cid / "evidence.json"
        if not ev.exists():
            code_lvl[kind]["no evidence yet"] += 1
            continue
        b = json.loads(ev.read_text())
        code_lvl[kind]["cases"] += 1
        code_lvl[kind]["recovered"] += b["seed"]["recovered"]
        offs = [s for s in b["scans"] if s["role"] == "OFF" and "error" not in s]
        n_ex = sum(s["track_exceeds_all_controls"] for s in offs)
        st = b.get("off_scans_stacked") or {}
        code_lvl[kind]["one OFF scan above controls"] += n_ex == 1
        code_lvl[kind]["two or more OFF scans above controls"] += n_ex >= 2
        code_lvl[kind]["stacked OFF above controls"] += bool(
            st.get("exceeds_all_controls")
        )
        vs = {}
        for role in ("investigator", "challenger"):
            p = ROOT / "cases" / "verdicts" / f"{code_for(cid)}_{role}.json"
            if p.exists():
                v = json.loads(p.read_text())
                vs[role] = v.get("verdict")
                verdicts[(kind, role)][v.get("verdict")] += 1
                tokens += v.get("usage", {}).get("total_tokens", 0)
        if len(vs) == 2:
            n_read += 1
        read_lvl[kind][outcome(vs.get("investigator"), vs.get("challenger"))] += 1
    print("code level")
    for kind in KINDS:
        if code_lvl[kind]:
            print(
                f"  {kind:17} "
                + ", ".join(f"{k}={v}" for k, v in code_lvl[kind].items())
            )
    print("\nreader level (case outcome)")
    for kind in KINDS:
        if read_lvl[kind]:
            print(
                f"  {kind:17} "
                + ", ".join(f"{k}={v}" for k, v in read_lvl[kind].items())
            )
    print("\nverdicts by reader")
    for (kind, role), c in sorted(verdicts.items()):
        print(f"  {kind:17} {role:12} " + ", ".join(f"{k}={v}" for k, v in c.items()))
    sky = read_lvl["sky_persistent"] + read_lvl["sky_intermittent"]
    n_sky = sum(v for k, v in sky.items() if k != "not read")
    if n_sky:
        print(
            f"\nfalse dismissal (injected sky-only signals both readers call interference): "
            f"{sky['dismissed']} of {n_sky}; disputed {sky['disputed']}; kept {sky['kept']}"
        )
    lk = read_lvl["leak"]
    n_lk = sum(v for k, v in lk.items() if k != "not read")
    if n_lk:
        print(
            f"false alarm (injected leaks both readers keep as unresolved): "
            f"{lk['kept']} of {n_lk}; disputed {lk['disputed']}; dismissed {lk['dismissed']}"
        )
    if n_read:
        print(
            f"tokens per case, both readers: {tokens / n_read:,.0f} (over {n_read} cases)"
        )


if __name__ == "__main__":
    main()
