"""Decide from an evidence bundle, by fixed rules and without a model, whether
a case is closed or stays open. A case is closed only on direct evidence:

  D1  the candidate itself is detected off-target (stacked OFF test more than
      1.0 above its control maximum, or one OFF scan 1.5 or more above its control maximum, or two
      or more OFF scans above their controls)
  D2  two or more clock-probe signals match a single mistuned oscillator
  D3  the same frequency (within 300 Hz) and drift toward another target
  D4  half or more of the track's power sits in one time sample: a burst
  D5  back at the same telescope frequency on another day, when the Earth's
      motion would have moved a sky signal well away (needs the repeat check,
      coldcase/repeat.py; it is not part of the first pass)

Context (band, drift, neighbours) never closes a case; readers use it to rank
the open ones.

usage: python -m coldcase.triage CASE_ID ...     (prints the decision)
"""

import json
import sys

from .remote import ROOT

OFF_MARGIN, BURST_SHARE, SAME_HZ, BURST_UNITS = 1.5, 0.5, 300.0, 100.0
# set 2026-11-12: bare stacked exceedances closed 2 of 59 sky-only injections
STACK_MARGIN = 1.0
# ten times SAME_HZ: below this shift the two readings cannot be told apart
REPEAT_MIN_SHIFT_HZ = 3000.0
CHANCE_MAX = 0.05


def repeat_rule(b, rp):
    """D5, from a repeat check (coldcase/repeat.py). A day counts when it could
    be measured and the Earth's motion would have moved a sky signal by at
    least REPEAT_MIN_SHIFT_HZ. It is a match when some scan of that day holds
    a signal within SAME_HZ of the event's telescope frequency, OFF_MARGIN or
    more above its control windows. In a crowded band an unrelated line lands
    there by chance: the chance per day is taken as in D3 (other targets with
    a similar-drift hit within 10 kHz, scaled to the window), and the rule
    needs the chance of this many matches in the days counted to be small.
    Returns (text or None, numbers)."""
    from math import comb

    days = [
        d
        for d in rp.get("days", [])
        if d.get("usable") and abs(d["shift_hz"]) >= REPEAT_MIN_SHIFT_HZ
    ]
    hits = [d for d in days if (d["same"].get("best_margin") or -1e9) >= OFF_MARGIN]
    near = (b.get("lookalikes_10khz") or {}).get("other_targets_similar_drift", 0)
    p = min(1.0, near * (2 * SAME_HZ / 20000.0))
    n, k = len(days), len(hits)
    tail = (
        sum(comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k, n + 1))
        if k
        else 1.0
    )
    stats = {
        "days_counted": n,
        "days_matched": k,
        "chance_per_day": round(p, 3),
        "chance_of_this_many_matches": round(tail, 4),
    }
    if not k or tail >= CHANCE_MAX:
        return None, stats
    best = max(hits, key=lambda d: d["same"]["best_margin"])
    return (
        f"D5: back at the same telescope frequency in {k} of {n} other observations "
        f"(MJD {best['mjd']:.2f}: +{best['same']['best_margin']} over controls), "
        "where the Earth's motion would have moved a sky signal by "
        f"{min(abs(d['shift_hz']) for d in hits) / 1e3:.1f} kHz or more; chance of "
        f"unrelated matches {tail:.3f}"
    ), stats


def decide(b, repeat=None):
    seed = b["seed"]
    if not seed.get("recovered"):
        # no tone at the catalogue position; say so more precisely when the
        # scan holds a strong burst, which is what makes such catalogue hits
        row = next((s for s in b["scans"] if s["pos"] == seed["seed_pos"]), {})
        bs = row.get("brightest_sample_in_cutout") or {}
        if bs.get("noise_units", 0) >= BURST_UNITS:
            return {
                "status": "closed",
                "verdict": "not_a_tone",
                "direct_evidence": [
                    f"D4: no tone at the catalogue position; the scan holds a burst of "
                    f"{bs['noise_units']:.0f} noise units at t = {bs['at_time_s']} s"
                ],
            }
        return {"status": "closed", "verdict": "not_recovered", "direct_evidence": []}
    direct = []
    offs = [s for s in b["scans"] if s["role"] == "OFF" and "error" not in s]
    st = b.get("off_scans_stacked") or {}
    if st.get("exceeds_all_controls") and (
        st["sum_track_snr"] - st["control_sum_max"] > STACK_MARGIN
    ):
        direct.append(
            f"D1: stacked OFF {st['sum_track_snr']} above control maximum "
            f"{st['control_sum_max']}"
        )
    margins = [
        (s["pos"], round(s["track"]["snr"] - s["control_windows"]["max_snr"], 1))
        for s in offs
        if s.get("control_windows") and s["track"].get("snr") is not None
    ]
    big = [m for m in margins if m[1] >= OFF_MARGIN]
    if big:
        direct.append(
            "D1: OFF scan margin over controls "
            + ", ".join(f"scan {p}: +{m}" for p, m in big)
        )
    if sum(1 for _, m in margins if m > 0) >= 2:
        direct.append("D1: two or more OFF scans above their controls")
    cp = b.get("clock_probes") or {}
    if cp.get("done") and cp.get("n_matching_one_oscillator", 0) >= 2:
        direct.append(
            f"D2: {cp['n_matching_one_oscillator']} probes match one oscillator "
            f"({cp['candidate_clock']['candidate_offset_ppm']} ppm off "
            f"{cp['candidate_clock']['clock_mhz']} MHz)"
        )
    f, d = seed["freq_mhz"], seed["drift"]
    same = [
        x
        for x in (b.get("lookalikes_1khz") or {}).get("strongest_other", [])
        if abs(x["freq_mhz"] - f) * 1e6 <= SAME_HZ
        and abs(x["drift"] - d) <= 0.05 + 0.25 * abs(d)
    ]
    if same:
        # In a crowded band a similar-drift hit lands within 300 Hz by chance.
        # Expected chance count: similar-drift targets within 10 kHz, scaled to
        # the 600 Hz window. D3 counts only when that is small.
        near = (b.get("lookalikes_10khz") or {}).get("other_targets_similar_drift", 0)
        chance = max(0, near - len({x["target"] for x in same})) * (
            2 * SAME_HZ / 20000.0
        )
        if chance < 0.05:
            direct.append(
                f"D3: same frequency and drift toward {len(same)} other target(s), "
                f"e.g. {same[0]['target']} on MJD {same[0]['mjd']}"
            )
    row = next((s for s in b["scans"] if s["pos"] == seed["seed_pos"]), {})
    share = (row.get("track_time_profile") or {}).get("largest_single_sample_share", 0)
    if share >= BURST_SHARE:
        direct.append(f"D4: {share:.0%} of the track's power is in one time sample")
    if repeat:
        text, _ = repeat_rule(b, repeat)
        if text:
            direct.append(text)
    if not direct:
        return {"status": "open", "verdict": "unresolved", "direct_evidence": []}
    kinds = {x[:2] for x in direct}
    verdict = (
        "rfi_identified"
        if "D2" in kinds
        else "not_a_tone"
        if kinds == {"D4"}
        else "rfi_probable"
    )
    return {"status": "closed", "verdict": verdict, "direct_evidence": direct}


def _round_clock(hz, tol=2e-4):
    """Name the round human clock rate a spacing matches, if any: a power of
    two in hertz, or 1, 2, 2.5 or 5 times a power of ten."""
    import math

    for n in range(10, 25):
        if abs(hz / 2**n - 1) <= tol:
            return f"2^{n} Hz"
    for e in range(3, 8):
        for m in (1, 2, 2.5, 5):
            if abs(hz / (m * 10**e) - 1) <= tol:
                return f"{m * 10**e / 1e3:g} kHz"
    return None


def family_comb(offsets_hz, tol_hz=120.0):
    """Do the lines of one event sit on a regular comb? offsets_hz: each
    line's distance from the strongest line. Returns the spacing that explains
    at least 80% of the lines (three at least), and the round clock rate it
    matches if any. A comb at a round clock rate is a human-made transmitter
    (D2), since hertz are a human unit."""
    offs = [o for o in offsets_hz if abs(o) > tol_hz]
    need = max(3, int(0.8 * len(offs) + 0.999))
    best = None
    for o in offs:
        for k in range(1, 25):
            sp = abs(o) / k
            if sp < 10_000:
                break
            m = [round(x / sp) for x in offs]
            ok = [
                (x, mi)
                for x, mi in zip(offs, m)
                if mi != 0 and abs(x - mi * sp) <= tol_hz
            ]
            if len(ok) >= need and (best is None or sp > best[0]):
                fit = sum(x * mi for x, mi in ok) / sum(mi * mi for _, mi in ok)
                best = (fit, len(ok))
    if best is None:
        return None
    return {
        "spacing_khz": round(best[0] / 1e3, 3),
        "lines_on_comb": best[1],
        "lines_tested": len(offs),
        "round_clock": _round_clock(best[0]),
    }


def decide_case(case_id):
    folder = ROOT / "cases" / "evidence" / case_id
    rp = folder / "repeat.json"
    return decide(
        json.loads((folder / "evidence.json").read_text()),
        repeat=json.loads(rp.read_text()) if rp.exists() else None,
    )


if __name__ == "__main__":
    for cid in sys.argv[1:]:
        print(cid, json.dumps(decide_case(cid)))
