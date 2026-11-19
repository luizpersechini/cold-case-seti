"""Session check: was an event's line also there while the telescope pointed
at other stars, in the half hour around it?

A signal from one star cannot follow the telescope to another. For a case
that already has evidence, take the scans of other stars made in the same
receiver band within WINDOW_MIN minutes of the event (the nearest MAX_SCANS)
and measure the event's own telescope frequency in each: forced photometry
within +-300 Hz against about 45 control windows, as in the repeat check.
Writes cases/evidence/<id>/session.json and session.png. Rule D7 in triage.py
turns matches into a closure when unrelated lines are unlikely to explain
them. "Absent" proves little: interference that comes and goes need not be on
in those scans.

usage: python -m coldcase.session CASE_ID ...
       python -m coldcase.session --open [--max-gb N]   every open queue event
       add --measure-only to write the results without closing anything
       python -m coldcase.session --rate               how often a moved
                                                       position holds a line
"""

import json
import sys
from math import acos, cos, degrees, radians, sin

import numpy as np

from . import db, ledger, remote, repeat, triage
from . import dedoppler as dd

WINDOW_MIN, MAX_SCANS = 30.0, 8
MIN_SEP_DEG = 1.0  # a neighbour closer than this to the event's pointing is not counted
NAME = "session.json"


def neighbours(con, cad, target, t_e):
    """Scans of other stars in the event's band within WINDOW_MIN minutes,
    nearest first. The event's own six scans and other scans of its star are
    left out."""
    own = [c["name"] for c in cad]
    rows = con.execute(
        "select distinct name, source, mjd from scans where band = ? "
        "and abs(mjd - ?) <= ? and upper(source) <> upper(?) order by abs(mjd - ?)",
        [cad[0]["band"], t_e, WINDOW_MIN / 1440.0, target, t_e],
    ).fetchall()
    out = []
    for name, source, mjd in rows:
        if name in own:
            continue
        out.append(
            {
                "pos": len(out),
                "role": "OTHER",
                "source": source,
                "mjd": mjd,
                "url": remote.url_for_scan(name, source),
            }
        )
        if len(out) == MAX_SCANS:
            break
    return out


def _sep_deg(ra1_h, dec1, ra2_h, dec2):
    a1, d1, a2, d2 = (radians(x) for x in (ra1_h * 15, dec1, ra2_h * 15, dec2))
    c = sin(d1) * sin(d2) + cos(d1) * cos(d2) * cos(a1 - a2)
    return degrees(acos(max(-1.0, min(1.0, c))))


def _plot(path, title, scans, cuts, f_e):
    """One row per neighbouring scan, around the event's telescope frequency.
    Red triangles on the panel edges mark the +-300 Hz window; they are
    annotation, not data."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    n = len(scans)
    fig, axes = plt.subplots(n, 1, figsize=(7, 1.45 * n + 1.3), squeeze=False)
    for ax, s, cu in zip(axes[:, 0], scans, cuts):
        ax.tick_params(labelsize=7)
        ax.set_ylabel(f"{s.get('source') or ''}\nt (s)", fontsize=7)
        if "error" in cu:
            ax.text(
                0.5, 0.5, "not read", transform=ax.transAxes, ha="center", fontsize=8
            )
            ax.set_xticks([])
            ax.set_yticks([])
            continue
        flat = dd.flatten(cu["data"])
        med = np.median(flat)
        mad = 1.4826 * np.median(np.abs(flat - med))
        nt, nf = flat.shape
        dur = nt * cu["tsamp"]
        ax.imshow(
            flat,
            aspect="auto",
            origin="upper",
            cmap="viridis",
            vmin=med - 2 * mad,
            vmax=med + 8 * mad,
            extent=[
                (cu["fch1"] - f_e) * 1e3,
                (cu["fch1"] + (nf - 1) * cu["foff"] - f_e) * 1e3,
                dur,
                0,
            ],
            interpolation="nearest",
        )
        for x in (-repeat.WINDOW_HZ / 1e3, repeat.WINDOW_HZ / 1e3):
            ax.plot([x], [0], marker="v", color="red", ms=5, clip_on=False)
            ax.plot([x], [dur], marker="^", color="red", ms=5, clip_on=False)
        ax.set_xlim(-1.5, 1.5)
    axes[-1, 0].set_xlabel(f"offset from {f_e:.6f} MHz (kHz)", fontsize=8)
    fig.text(
        0.5,
        0.004,
        "Red triangles on panel edges mark the +-300 Hz window (annotation, not data)",
        ha="center",
        fontsize=7,
    )
    fig.suptitle(title, fontsize=10)
    fig.tight_layout(rect=(0, 0.015, 1, 1))
    fig.savefig(path, dpi=110)
    plt.close(fig)


def _close(case_id, bundle, out, say):
    """Apply D7 to an open queue event and the other lines of the same event."""
    dec = triage.decide(bundle, session=out)
    if not any(x.startswith("D7") for x in dec["direct_evidence"]):
        return
    from . import runner

    con = runner.queue_db()
    row = con.execute("select status from queue where case_id=?", (case_id,)).fetchone()
    if not row or row[0] not in ("open", "open_unread"):
        return
    ids = [case_id] + [
        r[0]
        for r in con.execute(
            "select case_id from queue where status='open_member' and note=?",
            (case_id,),
        ).fetchall()
    ]
    why = "; ".join(dec["direct_evidence"])
    for cid in ids:
        text = why + (
            "" if cid == case_id else f"; this line belongs to the event of {case_id}"
        )
        ledger.add_verdict(
            ledger.connect(),
            {
                "case_id": cid,
                "role": "triage",
                "agent": "code",
                "verdict": dec["verdict"],
                "confidence": 1.0,
                "rationale": text,
                "evidence_refs": dec["direct_evidence"],
            },
        )
        runner.set_status(con, cid, "closed", dec["verdict"])
        runner.log(f"{cid} closed: {dec['verdict']} (session check)")
    say(f"  closed by D7: {', '.join(ids)}")


def run(case_id, save=True, close=True, verbose=True, bundle=None, cached_only=False):
    """Measure one case in the scans of other stars around it. Returns the
    record (also written to session.json) with the megabytes downloaded.
    With `bundle` the measurement is made for that evidence instead of the
    stored one. With cached_only a neighbouring scan is used only if a cutout
    of it is already in the cache, so nothing is downloaded."""
    say = print if verbose else (lambda *a, **k: None)
    folder = remote.ROOT / "cases" / "evidence" / case_id
    b = bundle or json.loads((folder / "evidence.json").read_text())
    seed, case = b["seed"], b["case"]
    out = {"case_id": case_id, "scans": [], "net_mb": 0.0}

    def done(note=None):
        text, stats = triage.session_rule(b, out)
        n, k = stats["scans_counted"], stats["scans_matched"]
        if note:
            outcome = "not_checked"
        elif n == 0:
            outcome, note = "no_other_star", "no other star observed within 30 minutes"
        elif text:
            outcome = "seen_toward_other_stars"
            note = f"the same line toward other stars in {k} of {n} scans within 30 minutes (D7)"
        elif k:
            outcome = "match_not_counted"
            note = (
                f"a line at the same frequency toward another star in {k} of {n} scans, "
                f"not counted: chance of unrelated lines {stats['chance_of_this_many']}"
            )
        else:
            outcome = "absent"
            note = f"absent toward other stars in {n} scans within 30 minutes"
        out["summary"] = {
            "outcome": outcome,
            "text": note,
            **stats,
            "direct_evidence": text,
        }
        if save:
            (folder / NAME).write_text(json.dumps(out, indent=1))
            if close and text:
                _close(case_id, b, out, say)
        say(f"  result: {note}  [{out['net_mb']} MB]")
        return out

    if (
        not seed.get("recovered")
        or "pointing" not in b
        or b.get("site", "GBT") != "GBT"
    ):
        return done(
            "not checked: needs a recovered Green Bank event with a known pointing"
        )
    ev = next(s for s in b["scans"] if s["pos"] == seed["seed_pos"])
    t_e, f_e, d_e = ev["mjd"], seed["freq_mhz"], seed["drift"]
    con = db.connect()
    cad = db.cadence_for(con, case["target"], t_e)
    if cad is None:
        return done("not checked: the event's cadence is not in the hit tables")
    dtol = 0.05 + 0.25 * abs(d_e)
    out["event"] = {
        "target": case["target"],
        "band": cad[0]["band"],
        "mjd": round(t_e, 5),
        "freq_mhz": f_e,
        "drift": d_e,
    }
    say(f"{case_id} {case['target']} {f_e:.6f} MHz drift {d_e:+.4f}, MJD {t_e:.4f}")
    scans = neighbours(con, cad, case["target"], t_e)
    if not scans:
        return done()
    half = repeat._half_khz(abs(d_e) + dtol)
    if cached_only:
        cuts = [
            remote.cached_cutout_near(s["url"], f_e, half)
            or {"error": "not in the cache"}
            for s in scans
        ]
    else:
        cuts = repeat._fetch_all([(s["url"], f_e, half) for s in scans])
    good = [c for c in cuts if "error" not in c]
    out["net_mb"] = round(
        sum(0 if c.get("cached") else c.get("net_bytes", 0) for c in good) / 1e6, 1
    )
    rows = repeat._measure(scans, cuts, [f_e] * len(scans), [d_e] * len(scans), dtol)
    ra, dec = b["pointing"]["ra_hours"], b["pointing"]["dec_deg"]
    for s, cu, r in zip(scans, cuts, rows):
        r["minutes_from_event"] = round((s["mjd"] - t_e) * 1440, 1)
        if "error" not in cu and cu.get("ra_hours") is not None:
            r["separation_deg"] = round(
                _sep_deg(ra, dec, cu["ra_hours"], cu["dec_deg"]), 2
            )
        say(
            f"  {s['source']:>12} {r['minutes_from_event']:+6.1f} min  "
            + (
                f"not read: {r['error']}"
                if "error" in r
                else f"track {r['track_snr']} at {r['offset_hz']:+.0f} Hz, margin "
                f"{r['margin_over_control_max']} over controls, "
                f"{r.get('separation_deg', '?')} deg away"
            )
        )
    out["scans"] = rows
    if save and good:
        out["plot"] = "session.png"
        _plot(
            folder / out["plot"],
            f"{case_id}  {case['target']}  other stars within {WINDOW_MIN:.0f} min",
            scans,
            cuts,
            f_e,
        )
    return done()


def rate(say=print):
    """How often a moved position holds a line that stands out, over every
    session.json written so far: the share of counted scans whose strongest
    control window leads the next by the margin D7 asks of a match, divided
    by the number of control windows. triage.SESSION_BASE is set from it."""
    n = lead = windows = 0
    for p in (remote.ROOT / "cases" / "evidence").glob(f"*/{NAME}"):
        for r in triage.session_scans(json.loads(p.read_text())):
            c = r["controls"]
            n += 1
            windows += c["n"]
            lead += c["max_snr"] - c["second_snr"] >= triage.OFF_MARGIN
    say(
        f"{n} scans counted; in {lead} the strongest of the control windows leads by "
        f"{triage.OFF_MARGIN} or more; chance per scan that a given position holds such "
        f"a line: {lead / windows if windows else float('nan'):.4f}"
    )
    return (lead / windows) if windows else None


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    if args == ["--rate"]:
        rate()
        return
    close = "--measure-only" not in args
    args = [a for a in args if a != "--measure-only"]
    cap_gb = 25.0
    if "--max-gb" in args:
        i = args.index("--max-gb")
        cap_gb = float(args[i + 1])
        del args[i : i + 2]
    if args == ["--open"]:
        args = [
            r[0]
            for r in ledger.connect()
            .execute(
                "select case_id from queue where status in ('open','open_unread') "
                "order by case_id"
            )
            .fetchall()
        ]
    used = 0.0
    with repeat._turn():
        for cid in args:
            if used / 1e3 >= cap_gb:
                print(
                    f"stopped at the download cap of {cap_gb} GB; {cid} and later not done"
                )
                break
            used += run(cid, close=close)["net_mb"]
            remote.clear_chunks()
    print(f"downloaded {used / 1e3:.2f} GB")


if __name__ == "__main__":
    main()
