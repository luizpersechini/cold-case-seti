"""Build the blinded brief an adjudicating agent reads: the evidence bundle
with names, ids, origins and URLs removed, plus a waterfall plot without them.
Uses cached cutouts only (no network).

usage: python -m coldcase.brief CASE_JSON [CASE_JSON ...]
"""

import hashlib
import json
import sys

from . import investigate, remote


code_for = investigate.code_for


def _scrub(obj, names):
    if isinstance(obj, dict):
        return {k: _scrub(v, names) for k, v in obj.items() if k not in ("url", "name")}
    if isinstance(obj, list):
        return [_scrub(v, names) for v in obj]
    if isinstance(obj, str):
        low = obj.lower()
        for n, label in names.items():
            if n and n.lower() in low:
                return label
    return obj


def build(case_path):
    with open(case_path) as fh:
        case = json.load(fh)
    ev_dir = remote.ROOT / "cases" / "evidence" / case["id"]
    bundle = json.loads((ev_dir / "evidence.json").read_text())
    code = code_for(case["id"])
    names, n_off = {}, 0
    for s in bundle["scans"]:
        label = "ON target" if s["role"] == "ON" else None
        if label is None:
            n_off += 1
            label = f"OFF pointing {n_off}"
        for key in ("source", "source_in_file"):
            if s.get(key) and s[key] not in names:
                names[s[key]] = label
    names[case["target"]] = "ON target"
    names[case["id"]] = code
    blind = _scrub(bundle, names)
    for sc in blind["scans"]:
        cw, tr = sc.get("control_windows"), sc.get("track", {})
        if cw and tr.get("snr") is not None:
            sc["track_vs_controls"] = {
                "margin_over_control_max": round(tr["snr"] - cw["max_snr"], 1),
                "empirical_p_floor": round(1 / (cw["n"] + 1), 3),
                "note": "with n controls the smallest measurable p is 1/(n+1); a track "
                        "barely above the control maximum is p of about that floor",
            }
    blind["case"] = {
        "code": code,
        "site": bundle["site"],
        "catalogue_freq_mhz": case["freq_mhz"],
        "catalogue_drift": case.get("drift"),
    }
    out = remote.ROOT / "cases" / "briefs" / code
    out.mkdir(parents=True, exist_ok=True)
    (out / "brief.json").write_text(json.dumps(blind, indent=1))
    ready = ev_dir / "waterfall_blind.png"
    if ready.exists():
        (out / "waterfall.png").write_bytes(ready.read_bytes())
    else:
        # blind plot from cached cutouts
        half = case.get("half_khz", max(5.0, abs(case.get("drift", 0.0)) * 2.5 + 3.0))
        scans, cuts = [], []
        n_off = 0
        for s in bundle["scans"]:
            if "error" in s or not s.get("url"):
                continue
            c = dict(remote.read_cutout(s["url"], case["freq_mhz"], half))
            n_off += s["role"] == "OFF"
            c["source_name"] = "target" if s["role"] == "ON" else f"pointing {n_off}"
            scans.append({"pos": s["pos"], "role": s["role"]})
            cuts.append(c)
        seed = next(
            i for i, s in enumerate(scans) if s["pos"] == bundle["seed"]["seed_pos"]
        )
        investigate.plot(
            {"id": code, "target": ""},
            scans,
            cuts,
            bundle["seed"],
            seed,
            out / "waterfall.png",
        )
    return code, out


def main():
    key = {}
    for p in sys.argv[1:]:
        code, out = build(p)
        key[code] = p
        print(code, "<-", p)
    key_path = remote.ROOT / "cases" / "briefs" / "KEY.json"
    old = json.loads(key_path.read_text()) if key_path.exists() else {}
    key_path.write_text(json.dumps({**old, **key}, indent=1))


if __name__ == "__main__":
    main()
