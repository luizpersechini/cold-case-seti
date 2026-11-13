"""The live part of the dashboard: what the batch is doing now, the cases of
the current (or last) run as tiles, the latest plot, and the whole queue laid
out along the spectrum. Returns HTML fragments for dashboard.py."""

import html
import json
import re
from datetime import datetime, timezone

from .remote import DATA, ROOT

LOG = ROOT / "cases" / "runner.log"
STATE = ROOT / "cases" / "runner_state.json"
STEPS = [
    ("download", "Download"),
    ("measure", "Measure"),
    ("decide", "Decide"),
    ("read", "Read"),
    ("controls", "Controls"),
    ("rank", "Rank"),
]
KINDS = {  # tile kind -> (label, css colour variable)
    "burst": ("burst, not a tone", "--burst"),
    "nosignal": ("no signal at the catalogue position", "--nosig"),
    "interference": ("interference", "--closed"),
    "open": ("open", "--open"),
    "member": ("open, part of another event", "--member"),
    "retry": ("to be retried", "--retry"),
    "control_open": ("control left open", "--ctl"),
    "control_closed": ("control closed", "--ctl"),
}
CSS = """
:root{--burst:#8a7fbf;--nosig:#8b97a6;--member:#e6c08a;--ctl:#2f8f5b}
.steps{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0}
.step{padding:3px 10px;border-radius:12px;border:1px solid var(--line);font-size:13px;color:var(--muted)}
.step.on{background:var(--closed);border-color:var(--closed);color:#fff}
.step.done{color:var(--text)}
.tiles{display:flex;flex-wrap:wrap;gap:4px;margin:8px 0}
.tile{width:15px;height:15px;border-radius:3px}
.tile.ring{background:transparent!important;border:2px solid var(--ctl)}
.tile.ring.shut{border-style:dotted}
.plot{max-width:100%;max-height:520px;border-radius:6px;border:1px solid var(--line)}
.two{display:grid;gap:14px;grid-template-columns:minmax(280px,1fr) minmax(280px,1.2fr);margin-bottom:14px}
@media (max-width:760px){.two{grid-template-columns:1fr}}
svg text{fill:var(--muted);font-size:10px}
"""


def esc(x):
    return html.escape(str(x))


def state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def run_events():
    """Events of the most recent run in the log: (start_ts, [(id, kind, text)])."""
    start, ev = None, []
    if not LOG.exists():
        return start, ev
    for line in LOG.read_text().splitlines():
        ts, _, msg = line.partition(" ")
        if msg.startswith("run start"):
            start, ev = ts, []
            continue
        m = re.match(r"control (c\S+): (\w+) \((\w+)\)", msg)
        if m:
            ev.append(
                (
                    m[1],
                    "control_open" if m[2] == "open" else "control_closed",
                    f"control, {m[2]} ({m[3]})",
                )
            )
            continue
        m = re.match(r"(q\d+) (.*)", msg)
        if not m or start is None:
            continue
        cid, what = m[1], m[2]
        if what.startswith("closed"):
            kind = (
                "burst"
                if "not_a_tone" in what
                else "nosignal"
                if "not_recovered" in what
                else "interference"
            )
        elif what.startswith("open; same event"):
            kind = "member"
        elif what.startswith("open"):
            kind = "open"
        elif "failed" in what:
            kind = "retry"
        else:
            continue
        ev = [
            e for e in ev if e[0] != cid
        ]  # a later line about a case replaces the earlier
        ev.append((cid, kind, what))
    return start, ev


def now_panel(running):
    st = state()
    if not running or not st:
        return ""
    step = st.get("step", "")
    idx = next((i for i, (k, _) in enumerate(STEPS) if k == step), -1)
    pills = "".join(
        f"<span class='step {'on' if i == idx else 'done' if i < idx else ''}'>{label}</span>"
        for i, (_, label) in enumerate(STEPS)
    )
    try:
        mins = (
            datetime.now(timezone.utc) - datetime.fromisoformat(st["run_started"])
        ).total_seconds() / 60
        elapsed = f"{mins:.0f} min into this batch"
    except Exception:
        elapsed = ""
    lines = []
    if st.get("target"):
        lines.append(
            f"<div class='big' style='font-size:22px'>{esc(st['target'])} "
            f"<span class='muted' style='font-size:15px;font-weight:400'>near "
            f"{st.get('freq_mhz', 0):.1f} MHz</span></div>"
        )
    detail = []
    if st.get("groups_done") is not None:
        detail.append(f"group {st['groups_done'] + 1} of this batch")
    if st.get("n_cases"):
        detail.append(
            f"case {min(st.get('i_case', 0) + 1, st['n_cases'])} of {st['n_cases']}"
        )
    if step == "download":
        n = (
            len(list(DATA.joinpath("chunks").glob("*.npz")))
            if DATA.joinpath("chunks").exists()
            else 0
        )
        detail.append(f"{min(n, 6)} of 6 scans downloaded")
    if elapsed:
        detail.append(elapsed)
    return (
        "<div class='card' style='margin-bottom:14px'><h2>Now</h2>"
        + "".join(lines)
        + f"<div class='steps'>{pills}</div>"
        + f"<div class='muted'>{esc(' · '.join(detail))}</div></div>"
    )


def tiles_and_plot(running):
    start, ev = run_events()
    st = state()
    title = "This run" if running else "Last run"
    tiles = []
    for cid, kind, what in ev:
        label, var = KINDS[kind]
        if kind.startswith("control"):
            cls = "tile ring" + (" shut" if kind == "control_closed" else "")
            tiles.append(f"<i class='{cls}' title='{esc(cid)}: {esc(what)}'></i>")
        else:
            tiles.append(
                f"<i class='tile' style='background:var({var})' "
                f"title='{esc(cid)}: {esc(label)}'></i>"
            )
    counts = {}
    for _, kind, _ in ev:
        counts[kind] = counts.get(kind, 0) + 1
    legend = "".join(
        f"<span><i class='dot' style='background:var({KINDS[k][1]})'></i>{esc(KINDS[k][0])} "
        f"{counts[k]}</span>"
        for k in KINDS
        if counts.get(k)
    )
    last = st.get("last_case") if running else None
    if not last:
        last = next((cid for cid, kind, _ in reversed(ev) if cid.startswith("q")), None)
    left = (
        f"<div class='card'><h2>{title}: one tile per case, in order</h2>"
        f"<div class='tiles'>{''.join(tiles) or '<span class=muted>Nothing yet</span>'}</div>"
        f"<div class='legend'>{legend}</div>"
        "<div class='muted' style='margin-top:8px;font-size:13px'>Ringed tiles are the "
        "synthetic controls added to each group.</div></div>"
    )
    img = ROOT / "cases" / "evidence" / str(last) / "waterfall.png"
    right = (
        "<div class='card'><h2>Latest plot"
        + (f": {esc(last)}" if last else "")
        + "</h2>"
        + (
            f"<a href='../cases/evidence/{esc(last)}/waterfall.png'><img class='plot' "
            f"src='../cases/evidence/{esc(last)}/waterfall.png' alt='frequency versus time "
            f"for case {esc(last)}'></a>"
            if last and img.exists()
            else "<span class='muted'>No plot yet</span>"
        )
        + "<div class='muted' style='margin-top:6px;font-size:13px'>Frequency across, time "
        "down; one panel per scan, on-target and off-target alternating.</div></div>"
    )
    return f"<div class='two'>{left}{right}</div>"


def spectrum_map(con):
    try:
        rows = con.execute("select freq_mhz, status, note from queue").fetchall()
    except Exception:
        rows = []
    bands = [
        ("L band", 1100, 1900, 34, (1200, 1340)),
        ("S band", 1800, 2800, 104, (2300, 2360)),
    ]

    def x(f, lo, hi):
        return 46 + (f - lo) / (hi - lo) * 930

    out = [
        "<svg viewBox='0 0 1000 150' style='width:100%;height:auto' role='img' "
        "aria-label='Queue cases along the frequency axis'>"
    ]
    for name, lo, hi, y, notch in bands:
        out.append(f"<text x='0' y='{y + 4}'>{name}</text>")
        out.append(
            f"<line x1='46' x2='976' y1='{y + 18}' y2='{y + 18}' stroke='var(--line)'/>"
        )
        out.append(
            f"<rect x='{x(notch[0], lo, hi):.1f}' y='{y - 16}' "
            f"width='{x(notch[1], lo, hi) - x(notch[0], lo, hi):.1f}' height='34' "
            "fill='var(--line)' opacity='.6'><title>receiver filter, not searched</title></rect>"
        )
        for f in range(lo, hi + 1, 100):
            out.append(
                f"<text x='{x(f, lo, hi):.0f}' y='{y + 30}' text-anchor='middle'>{f}</text>"
            )
        layers = {"pending": [], "closed": [], "open": []}
        for f, status, note in rows:
            band_l = f < 1900
            if (name == "L band") != band_l:
                continue
            if status == "closed":
                var = (
                    "--burst"
                    if note == "not_a_tone"
                    else "--nosig"
                    if note == "not_recovered"
                    else "--closed"
                )
                layers["closed"].append((f, var, 22))
            elif status.startswith("open"):
                layers["open"].append((f, "--open", 32))
            else:
                layers["pending"].append((f, "--pending", 10))
        for key in ("pending", "closed", "open"):
            for f, var, h in layers[key]:
                out.append(
                    f"<rect x='{x(f, lo, hi) - 0.7:.1f}' y='{y + 18 - h}' width='1.4' "
                    f"height='{h}' fill='var({var})'/>"
                )
    out.append("</svg>")
    legend = (
        "<div class='legend'>"
        "<span><i class='dot' style='background:var(--pending)'></i>pending</span>"
        "<span><i class='dot' style='background:var(--burst)'></i>burst</span>"
        "<span><i class='dot' style='background:var(--nosig)'></i>no signal</span>"
        "<span><i class='dot' style='background:var(--closed)'></i>interference</span>"
        "<span><i class='dot' style='background:var(--open)'></i>open</span></div>"
    )
    return (
        "<div class='card' style='margin-bottom:14px'><h2>The queue along the spectrum "
        "(MHz)</h2>"
        + "".join(out)
        + legend
        + "<div class='muted' style='margin-top:6px;font-size:13px'>Each tick is one "
        "catalogue hit in the queue. Grey blocks are receiver filter ranges that were "
        "not searched.</div></div>"
    )


def build(con, running):
    return now_panel(running) + tiles_and_plot(running) + spectrum_map(con)
