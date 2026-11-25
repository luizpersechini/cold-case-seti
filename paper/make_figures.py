"""Figures of the paper, from the ledger, the stored evidence and the hit
database. Read-only.

usage: uv run python paper/make_figures.py
Writes paper/figures/*.pdf and prints the numbers each figure shows.
"""

import json
import re
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from coldcase import checks, db, ledger

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "paper" / "figures"
OUT.mkdir(parents=True, exist_ok=True)
plt.rcParams.update({"font.size": 9, "axes.linewidth": 0.6, "figure.dpi": 150})

# the nine events probed, as the paper names them
PROBED = [
    ("f027", "HIP 100064, 1595.877"),
    ("f024", "HIP 54677, 1435.943"),
    ("f007", "HIP 100064, 1560.945"),
    ("f003", "HIP 109427, 1559.671"),
    ("f002", "HIP 109427, 1560.003"),
    ("f018", "HIP 29432, 1556.346"),
    ("f019", "HIP 42173, 1538.712"),
    ("f034", "HIP 87108, 2199.097"),
    ("q00671", "HIP 36357, 2051.617"),
]


def perm(cid):
    return json.load(open(ROOT / "cases" / "evidence" / cid / "probe_perm.json"))


def fig_probe():
    """Left: the line of one event toward other stars against time. Right:
    the event's statistic against the other windows of the same cutouts."""
    fig, (a, b) = plt.subplots(
        1, 2, figsize=(7.1, 3.1), gridspec_kw={"width_ratios": [1, 1.25]}
    )
    j = perm("f027")
    w = next(x for x in j["widths"] if x["half_hz"] == 2000.0)
    t = np.array([r["hours_from_event"] for r in w["scans"]])
    off = np.array([r["offset_hz"] for r in w["scans"]])
    m = np.array([r["p_scan"] for r in w["scans"]]) <= 0.01
    a.scatter(t[m], off[m], s=14, c="C3", label="stands out")
    a.scatter(
        t[~m], off[~m], s=14, facecolors="none", edgecolors="0.5", label="does not"
    )
    a.set_xlabel("hours after the event")
    a.set_ylabel("offset of the strongest line (Hz)")
    a.set_title("HIP 100064, 1595.877 MHz", fontsize=9)
    a.legend(frameon=False, fontsize=8)
    print("f027 at +-2 kHz:", len(t), "scans,", int(m.sum()), "stand out")

    for k, (cid, name) in enumerate(PROBED):
        j = perm(cid)
        w = next(x for x in j["widths"] if x["half_hz"] == j["best_half_hz"])
        q50, q90, q99, q999 = w["T_null_q"]
        b.plot([q50, q99], [k, k], color="0.6", lw=5, solid_capstyle="butt")
        b.plot([q99, w["T_null_max"]], [k, k], color="0.6", lw=1)
        b.plot([w["T_null_max"]], [k], marker="|", color="0.4", ms=7)
        b.plot([w["T_event"]], [k], marker="o", color="C3", ms=5)
        print(
            f"{name}: +-{j['best_half_hz']:g} Hz, T {w['T_event']}, null median {q50}, "
            f"99% {q99}, max {w['T_null_max']}, P {w['P']:.2g}, corrected {j['P_corrected']:.2g}, "
            f"{w['scans_standing_out']} of {j['scans_used']} scans stand out, {w['windows']} windows"
        )
    b.set_yticks(range(len(PROBED)))
    b.set_yticklabels([n for _, n in PROBED], fontsize=7.5)
    b.invert_yaxis()
    b.set_xlabel(
        "statistic $T$"
    )
    fig.tight_layout()
    fig.savefig(OUT / "probe.pdf")


def hms(s):
    a = [float(x) for x in re.findall(r"-?\d+\.?\d*", s)]
    v = abs(a[0]) + a[1] / 60 + a[2] / 3600
    return -v if s.strip().startswith("-") else v


def fig_belt():
    """Angle from the geostationary belt: every L-band star of the survey and
    the L-band stars of the events flagged for follow-up."""
    con = db.connect()
    per = {}
    for src, ra, dec, mjd in con.execute(
        """select distinct upper(s.source), s.ra, s.dec, s.mjd from cadences c
        join scans s using (file_id) where c.pos = 0 and s.band = 'L' order by s.mjd"""
    ).fetchall():
        if src in per:
            continue
        g = checks.geometry(hms(ra), hms(dec), mjd, "GBT")
        if g["geo_belt_separation_deg"] is not None:
            per[src] = g["geo_belt_separation_deg"]
    led = ledger.connect()
    fu = {}
    for tgt, status in led.execute(
        "select target, status from followup where band = 'L'"
    ):
        fu.setdefault(tgt.upper(), []).append(status)
    allv = np.array(list(per.values()))
    fv = np.array([per[k] for k in fu if k in per])
    op = np.array([any(s == "open" for s in fu[k]) for k in fu if k in per])
    print(
        "L-band stars",
        len(allv),
        "median",
        round(float(np.median(allv)), 1),
        "| flagged stars",
        len(fv),
        "median",
        round(float(np.median(fv)), 1),
        "| within 13 deg:",
        round(float(np.mean(allv < 13)), 3),
        int(np.sum(fv < 13)),
    )
    fig, a = plt.subplots(figsize=(3.4, 2.6))
    bins = np.arange(0, 95, 5)
    a.hist(
        allv,
        bins=bins,
        weights=np.full(len(allv), 1 / len(allv)),
        color="0.75",
        label=f"all L-band stars ({len(allv)})",
    )
    a.hist(
        fv,
        bins=bins,
        weights=np.full(len(fv), 1 / len(fv)),
        histtype="step",
        color="C3",
        lw=1.2,
        label=f"flagged stars ({len(fv)})",
    )
    for x in fv[op]:
        a.plot([x], [0.01], marker="|", color="k", ms=8)
    a.set_xlabel("angle from the geostationary belt (deg)")
    a.set_ylabel("fraction")
    a.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(OUT / "belt.pdf")


def wilson(k, n, z=1.0):
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return p, p - (c - h), (c + h) - p


def fig_controls():
    """What the rules did with the hidden test signals, against the injected
    S/N and against the drift."""
    led = ledger.connect()
    rows = led.execute(
        "select kind, inj_snr, abs(drift), status, masked from controls"
    ).fetchall()
    fig, axes = plt.subplots(1, 2, figsize=(7.1, 2.7), sharey=True)
    for ax, col, lab in (
        (axes[0], 1, "injected S/N (nominal)"),
        (axes[1], 2, "|drift| (Hz s$^{-1}$)"),
    ):
        for kinds, want, name, c in (
            (
                ("sky_persistent", "sky_intermittent"),
                "open",
                "sky-only, left open",
                "C0",
            ),
            (("leak",), "closed", "leak, closed", "C3"),
        ):
            r = [(x[col], x[3] == want) for x in rows if x[0] in kinds and not x[4]]
            s = np.array([x[0] for x in r])
            ok = np.array([x[1] for x in r])
            edges = np.quantile(s, np.linspace(0, 1, 7))
            mid, frac, lo, hi = [], [], [], []
            for e0, e1 in zip(edges[:-1], edges[1:]):
                sel = (s >= e0) & (s <= e1)
                p, dl, dh = wilson(int(ok[sel].sum()), int(sel.sum()))
                mid.append(np.median(s[sel]))
                frac.append(p)
                lo.append(dl)
                hi.append(dh)
            ax.errorbar(
                mid,
                frac,
                yerr=[lo, hi],
                marker="o",
                ms=3,
                lw=1,
                color=c,
                label=name,
                capsize=2,
            )
            if col == 1:
                print(name, len(s), "total", int(ok.sum()), "as intended")
        ax.set_xlabel(lab)
        ax.set_ylim(0, 1.05)
    axes[0].set_ylabel("fraction decided as intended")
    axes[0].legend(frameon=False, fontsize=8, loc="lower right")
    fig.tight_layout()
    fig.savefig(OUT / "controls.pdf")


if __name__ == "__main__":
    fig_probe()
    fig_belt()
    fig_controls()
    from PIL import Image

    im = Image.open(ROOT / "cases" / "evidence" / "q00671" / "waterfall.png")
    im.crop((0, 45, im.width, im.height)).save(OUT / "hip36357.png")  # no title line
