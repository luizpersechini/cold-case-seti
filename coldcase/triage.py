"""Decide from an evidence bundle, by fixed rules and without a model, whether
a case is closed or stays open. A case is closed only on direct evidence:

  D1  the candidate itself is detected off-target (stacked OFF test more than
      1.0 above its control maximum, or one OFF scan 1.5 or more above its control maximum, or two
      or more OFF scans above their controls; or a line at its drift within
      300 Hz of the predicted place among an OFF scan's blind detections,
      when an unrelated line that close is unlikely: see off_wide; or an OFF
      track of S/N 8 or more closer to the predicted frequency than the
      lines in its control windows make likely: see off_crowded)
  D2  two or more clock-probe signals match a single mistuned oscillator
  D3  the same frequency (within 300 Hz) and drift toward two or more other
      targets (one other target is only flagged: on injected signals a single
      weak catalogue hit landed that close about once in a hundred)
  D4  half or more of the track's power sits in one time sample: a burst;
      or nothing is left of the track above its control windows when its
      strongest time sample is taken out: see flash
  D5  back at the same telescope frequency on another day, when the Earth's
      motion would have moved a sky signal well away (needs the repeat check,
      coldcase/repeat.py; it is not part of the first pass)
  D6  two or more lines of one scan sit at different whole multiples of one
      clock rate, all the same fraction off nominal: harmonics of a single
      oscillator (coldcase/harmonics.py; it compares events, so it runs
      over the open list and not inside decide())
  D7  the same line, at the same telescope frequency, while the telescope
      pointed at other stars within half an hour (needs the session check,
      coldcase/session.py; it is not part of the first pass)
  D8  lines elsewhere in the band that share the event's scans, drift and
      moment-to-moment variation (companions, coldcase/companions.py) are
      one emitter with it, and a member of that emitter is shown to be local:
      measured as a case of its own, a companion is closed by D1, D4 or D7;
      or two companions and the event are spaced equally to within the
      measurement's accuracy, as a carrier and its sidebands are. A plain
      companion is only flagged (needs the companion measurement; it is not
      part of the first pass)

Context (band, drift, neighbours) never closes a case; readers use it to rank
the open ones. The satellite check (coldcase/satellites.py) is context too: a
payload passing close to the line of sight when the signal peaked is flagged.
Over the 52 open events checked on 2026-11-18, two had a pass rarer than 5%,
where chance alone gives two or three, and both payloads were long dead. On
the tracks measured again, 2026-11-19: seven of 70, where chance gives three
or four; three of those payloads are listed as working.

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
# set 2026-11-17: with one star, D3 closed 8 of 888 recovered sky-only controls
# and was the only reason for 1 of 126 real tones, which is the chance rate
D3_MIN_STARS = 2
# D6: clock rates tried (whole MHz), how far a crystal may sit from nominal,
# and how closely the lines of one scan must agree on that offset
CLOCKS_MHZ = tuple(range(4, 51))
CLOCK_PPM_MAX = 200.0
CLOCK_AGREE_PPM = 0.3
# D1, wide part. Weakest blind detection that counts (the blind search keeps
# nothing below 8), and the least chance per OFF scan that an unrelated line
# sits within SAME_HZ of a given place. Set 2026-11-17 on half of the sky-only
# controls: a line of any drift lay that close to the injected position in 7
# of 1,290 OFF scans; 1% is the 95% upper limit of that rate.
WIDE_SNR_MIN = 8.0
WIDE_BASE = 0.01
# D4, second part: the largest share of its S/N a track may keep without its
# strongest time sample and still count as a flash. Set 2026-11-17 on half of
# the sky-only controls, where no steady tone kept less than 0.82 of it (0.74
# in the other half).
FLASH_KEEP = 0.70
# D7: the least chance per scan of another star that a given position holds an
# unrelated line standing OFF_MARGIN above its control windows. Measured
# 2026-11-18 over the 53 open events (`python -m coldcase.session --rate`):
# 25 such lines in 7,820 control windows of 170 scans, 0.0032. The floor is
# kept at three times that; the same events close with either value.
SESSION_BASE = 0.01


def flash(row):
    """D4, second part, on the seed scan's row: is anything left of the track
    when its strongest time sample is taken out? It is a flash when the track
    stood above its control windows on the whole scan, does not without that
    sample, and kept less than FLASH_KEEP of its S/N. A track that loses that
    much but still stands above its controls varies strongly and is flagged.
    Returns (text or None, flag or None)."""
    w = row.get("track_without_strongest_sample")
    if not w or w["share_of_full_snr"] >= FLASH_KEEP:
        return None, None
    change = f"S/N {w['full_snr']} on the whole scan, {w['snr']} without it"
    if w["full_snr"] > w["full_control_max_snr"] and w["snr"] <= w["control_max_snr"]:
        return (
            f"D4: nothing is left of the track without its strongest time sample "
            f"({change}, against {w['control_max_snr']} in its control windows): a flash"
        ), None
    return None, (
        f"the track keeps {w['share_of_full_snr']:.0%} of its S/N without its "
        f"strongest time sample ({change}); steady test signals kept 74% or more"
    )


def off_wide(b, floor=WIDE_SNR_MIN):
    """D1, wide part: is the candidate among the blind detections of an OFF
    scan? The forced track only looks within tens of Hz of the predicted
    place, so a line that sits a few hundred Hz away there is missed. A scan
    counts when its blind detections hold a line within SAME_HZ of the
    prediction at the candidate's drift. An unrelated line lands that close
    by chance: the chance per scan is taken from the lines of that drift seen
    farther away in the OFF cutouts, and never below WIDE_BASE; the rule
    needs the chance of this many scans to be small. Returns (text or None,
    numbers)."""
    from math import comb

    seed = b["seed"]
    d = seed["drift"]
    tol = 0.05 + 0.25 * abs(d)
    case = b.get("case") or {}
    half_khz = case.get("half_khz") or max(5.0, abs(case.get("drift", d)) * 2.5 + 3.0)
    width = 2e3 * half_khz - 2 * SAME_HZ
    near, far, n = [], 0, 0
    for s in b["scans"]:
        if s["role"] != "OFF" or "error" in s:
            continue
        n += 1
        best = None
        for h in s.get("strongest_in_cutout") or []:
            if abs(h["drift"] - d) > tol:
                continue
            if abs(h["offset_from_prediction_hz"]) > SAME_HZ:
                far += 1
            elif h["snr"] >= floor and (best is None or h["snr"] > best["snr"]):
                best = h
        if best:
            near.append((s["pos"], best))
    k = len(near)
    p = min(1.0, max(WIDE_BASE, far * 2 * SAME_HZ / (n * width))) if n else 1.0
    tail = (
        sum(comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k, n + 1))
        if k
        else 1.0
    )
    stats = {
        "off_scans": n,
        "off_scans_with_line": k,
        "same_drift_lines_farther_away": far,
        "chance_per_scan": round(p, 3),
        "chance_of_this_many": round(tail, 4),
    }
    if not k or tail >= CHANCE_MAX:
        return None, stats
    pos, h = max(near, key=lambda x: x[1]["snr"])
    return (
        f"D1: a line at the candidate's drift within {SAME_HZ:.0f} Hz of the "
        f"predicted place in {k} of {n} OFF scans (scan {pos}: S/N {h['snr']} at "
        f"{h['offset_from_prediction_hz']:+.0f} Hz); chance of an unrelated line "
        f"{tail:.3f}"
    ), stats


def off_crowded(b, floor=WIDE_SNR_MIN):
    """D1, crowded part: is the line found at the predicted place of an OFF
    scan the candidate, or an unrelated line? The other parts of D1 ask the
    OFF track to beat its strongest control window. Where those windows hold
    lines of their own (the other teeth of a comb the candidate belongs to,
    say) it cannot, however plainly it is there. This part counts an OFF scan
    whose track has S/N of floor or more and asks how likely an unrelated
    line that strong is to sit that close to the predicted frequency: the
    lines at least as strong per hertz of control windows, plus one for the
    one found, times the stretch of frequency as close as it was found. The
    same unrelated line may be there in every scan, so scans are not
    multiplied: the chance is the largest of the counted scans', times one
    more than the number of OFF scans without the line. Returns (text or
    None, numbers)."""
    n, near = 0, []
    for s in b["scans"]:
        cw = s.get("control_windows") or {}
        if s["role"] != "OFF" or "error" in s or cw.get("at_least_track") is None:
            continue
        n += 1
        tr = s["track"]
        if tr.get("snr") is None or tr["snr"] < floor:
            continue
        close_hz = 2 * abs(tr["offset_from_prediction_hz"]) + s["chan_hz"]
        p = min(
            1.0, (cw["at_least_track"] + 1) * close_hz / (cw["n"] * cw["window_hz"])
        )
        near.append((p, s["pos"], tr, cw))
    k = len(near)
    stats = {"off_scans": n, "off_scans_with_line": k, "chance": None}
    if not k:
        return None, stats
    p, pos, tr, cw = max(near, key=lambda x: x[0])
    stats["chance"] = chance = round(min(1.0, (n - k + 1) * p), 4)
    stats["text"] = (
        f"a line of S/N {floor:.0f} or more at the predicted place in {k} of {n} OFF "
        f"scans (scan {pos}: S/N {tr['snr']} at {tr['offset_from_prediction_hz']:+.1f} "
        f"Hz, where {cw['at_least_track']} of {cw['n']} control windows hold one as "
        f"strong); chance of an unrelated line that close {chance:.3f}"
    )
    if chance >= CHANCE_MAX:
        return None, stats
    return "D1: " + stats["text"], stats


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


def session_scans(sess):
    """The scans of a session check that count: read, measured against at
    least two control windows, and pointed a degree or more from the event."""
    return [
        r
        for r in sess.get("scans", [])
        if "error" not in r
        and r.get("margin_over_control_max") is not None
        and (r.get("controls") or {}).get("second_snr") is not None
        and (r.get("separation_deg") or 0) >= 1.0
    ]


def session_rule(b, sess):
    """D7, from a session check (coldcase/session.py). A scan of another star
    is a match when it holds a signal within SAME_HZ of the event's telescope
    frequency, at its drift, OFF_MARGIN or more above its control windows. An
    unrelated line does that by chance: the chance per scan is the share of
    this event's scans whose strongest control window leads the others by the
    same margin, spread over the windows, and never below SESSION_BASE. The
    rule needs the chance of this many matches to be small. Returns (text or
    None, numbers)."""
    from math import comb

    rows = session_scans(sess)
    hits = [r for r in rows if r["margin_over_control_max"] >= OFF_MARGIN]
    n, k = len(rows), len(hits)
    lead = sum(
        1
        for r in rows
        if r["controls"]["max_snr"] - r["controls"]["second_snr"] >= OFF_MARGIN
    )
    windows = sum(r["controls"]["n"] for r in rows)
    p = min(1.0, max(SESSION_BASE, lead / windows)) if windows else 1.0
    tail = (
        sum(comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k, n + 1))
        if k
        else 1.0
    )
    stats = {
        "scans_counted": n,
        "scans_matched": k,
        "chance_per_scan": round(p, 4),
        "chance_of_this_many": round(tail, 4),
    }
    if not k or tail >= CHANCE_MAX:
        return None, stats
    best = max(hits, key=lambda r: r["margin_over_control_max"])
    return (
        f"D7: the same line toward other stars in {k} of {n} scans within half an "
        f"hour (e.g. {best['source']}, {best['minutes_from_event']:+.0f} min: "
        f"+{best['margin_over_control_max']} over controls at "
        f"{best['offset_hz']:+.0f} Hz); chance of unrelated lines {tail:.3f}"
    ), stats


def satellite_rule(sat):
    """From a satellite check (coldcase/satellites.py): a flag, not evidence.
    The check gives the smallest angle between the line of sight and any
    payload in the window around the signal's peak, and the share of other
    windows of the same night in which some payload came at least that
    close. The flag is raised when that share is small. Returns (text or
    None, numbers)."""
    null, near = sat.get("null") or {}, sat.get("closest") or []
    share = null.get("share_this_close")
    if share is None or not near or null.get("windows", 0) < 50:
        return None, {}
    c = near[0]
    stats = {
        "closest_deg": c["separation_deg"],
        "share_this_close": share,
        "windows": null["windows"],
    }
    if share >= CHANCE_MAX:
        return None, stats
    return (
        f"the payload {c['name']} (catalogue number {c['norad']}) passed "
        f"{c['separation_deg']} deg from the line of sight {c['seconds_from_peak']:+.0f} s "
        f"from the signal's peak; a payload came that close in {share:.1%} of "
        f"{null['windows']} other times that night. Not counted as evidence: the "
        "catalogue does not say whether it was still working"
    ), stats


# D8, first part: two spacings count as equal when they differ by no more than
# this many channels of the seed scan, plus a few hertz for the drift
SIDEBAND_CHANNELS = 3


def companion_rule(b, comp):
    """D8, from a companion measurement (coldcase/companions.py). comp: the
    record companions.py writes, with "leads" and, once measured, "members"
    (each companion as a case of its own). The event closes when a member is
    closed by D1, D4 or D7, or when two companions and the event make three
    lines spaced equally to within the measurement's accuracy: a carrier and
    its sidebands. Returns (text or None, flag or None)."""
    f0 = b["seed"]["freq_mhz"]
    found = [x for x in comp.get("leads", []) if x.get("outcome") == "companion"]
    if not found:
        return None, None
    for m in comp.get("members", []):
        kinds = {x[:2] for x in m.get("direct_evidence", [])}
        if m.get("status") == "closed" and kinds & {"D1", "D4", "D7"}:
            return (
                f"D8: its companion at {m['freq_mhz']:.6f} MHz, which rises and falls "
                f"with it, is itself closed: {'; '.join(m['direct_evidence'])}"
            ), None
    seed_row = next((s for s in b["scans"] if s["pos"] == b["seed"]["seed_pos"]), {})
    tol = SIDEBAND_CHANNELS * seed_row.get("chan_hz", 3.0) + 2.0
    lines = sorted([f0] + [x["freq_mhz"] for x in found])
    for i in range(len(lines)):
        for j in range(i + 1, len(lines)):
            for k in range(j + 1, len(lines)):
                if f0 not in (lines[i], lines[j], lines[k]):
                    continue
                a, c = (lines[j] - lines[i]) * 1e6, (lines[k] - lines[j]) * 1e6
                if abs(a - c) <= tol:
                    return (
                        f"D8: with two lines that rise and fall with it, it forms three "
                        f"lines spaced equally to within {tol:.0f} Hz ({a / 1e3:.3f} and "
                        f"{c / 1e3:.3f} kHz apart): a carrier and its sidebands"
                    ), None
    return None, (
        f"{len(found)} line(s) elsewhere in the band rise and fall with it: "
        + ", ".join(
            f"{x['freq_mhz']:.6f} MHz (r = {x['correlation_with_event']})" for x in found
        )
    )


def decide(b, repeat=None, session=None, satellites=None, companions=None):
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
    flags = []
    text, ws = off_wide(b)
    if text:
        direct.append(text)
    elif ws["off_scans_with_line"]:
        flags.append(
            f"a line at the candidate's drift within {SAME_HZ:.0f} Hz of the predicted "
            f"place in {ws['off_scans_with_line']} of {ws['off_scans']} OFF scans, but "
            f"{ws['same_drift_lines_farther_away']} more such lines farther away put "
            f"the chance of an unrelated line at {ws['chance_of_this_many']:.2f}"
        )
    text, cs = off_crowded(b)
    if text:
        direct.append(text)
    elif cs["off_scans_with_line"]:
        flags.append(cs["text"])
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
        stars = sorted({x["target"] for x in same})
        near = (b.get("lookalikes_10khz") or {}).get("other_targets_similar_drift", 0)
        chance = max(0, near - len(stars)) * (2 * SAME_HZ / 20000.0)
        if chance < 0.05 and len(stars) >= D3_MIN_STARS:
            direct.append(
                f"D3: same frequency and drift toward {len(stars)} other targets, "
                f"e.g. {same[0]['target']} on MJD {same[0]['mjd']}"
            )
        elif chance < 0.05:
            flags.append(
                f"one other target at the same frequency and drift ({same[0]['target']} "
                f"on MJD {same[0]['mjd']}); one is not enough to close a case"
            )
    row = next((s for s in b["scans"] if s["pos"] == seed["seed_pos"]), {})
    share = (row.get("track_time_profile") or {}).get("largest_single_sample_share", 0)
    if share >= BURST_SHARE:
        direct.append(f"D4: {share:.0%} of the track's power is in one time sample")
    text, flag = flash(row)
    if text:
        direct.append(text)
    elif flag:
        flags.append(flag)
    if repeat:
        text, _ = repeat_rule(b, repeat)
        if text:
            direct.append(text)
    if session:
        text, _ = session_rule(b, session)
        if text:
            direct.append(text)
    if satellites:
        text, _ = satellite_rule(satellites)
        if text:
            flags.append(text)
    if companions:
        text, flag = companion_rule(b, companions)
        if text:
            direct.append(text)
        elif flag:
            flags.append(flag)
    if not direct:
        return {
            "status": "open",
            "verdict": "unresolved",
            "direct_evidence": [],
            "flags": flags,
        }
    kinds = {x[:2] for x in direct}
    # D8 that rests only on a companion's burst says what D4 says
    burst_only = kinds == {"D8"} and all("is itself closed: D4:" in x and "D1:" not in x and "D7:" not in x for x in direct)
    verdict = (
        "rfi_identified"
        if "D2" in kinds
        else "not_a_tone"
        if kinds == {"D4"} or burst_only
        else "rfi_probable"
    )
    return {
        "status": "closed",
        "verdict": verdict,
        "direct_evidence": direct,
        "flags": flags,
    }


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
    rp, sp = folder / "repeat.json", folder / "session.json"
    tp = folder / "satellites.json"
    cp = ROOT / "cases" / "companions" / f"{case_id}.json"
    b = json.loads((folder / "evidence.json").read_text())
    return decide(
        b,
        repeat=json.loads(rp.read_text()) if rp.exists() else None,
        session=json.loads(sp.read_text()) if sp.exists() else None,
        satellites=json.loads(tp.read_text()) if tp.exists() else None,
        companions=json.loads(cp.read_text()) if cp.exists() else None,
    )
