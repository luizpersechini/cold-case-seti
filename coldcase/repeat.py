"""Repeat check: was an event seen again on another day?

For a case that already has evidence, find the other days on which the same
star was observed in a band that holds the event's frequency, and on each of
them measure two places in every scan:

  same      the event's own frequency at the telescope. A local emitter stays
            there; a sky signal is moved away by the Earth's motion.
  shifted   where a transmitter that keeps a fixed frequency in the solar
            system barycentre frame would have moved to.

Each measurement is forced photometry within +-300 Hz against about 45 control
windows, as in investigate.measure. Writes cases/evidence/<id>/repeat.json and
one plot per day. Rule D5 in triage.py turns a repeat at the same telescope
frequency into a closure; a repeat at the shifted frequency is only flagged
for a human. "Absent" proves little: a transmitter on an orbiting planet can
be shifted by an unknown amount, and this check does not search for it.

usage: python -m coldcase.repeat CASE_ID ...       queue or control ids
       python -m coldcase.repeat --open            every open queue event
       python -m coldcase.repeat --selftest CASE_ID
           injected tones and the blind controls of the case's download group;
           writes nothing
"""

import contextlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone

import numpy as np

from . import checks, db, investigate, ledger, remote, triage
from . import dedoppler as dd

CHAN_HZ = 2.7939677  # GBT fine resolution, to size cutouts before a file is read
WINDOW_HZ = triage.SAME_HZ
MARGIN = triage.OFF_MARGIN
MIN_SHIFT_HZ = triage.REPEAT_MIN_SHIFT_HZ
OUT_OF_BAND = "outside the file's band"


def find_days(con, target, band, event_mjd, freq_mhz):
    """Other observations of the target that may hold freq_mhz. First the
    six-scan cadences of the same band in the hit tables; then fine-resolution
    files the archive lists for the target that belong to none of them (those
    give on-star scans only)."""
    rows = con.execute(
        """select c.cadence_id, c.pos, s.mjd, s.name, s.source
           from cadences c join scans s using (file_id)
           where c.cadence_id in (
               select c2.cadence_id from cadences c2 join scans s2 using (file_id)
               where c2.pos = 0 and upper(s2.source) = upper(?) and s2.band = ?)
           order by c.cadence_id, c.pos""",
        [target, band],
    ).fetchall()
    cads = {}
    for cad, pos, mjd, name, source in rows:
        cads.setdefault(cad, []).append((pos, mjd, name, source))
    days, known = [], set()
    for sc in cads.values():
        if abs(sc[0][1] - event_mjd) < 0.5:
            continue
        scans = [
            {
                "pos": pos,
                "role": "ON" if pos in investigate.ON_POS else "OFF",
                "source": source,
                "mjd": mjd,
                "url": remote.url_for_scan(name, source),
            }
            for pos, mjd, name, source in sc
        ]
        known.update(s["url"] for s in scans)
        days.append({"scans": scans, "off_scans": True})
    # the same scan can be listed in both file formats, and under a cadence
    taken = [s["mjd"] for d in days for s in d["scans"]]
    extra = {}
    for r in remote.api_files(target):
        url = r.get("url") or ""
        try:
            mjd, centre = float(r["mjd"]), float(r["center_freq"])
        except (KeyError, TypeError, ValueError):
            continue
        if (
            not url.endswith(("0000.h5", "0000.fil"))
            or url in known
            or abs(mjd - event_mjd) < 0.5
            or abs(centre - freq_mhz) > 500
            or any(abs(mjd - t) < 1 / 1440 for t in taken)
        ):
            continue
        group = extra.setdefault((int(mjd), round(centre)), {})
        slot = round(mjd, 4)
        if slot not in group or url.endswith(".h5"):
            group[slot] = {"role": "ON", "source": target, "mjd": mjd, "url": url}
    for group in extra.values():
        sc = sorted(group.values(), key=lambda s: s["mjd"])
        days.append(
            {"scans": [dict(s, pos=i) for i, s in enumerate(sc)], "off_scans": False}
        )
    return sorted(days, key=lambda d: d["scans"][0]["mjd"])


def _half_khz(drift_reach):
    """Cutout half-width that holds the window, its control windows and the
    drift across a scan of about five minutes."""
    ctol = int(np.ceil(WINDOW_HZ / CHAN_HZ))
    channels = 25.5 * (2 * ctol + 3) + ctol + drift_reach * 320.0 / CHAN_HZ + 30
    return float(np.ceil(channels * CHAN_HZ / 1e3))


def _fetch_all(jobs):
    """Cutouts for (url, MHz, half_khz) jobs, six at a time. A failed read is
    tried once more: a runner batch may clear the chunk cache meanwhile."""
    missing = {"error": "no public file found", "url": None}
    todo = [j for j in jobs if j[0]]
    got = []
    if todo:
        with ProcessPoolExecutor(
            max_workers=min(6, len(todo)), initializer=investigate._quiet_worker
        ) as ex:
            got = list(ex.map(investigate._fetch, todo))
        got = [
            investigate._fetch(j)
            if "error" in c and OUT_OF_BAND not in c["error"]
            else c
            for j, c in zip(todo, got)
        ]
    it = iter(got)
    return [next(it) if j[0] else dict(missing) for j in jobs]


def _measure(scans, cuts, freqs, drifts, dtol):
    """In every scan: the strongest signal starting within WINDOW_HZ of the
    predicted frequency, and the same measurement at control windows."""
    rows = []
    for s, cu, f_pred, d in zip(scans, cuts, freqs, drifts):
        row = {"pos": s["pos"], "role": s["role"], "source": s.get("source")}
        if "error" in cu:
            rows.append({**row, "error": cu["error"]})
            continue
        fo = cu["foff"] * 1e6
        k = (f_pred - cu["fch1"]) / cu["foff"]
        ctol = WINDOW_HZ / abs(fo)
        width = 2 * int(np.ceil(ctol)) + 3
        ctrl_k = [k + m * width for m in range(-25, 26) if abs(m) >= 3]
        res = dd.track_multi(
            cu["data"], fo, cu["tsamp"], [k] + ctrl_k, d, chan_tol=ctol, drift_tol=dtol
        )
        tr = res[0]
        ctrl = np.array([r["snr"] for r in res[1:] if r["in_cutout"]])
        ok = tr["snr"] is not None and len(ctrl) >= 10
        found = None if tr["chan"] is None else cu["fch1"] + tr["chan"] * cu["foff"]
        rows.append(
            {
                **row,
                "mjd": cu["tstart"],
                "predicted_freq_mhz": round(f_pred, 6),
                "track_snr": None if tr["snr"] is None else round(tr["snr"], 1),
                "offset_hz": None
                if found is None
                else round((found - f_pred) * 1e6, 1),
                "drift": None if tr["drift"] is None else round(tr["drift"], 4),
                "controls": None
                if len(ctrl) == 0
                else {
                    "n": int(len(ctrl)),
                    "median_snr": round(float(np.median(ctrl)), 1),
                    "max_snr": round(float(ctrl.max()), 1),
                    "second_snr": round(float(np.sort(ctrl)[-2]), 1)
                    if len(ctrl) > 1
                    else None,
                },
                "margin_over_control_max": round(float(tr["snr"] - ctrl.max()), 1)
                if ok
                else None,
            }
        )
    return rows


def _best(rows, role=None):
    """Largest margin over the controls among the scans (of one role)."""
    m = [
        (r["margin_over_control_max"], r["pos"])
        for r in rows
        if r.get("margin_over_control_max") is not None
        and (role is None or r["role"] == role)
    ]
    return max(m, default=(None, None))


def _inject(scans, cuts, f0_mhz, drift, off_fraction, snr=30.0):
    """Tests only: a synthetic tone in the day's cutouts, in memory."""
    n_on = sum(s["role"] == "ON" for s in scans)
    spec = {
        "f0_mhz": f0_mhz,
        "drift": drift,
        "snr": snr,
        "present_on": [1] * n_on,
        "scint": [1.0] * n_on,
        "off_fraction": off_fraction,
    }
    investigate.apply_injection({"inject": spec}, scans, cuts)


def _plot(path, title, scans, same_cuts, shift_cuts, f_same, f_shift, shift_hz):
    """One row per scan. Left: around the event's telescope frequency. Right:
    around the frequency shifted by the Earth's motion. Red triangles on the
    panel edges mark the +-300 Hz window; they are annotation, not data."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    n = len(scans)
    fig, axes = plt.subplots(n, 2, figsize=(10, 1.45 * n + 1.3), squeeze=False)
    for i, s in enumerate(scans):
        for j, (cu, fc) in enumerate(
            ((same_cuts[i], f_same), (shift_cuts[i], f_shift[i]))
        ):
            ax = axes[i, j]
            ax.tick_params(labelsize=7)
            if j == 0:
                ax.set_ylabel(f"{s['role']} {s.get('source') or ''}\nt (s)", fontsize=7)
            if "error" in cu:
                ax.text(
                    0.5,
                    0.5,
                    "not read",
                    transform=ax.transAxes,
                    ha="center",
                    fontsize=8,
                )
                ax.set_xticks([])
                ax.set_yticks([])
                continue
            flat = dd.flatten(cu["data"])
            med = np.median(flat)
            mad = 1.4826 * np.median(np.abs(flat - med))
            nt, nf = flat.shape
            dur = nt * cu["tsamp"]
            x0 = (cu["fch1"] - fc) * 1e3
            x1 = (cu["fch1"] + (nf - 1) * cu["foff"] - fc) * 1e3
            ax.imshow(
                flat,
                aspect="auto",
                origin="upper",
                cmap="viridis",
                vmin=med - 2 * mad,
                vmax=med + 8 * mad,
                extent=[x0, x1, dur, 0],
                interpolation="nearest",
            )
            for x in (-WINDOW_HZ / 1e3, WINDOW_HZ / 1e3):
                ax.plot([x], [0], marker="v", color="red", ms=5, clip_on=False)
                ax.plot([x], [dur], marker="^", color="red", ms=5, clip_on=False)
            ax.set_xlim(-1.5, 1.5)
    axes[0, 0].set_title("same telescope frequency", fontsize=9)
    axes[0, 1].set_title(
        f"shifted by the Earth's motion ({shift_hz / 1e3:+.1f} kHz)", fontsize=9
    )
    axes[-1, 0].set_xlabel(f"offset from {f_same:.6f} MHz (kHz)", fontsize=8)
    axes[-1, 1].set_xlabel("offset from the shifted frequency (kHz)", fontsize=8)
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


def _close(case_id, bundle, out):
    """Apply D5 to an open queue event and the other lines of the same event."""
    dec = triage.decide(bundle, repeat=out)
    if not any(x.startswith("D5") for x in dec["direct_evidence"]):
        return []
    from . import runner

    con = runner.queue_db()
    row = con.execute("select status from queue where case_id=?", (case_id,)).fetchone()
    if not row or row[0] not in ("open", "open_unread"):
        return []
    ids = [case_id] + [
        r[0]
        for r in con.execute(
            "select case_id from queue where status='open_member' and note=?",
            (case_id,),
        ).fetchall()
    ]
    why = "; ".join(dec["direct_evidence"])
    for cid in ids:
        ledger.add_verdict(
            ledger.connect(),
            {
                "case_id": cid,
                "role": "triage",
                "agent": "code",
                "verdict": dec["verdict"],
                "confidence": 1.0,
                "rationale": why,
                "evidence_refs": dec["direct_evidence"],
            },
        )
        runner.set_status(con, cid, "closed", dec["verdict"])
        runner.log(f"{cid} closed: {dec['verdict']} (repeat check)")
    return ids


def _summarise(bundle, out):
    text, stats = triage.repeat_rule(bundle, out)
    days = [d for d in out["days"] if d["usable"]]
    sky = [d for d in days if d["outcome"] == "shifted_frequency_on_star_only"]
    same = [d for d in days if d["outcome"] == "same_telescope_frequency"]
    parts = []
    if not days:
        outcome = "no_other_day"
        parts.append("no other observation in this band")
    elif text:
        outcome = "local_repeat"
        parts.append(
            f"back at the same telescope frequency in {stats['days_matched']} of "
            f"{stats['days_counted']} other observations (D5)"
        )
    elif sky:
        outcome = "sky_frame_repeat"
    elif same:
        outcome = "same_frequency_not_counted"
    else:
        outcome = "absent"
        on_only = sum(1 for d in days if not d["off_scans_available"])
        parts.append(
            f"absent in {len(days)} other observation{'s' if len(days) != 1 else ''}"
            + (f" ({on_only} with on-star scans only)" if on_only else "")
        )
    if sky:
        parts.append(
            "SEEN AGAIN at the Earth-shifted frequency on MJD "
            + ", ".join(
                f"{d['mjd']:.2f}"
                + ("" if d["off_scans_available"] else " (no off-star scans)")
                for d in sky
            )
            + ", on-star scans only"
        )
    if same and not text:
        parts.append(
            f"same telescope frequency in {len(same)} of {len(days)} other observations, not "
            f"counted: chance of unrelated matches {stats['chance_of_this_many_matches']}"
        )
    out["summary"] = {
        "outcome": outcome,
        "text": "; ".join(parts),
        **stats,
        "direct_evidence": text,
        "net_mb": round(sum(d["net_mb"] for d in out["days"]), 1),
    }
    return out


def run(case_id, inject=None, save=True, verbose=True):
    """Measure one case on its other days. inject ('local' or 'sky') adds a
    synthetic tone to the first suitable day, for the self-test."""
    say = print if verbose else (lambda *a, **k: None)
    folder = remote.ROOT / "cases" / "evidence" / case_id
    b = json.loads((folder / "evidence.json").read_text())
    seed, case = b["seed"], b["case"]
    out = {"case_id": case_id, "days": []}

    def done(note=None):
        _summarise(b, out)
        if note:
            out["summary"].update(outcome="not_checked", text=note)
        if save:
            (folder / "repeat.json").write_text(json.dumps(out, indent=1))
            closed = (
                _close(case_id, b, out)
                if out["summary"]["outcome"] == "local_repeat"
                else []
            )
            if closed:
                say(f"  closed by D5: {', '.join(closed)}")
        say(f"  result: {out['summary']['text']}  [{out['summary']['net_mb']} MB]")
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
    ra, dec = b["pointing"]["ra_hours"], b["pointing"]["dec_deg"]
    con = db.connect()
    cad = db.cadence_for(con, case["target"], t_e)
    if cad is None:
        return done("not checked: the event's cadence is not in the hit tables")
    d_earth = checks.sky_frame_prediction(ra, dec, "GBT", t_e, f_e, [t_e])[
        "drift_at_ref"
    ]
    unexplained = (
        d_e - d_earth
    )  # the part of the drift the Earth's motion does not give
    dtol = 0.05 + 0.25 * abs(d_e)
    dtol_shift = min(1.0, abs(unexplained) / 2 + dtol)
    out["event"] = {
        "target": case["target"],
        "band": cad[0]["band"],
        "mjd": round(t_e, 5),
        "freq_mhz": f_e,
        "drift": d_e,
        "earth_motion_drift": round(d_earth, 4),
        "drift_matches_earth_motion": bool(abs(unexplained) <= dtol),
    }
    say(
        f"{case_id} {case['target']} {f_e:.6f} MHz drift {d_e:+.4f}, event MJD {t_e:.3f}, "
        f"band {cad[0]['band']}"
    )
    if not out["event"]["drift_matches_earth_motion"]:
        say(
            f"  note: the event drifted {d_e:+.3f} Hz/s where the Earth's motion alone gives "
            f"{d_earth:+.3f}; a transmitter fixed in the sky frame would not, so a repeat at "
            "the shifted frequency is not expected"
        )
    for day in find_days(con, case["target"], cad[0]["band"], t_e, f_e):
        scans = day["scans"]
        pred = checks.sky_frame_prediction(
            ra, dec, "GBT", t_e, f_e, [s["mjd"] for s in scans]
        )
        reach = max(
            abs(d_e) + dtol,
            max(abs(x + unexplained / 2) for x in pred["drift"]) + dtol_shift,
        )
        half = _half_khz(reach)
        # two rounds, so the second finds the data chunk of the first in the cache
        same_cuts = _fetch_all([(s["url"], f_e, half) for s in scans])
        shift_cuts = _fetch_all(
            [(s["url"], round(f, 6), half) for s, f in zip(scans, pred["freq_mhz"])]
        )
        errs = [c["error"] for c in same_cuts + shift_cuts if "error" in c]
        if errs and len(errs) == 2 * len(scans) and all(OUT_OF_BAND in e for e in errs):
            continue  # another receiver band: this day cannot hold the frequency
        good = [c for c in same_cuts + shift_cuts if "error" not in c]
        t_first = min((c["tstart"] for c in good), default=scans[0]["mjd"])
        times = [
            c["tstart"] if "error" not in c else s["mjd"]
            for c, s in zip(shift_cuts, scans)
        ]
        pred = checks.sky_frame_prediction(ra, dec, "GBT", t_e, f_e, times)
        first = int(np.argmin(times))
        shift_hz = (pred["freq_mhz"][first] - f_e) * 1e6
        injected = None
        if (
            inject
            and "injected" not in out
            and day["off_scans"]
            and not errs
            and abs(shift_hz) >= MIN_SHIFT_HZ
        ):
            if inject == "local":  # at the telescope frequency, in every scan
                _inject(scans, same_cuts, f_e, d_e, 1.0)
            else:  # at the shifted frequency, on the star only
                _inject(
                    scans,
                    shift_cuts,
                    pred["freq_mhz"][first],
                    pred["drift"][first],
                    0.0,
                )
            injected = out["injected"] = inject
        same = _measure(scans, same_cuts, [f_e] * len(scans), [d_e] * len(scans), dtol)
        shift_drifts = [x + unexplained / 2 for x in pred["drift"]]
        shifted = _measure(
            scans, shift_cuts, pred["freq_mhz"], shift_drifts, dtol_shift
        )
        bs, bs_pos = _best(same)
        bon, bon_pos = _best(shifted, "ON")
        boff, _ = _best(shifted, "OFF")
        usable = bs is not None and bon is not None
        det_same = bs is not None and bs >= MARGIN
        det_on = bon is not None and bon >= MARGIN
        det_off = boff is not None and boff >= MARGIN
        if not usable:
            outcome = "unusable"
        elif abs(shift_hz) < MIN_SHIFT_HZ:
            outcome = "too_close_to_tell" if det_same or det_on else "absent"
        elif det_same:
            outcome = "same_telescope_frequency"
        elif det_on and not det_off:
            outcome = "shifted_frequency_on_star_only"
        elif det_on:
            outcome = "shifted_frequency_also_off_star"
        else:
            outcome = "absent"
        rec = {
            "mjd": round(t_first, 5),
            "days_from_event": round(t_first - t_e, 1),
            "off_scans_available": day["off_scans"],
            "shift_hz": round(shift_hz, 1),
            "usable": usable,
            "outcome": outcome,
            "same": {
                "best_margin": bs,
                "best_scan": bs_pos,
                "drift_window_hz_s": [round(d_e - dtol, 4), round(d_e + dtol, 4)],
                "scans": same,
            },
            "shifted": {
                "best_margin_on": bon,
                "best_scan_on": bon_pos,
                "best_margin_off": boff,
                "drift_window_hz_s": [
                    round(min(shift_drifts) - dtol_shift, 4),
                    round(max(shift_drifts) + dtol_shift, 4),
                ],
                "scans": shifted,
            },
            "net_mb": round(
                sum(0 if c.get("cached") else c.get("net_bytes", 0) for c in good)
                / 1e6,
                1,
            ),
        }
        if injected:
            rec["injected"] = injected
        if errs:
            rec["errors"] = sorted(set(errs))
        if save and good:
            rec["plot"] = f"repeat_{rec['mjd']:.3f}.png"
            _plot(
                folder / rec["plot"],
                f"{case_id}  {case['target']}  MJD {rec['mjd']:.2f}  "
                f"({rec['days_from_event']:+.0f} days from the event)",
                scans,
                same_cuts,
                shift_cuts,
                f_e,
                pred["freq_mhz"],
                shift_hz,
            )
        out["days"].append(rec)
        say(
            f"  MJD {rec['mjd']:.2f}  shift {shift_hz / 1e3:+8.1f} kHz | same frequency: "
            f"{bs} over controls | shifted: on-star {bon}, off-star {boff} | {outcome}"
            f"{' [injected ' + injected + ']' if injected else ''} | {rec['net_mb']} MB"
        )
    return done()


def selftest(case_id):
    """Check the check, on the blind sky-only controls of the case's download
    group: nothing was injected into their other days, so every day must come
    out absent; a tone added at the telescope frequency must be reported as
    local, and one added at the shifted frequency as a sky-frame repeat."""
    led = ledger.connect()
    row = led.execute("select grp from queue where case_id=?", (case_id,)).fetchone()
    row = (
        row
        or led.execute(
            "select grp from controls where case_id=?", (case_id,)
        ).fetchone()
    )
    if not row:
        print(f"{case_id}: not in the queue or the controls")
        return False
    ctl = [
        r[0]
        for r in led.execute(
            "select case_id from controls where grp=? and kind like 'sky%' and recovered=1 "
            "and status='open' order by case_id",
            (row[0],),
        ).fetchall()
    ]
    if not ctl:
        print(f"group {row[0]} has no open sky-only control to test with")
        return False
    seen = (
        "same_telescope_frequency",
        "shifted_frequency_on_star_only",
        "too_close_to_tell",
    )
    results = []
    for cid in ctl:
        res = run(cid, save=False)
        days = [d for d in res["days"] if d["usable"]]
        bad = [d for d in days if d["outcome"] in seen]
        results.append(
            (
                f"control {cid}: absent in all {len(days)} other observations",
                bool(days) and not bad,
            )
        )
    bundle = json.loads(
        (remote.ROOT / "cases" / "evidence" / ctl[0] / "evidence.json").read_text()
    )
    for kind, want in (
        ("local", "same_telescope_frequency"),
        ("sky", "shifted_frequency_on_star_only"),
    ):
        res = run(ctl[0], inject=kind, save=False)
        day = next((d for d in res["days"] if d.get("injected")), None)
        label = f"{kind} tone injected into {ctl[0]}: reported as {want}"
        if not day:
            label += " (no suitable day to inject into)"
        elif kind == "local":
            text, stats = triage.repeat_rule(bundle, res)
            label += (
                f"; D5 {'applies' if text else 'does not apply'}, chance of "
                f"unrelated matches {stats['chance_of_this_many_matches']}"
            )
        results.append((label, bool(day) and day["outcome"] == want))
    print()
    for label, ok in results:
        print(("PASS  " if ok else "FAIL  ") + label)
    return all(ok for _, ok in results)


@contextlib.contextmanager
def _turn():
    """Take turns with the runner by holding its lock: both use one download
    cache, and a runner batch empties it after every group. Waits for a batch
    in progress to end; the cache is emptied when the check is done."""
    from . import runner

    told = False
    while runner.LOCK.exists():
        pid = runner.LOCK.read_text().strip()
        if not (
            pid.isdigit()
            and subprocess.run(["kill", "-0", pid], capture_output=True).returncode == 0
        ):
            break
        if not told:
            print(
                f"a runner batch is in progress (pid {pid}); waiting for it to end",
                flush=True,
            )
            told = True
        time.sleep(20)
    runner.LOCK.write_text(str(os.getpid()))
    runner._state.clear()
    runner.set_state(
        pid=os.getpid(),
        step="repeat check",
        run_started=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    try:
        yield
    finally:
        remote.clear_chunks()
        runner.set_state(step="idle")
        runner.LOCK.unlink(missing_ok=True)


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    if args[0] == "--selftest":
        with _turn():
            ok = selftest(args[1])
        sys.exit(0 if ok else 1)
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
    with _turn():
        for cid in args:
            run(cid)


if __name__ == "__main__":
    main()
