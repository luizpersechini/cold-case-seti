"""Write report/dossier.html: every open event on one page, for a person to
review. Each event has its plot, the measurements per scan, the flags, the
repeat check and what the two readers wrote. Events are ordered by the
readers' rating and then by signal strength. Static HTML; nothing is sent
anywhere.

usage: python -m coldcase.dossier
"""

import json
from collections import Counter
from datetime import datetime, timedelta

from . import ledger, triage
from .dashboard import CSS, esc
from .investigate import code_for
from .remote import ROOT

OUT = ROOT / "report" / "dossier.html"
PRIORITY = {"high": 0, "medium": 1, "low": 2}
CLOSING = {
    "rfi_probable": "interference",
    "rfi_identified": "interference",
    "instrumental": "instrument artefact",
    "not_a_tone": "a burst",
    "not_recovered": "no signal",
    "known_spacecraft": "a spacecraft",
}
ROLES = (("investigator", "Reader 1"), ("challenger", "Reader 2, checking the first"))
MORE_CSS = """
.event{margin-bottom:14px}.event h2{font-size:17px;margin-bottom:6px}
.facts{display:grid;gap:4px 18px;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));
margin:8px 0 12px;font-size:13.5px}.facts b{font-weight:500;color:var(--muted)}
.event img{max-width:100%;border:1px solid var(--line);border-radius:6px;margin:10px 0}
.reader{border-top:1px solid var(--line);padding-top:10px;margin-top:10px;font-size:14px}
.reader p{margin:6px 0}.flag{color:var(--open)}ul{margin:6px 0;padding-left:20px}
td.on{font-weight:600}.top{font-size:13px;float:right}
"""


def _date(mjd):
    return (datetime(1858, 11, 17) + timedelta(days=mjd)).strftime("%d %b %Y %H:%M UTC")


def _rating(v):
    """(sort value, words) for one reading."""
    if v.get("verdict") == "unresolved":
        p = str(v.get("followup_priority") or "").lower()
        return PRIORITY.get(p, 3), p or "no rating"
    return 3, CLOSING.get(v.get("verdict"), str(v.get("verdict")))


def readings(case_id):
    out = []
    for role, label in ROLES:
        p = ROOT / "cases" / "verdicts" / f"{code_for(case_id)}_{role}.json"
        if p.exists():
            out.append((label, json.loads(p.read_text())))
    return out


def events(con):
    """One dict per open event (the strongest line of each), sorted."""
    hp = ROOT / "cases" / "harmonics.json"
    clock = {}
    for x in (json.loads(hp.read_text()) if hp.exists() else {}).get("flags", []):
        clock.setdefault(x["id"], []).append(x["text"])
    out = []
    for cid, target, f, d, status in con.execute(
        "select case_id, target, freq_mhz, drift, status from queue "
        "where status in ('open', 'open_unread') order by case_id"
    ).fetchall():
        ev = ROOT / "cases" / "evidence" / cid
        b = json.loads((ev / "evidence.json").read_text())
        reads = readings(cid)
        rates = [_rating(v) for _, v in reads]
        on = [
            s["track"]["snr"] or 0
            for s in b["scans"]
            if s["role"] == "ON" and "error" not in s
        ]
        try:
            flags = list(triage.decide_case(cid).get("flags", []))
        except Exception:
            flags = []
        rp, sp = ev / "repeat.json", ev / "session.json"
        tp = ev / "satellites.json"
        try:
            from . import rank

            rk = rank.rank_case(cid)
        except Exception:
            rk = None
        out.append(
            {
                "id": cid,
                "target": target,
                "freq": f,
                "status": status,
                "bundle": b,
                "reads": reads,
                "rating": " / ".join(r[1] for r in rates) or "not read",
                "sort": (
                    min((r[0] for r in rates), default=4),
                    sum(r[0] for r in rates) if rates else 9,
                    -max(on, default=0),
                ),
                "flags": flags + clock.get(cid, []),
                "repeat": json.loads(rp.read_text())["summary"]["text"]
                if rp.exists()
                else "not checked",
                "session": json.loads(sp.read_text())["summary"]["text"]
                if sp.exists()
                else "not checked",
                "session_plot": sp.exists() and (ev / "session.png").exists(),
                "satellites": json.loads(tp.read_text())["summary"]["text"]
                if tp.exists()
                else "not checked",
                "members": con.execute(
                    "select case_id, freq_mhz from queue where status='open_member' "
                    "and note=? order by freq_mhz",
                    (cid,),
                ).fetchall(),
                "rank": rk,
            }
        )
    return sorted(out, key=lambda e: e["sort"])


def _scans(b):
    seed = b["seed"]["seed_pos"]
    h = [
        "<table><tr><th>Scan</th><th>Pointing</th><th>Signal on the track</th>"
        "<th>Strongest control</th><th>Margin</th><th></th></tr>"
    ]
    for s in b["scans"]:
        on = s["role"] == "ON"
        where = "on the star" if on else f"off the star ({esc(s.get('source') or '?')})"
        if "error" in s:
            h.append(
                f"<tr><td>{s['pos'] + 1}</td><td>{where}</td>"
                f"<td class='muted' colspan='4'>not measured: {esc(s['error'])}</td></tr>"
            )
            continue
        snr, cw = s["track"]["snr"], s.get("control_windows") or {}
        mx = cw.get("max_snr")
        margin = "" if snr is None or mx is None else f"{snr - mx:+.1f}"
        note = []
        if s["pos"] == seed:
            note.append("where it was found")
            share = (s.get("track_time_profile") or {}).get(
                "largest_single_sample_share"
            )
            if share is not None:
                note.append(f"{share:.0%} of its power in one moment")
            w = s.get("track_without_strongest_sample")
            if w:
                note.append(
                    f"without that moment {w['snr']} against {w['control_max_snr']}"
                )
        h.append(
            f"<tr><td>{s['pos'] + 1}</td><td{' class=on' if on else ''}>{where}</td>"
            f"<td>{'-' if snr is None else snr}</td><td>{'-' if mx is None else mx}</td>"
            f"<td>{margin}</td><td class='wrap muted'>{esc('; '.join(note))}</td></tr>"
        )
    h.append("</table>")
    st = b.get("off_scans_stacked")
    if st:
        h.append(
            "<div class='muted' style='font-size:13px;margin-top:6px'>Off-star scans "
            f"summed: {st['sum_track_snr']} against {st['control_sum_max']} at the "
            "strongest control position.</div>"
        )
    return "".join(h)


def _facts(e):
    b = e["bundle"]
    seed, geo = b["seed"], b.get("geometry") or {}
    row = next(s for s in b["scans"] if s["pos"] == seed["seed_pos"])
    on = [s for s in b["scans"] if s["role"] == "ON" and "error" not in s]
    seen = sum(1 for s in on if s.get("track_exceeds_all_controls"))
    use = "; ".join(a.get("service", "") for a in b.get("allocation") or [])
    l1, l10 = b.get("lookalikes_1khz") or {}, b.get("lookalikes_10khz") or {}
    items = [
        ("Frequency", f"{seed['freq_mhz']:.6f} MHz"),
        ("Drift", f"{seed['drift']:+.4f} Hz/s"),
        ("Observed", _date(row["mjd"]) if row.get("mjd") else "-"),
        ("Above its controls in", f"{seen} of {len(on)} on-star scans"),
        ("Band use (table from memory)", use or "no entry"),
        (
            "Telescope elevation",
            f"{geo['elevation_deg']:.0f} degrees" if "elevation_deg" in geo else "-",
        ),
        (
            "Catalogue hits toward other stars",
            f"{l1.get('other_targets', 0)} stars within 1 kHz, "
            f"{l10.get('other_targets', 0)} within 10 kHz",
        ),
        ("Other days", e["repeat"]),
        ("Other stars, same half hour", e["session"]),
        ("Satellites near the line of sight", e["satellites"]),
        ("Rank score", "-" if e["rank"] is None else f"{e['rank']:.2f}"),
    ]
    if e["members"]:
        items.append(
            (
                "Other lines of this event",
                ", ".join(f"{cid} at {f:.6f} MHz" for cid, f in e["members"]),
            )
        )
    return (
        "<div class='facts'>"
        + "".join(f"<div><b>{esc(k)}</b><br>{esc(v)}</div>" for k, v in items)
        + "</div>"
    )


def _reader(label, v):
    rate = _rating(v)[1]
    head = (
        f"priority {rate}"
        if v.get("verdict") == "unresolved"
        else f"would close it as {rate}"
    )
    cs = v.get("context_suspicion")
    if isinstance(cs, (int, float)):
        head += f", suspicion {cs:.2f}"
    h = [f"<div class='reader'><b>{esc(label)}</b>: {esc(head)}"]
    if v.get("mechanism"):
        h.append(f"<p class='muted'>Mechanism named: {esc(v['mechanism'])}</p>")
    if v.get("direct_evidence"):
        h.append(
            "<p>Evidence cited: "
            + esc("; ".join(str(x) for x in v["direct_evidence"]))
            + "</p>"
        )
    h.append(f"<p>{esc(v.get('rationale', ''))}</p>")
    if v.get("what_you_saw_in_plot"):
        h.append(f"<p class='muted'>In the plot: {esc(v['what_you_saw_in_plot'])}</p>")
    for key, title in (
        ("errors_in_first_verdict", "Errors found in the first reading"),
        ("checks_missing", "Checks they would want"),
    ):
        if v.get(key):
            h.append(
                f"<p class='muted'>{title}:</p><ul class='muted'>"
                + "".join(f"<li>{esc(x)}</li>" for x in v[key])
                + "</ul>"
            )
    h.append("</div>")
    return "".join(h)


def card(e, n):
    plot = f"../cases/evidence/{e['id']}/waterfall.png"
    h = [
        f"<div class='card event' id='{e['id']}'><a class='top muted' href='#top'>top</a>"
        f"<h2>{n}. {e['id']} &middot; {esc(e['target'])} &middot; {e['freq']:.6f} MHz "
        f"<span class='pill'>{esc(e['rating'])}</span></h2>",
        _facts(e),
    ]
    if e["flags"]:
        h.append(
            "<div class='flag'>Flagged, not closed:</div><ul>"
            + "".join(f"<li>{esc(x)}</li>" for x in e["flags"])
            + "</ul>"
        )
    h.append(_scans(e["bundle"]))
    h.append(
        f"<a href='{plot}'><img loading='lazy' src='{plot}' alt='Frequency against "
        f"time for the six scans of {e['id']}'></a>"
    )
    if e["session_plot"]:
        h.append(
            f"<div style='font-size:13.5px'><a href='../cases/evidence/{e['id']}/"
            "session.png'>Plot of the same frequency toward the other stars observed "
            "within half an hour</a></div>"
        )
    for label, v in e["reads"]:
        h.append(_reader(label, v))
    if not e["reads"]:
        h.append("<div class='reader muted'>Not read yet.</div>")
    h.append("</div>")
    return "".join(h)


def build():
    con = ledger.connect()
    evs = events(con)
    hits = len(evs) + sum(len(e["members"]) for e in evs)
    counts = Counter(e["rating"] for e in evs)
    h = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>Cold Case open events</title>",
        f"<style>{CSS}{MORE_CSS}</style></head><body><main id='top'>",
        "<h1>Cold Case: open events</h1>",
        f"<div class='sub'>{len(evs)} events ({hits} catalogue hits) left open by the "
        f"code rules. Written {datetime.now().strftime('%d %b %Y %H:%M')}. "
        "<a href='dashboard.html'>Back to the dashboard</a></div>",
        "<div class='card' style='margin-bottom:14px'><h2>How to read this</h2>"
        "<div style='font-size:14px'>Open means no direct evidence of interference was "
        "found. It does not mean the signal is unexplained. Each event was measured in "
        "six scans, three on the star and three off it, and compared with about 45 "
        "control positions per scan; a margin above zero means the track is stronger "
        "than every control. The two readers only rate how much an event deserves a "
        "look; when one of them would close it, the code rules did not confirm their "
        "reason. Suspicion runs from 0 (quiet context) to 1.</div>"
        "<div class='legend' style='margin-top:10px'>"
        + "".join(f"<span><b>{v}</b> {esc(k)}</span>" for k, v in counts.most_common())
        + "</div></div>",
        "<div class='card' style='margin-bottom:14px'><h2>Index</h2><table>"
        "<tr><th>#</th><th>Case</th><th>Star</th><th>MHz</th><th>Readers</th>"
        "<th>Strongest on-star scan</th><th>Flags</th></tr>",
    ]
    for n, e in enumerate(evs, 1):
        h.append(
            f"<tr><td>{n}</td><td><a href='#{e['id']}'>{e['id']}</a></td>"
            f"<td>{esc(e['target'])}</td><td>{e['freq']:.6f}</td>"
            f"<td>{esc(e['rating'])}</td><td>{-e['sort'][2]:.1f}</td>"
            f"<td>{len(e['flags']) or ''}</td></tr>"
        )
    h.append("</table></div>")
    h += [card(e, n) for n, e in enumerate(evs, 1)]
    h.append("</main></body></html>")
    OUT.parent.mkdir(exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text("\n".join(h))
    tmp.replace(OUT)
    return OUT


if __name__ == "__main__":
    print(build())
