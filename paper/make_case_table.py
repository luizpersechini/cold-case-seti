"""The outcome of every case, as a CSV that accompanies the paper.

usage: uv run python paper/make_case_table.py
Writes paper/cases.csv from the ledger (read-only). Drift is the hit table's
value, with the hit table's sign.
"""

import csv
import json
from pathlib import Path

from coldcase import ledger

OUT = Path(__file__).resolve().parent / "cases.csv"


def klass(status, verdict, refs):
    t = " | ".join(refs)
    if status == "no_data":
        return "no public data", ""
    if status != "closed":
        return "not explained", ""
    if verdict == "not_recovered":
        return "not reproduced", "no line at the catalogue position"
    if verdict == "not_a_tone":
        if t.startswith("D8"):
            return "not a steady tone", "D8: companion is a burst"
        if "no tone at the catalogue position" in t:
            return "not reproduced", "no line; a strong burst in the cutout"
        if "nothing is left of the track" in t:
            return "not a steady tone", "D4: nothing left without the strongest sample"
        return "not a steady tone", "D4: half or more of the power in one sample"
    rule = refs[0].split(":")[0] if refs else ""
    if rule == "D8" and ("sideband" in t or "carrier" in t):
        return "carrier with sidebands", "D8"
    return "shown to be local", rule


def main():
    led = ledger.connect()
    rows = []
    for cid, tgt, f, d, mjd, snr, status in led.execute(
        "select case_id, target, freq_mhz, drift, mjd, snr, status from queue order by case_id"
    ):
        v = led.execute(
            "select verdict, evidence_refs from verdicts where case_id = ? and agent = 'code' "
            "order by created desc limit 1",
            [cid],
        ).fetchone()
        verdict, refs = (v[0], json.loads(v[1]) if v[1] else []) if v else ("", [])
        st = "closed" if status == "closed" else "no_data" if status == "no_data" else "open"
        k, rule = klass(st, verdict, refs)
        rows.append(["near-miss", cid, tgt, f"{f:.6f}", f"{d:.4f}", f"{mjd:.5f}", f"{snr:.1f}", k, rule,
                     refs[0] if st == "closed" and refs else ""])
    for cid, tgt, f, d, mjd, snr, status, verdict, why in led.execute(
        "select case_id, target, freq_mhz, drift, mjd, snr, status, verdict, why from followup order by case_id"
    ):
        refs = json.loads(why) if why else []
        k, rule = klass(status, verdict, refs)
        rows.append(["flagged", cid, tgt, f"{f:.6f}", f"{d:.4f}", f"{mjd:.5f}", f"{snr:.1f}", k, rule,
                     refs[0] if status == "closed" and refs else ""])
    with open(OUT, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["set", "case", "target", "freq_mhz", "listed_drift_hz_s", "mjd", "listed_snr",
                    "class", "rule", "evidence"])
        w.writerows(rows)
    import collections
    for s in ("near-miss", "flagged"):
        print(s, dict(collections.Counter(r[7] for r in rows if r[0] == s)))


if __name__ == "__main__":
    main()
