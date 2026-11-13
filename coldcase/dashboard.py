"""Write report/dashboard.html: one self-reloading page showing what the
runner has done. Static HTML, no server. The runner refreshes it after every
check; run it by hand any time.

usage: python -m coldcase.dashboard
"""

import html
import json
import re
import subprocess
from collections import Counter
from datetime import datetime

from . import ledger, liveview, readers
from .investigate import code_for
from .remote import ROOT

OUT = ROOT / "report" / "dashboard.html"
LOG = ROOT / "cases" / "runner.log"
REASONS = {
    "rfi_probable": "Interference: seen off-target or toward another star",
    "rfi_identified": "Interference: mechanism named",
    "instrumental": "Instrument artefact",
    "not_a_tone": "A burst, not a tone",
    "not_recovered": "No signal at the catalogue position",
}
PRIORITY = {"high": 0, "medium": 1, "low": 2}
CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1b1f24;--muted:#5f6b7a;--line:#dfe3e8;
--closed:#4f7cac;--open:#d98e2b;--retry:#b8556a;--pending:#cfd5dc;--ok:#2f8f5b;--bad:#b8556a}
@media (prefers-color-scheme: dark){:root{--bg:#14171b;--card:#1d2127;--text:#e6e9ed;
--muted:#9aa5b1;--line:#2c323a;--pending:#3a414b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:22px;margin:0 0 2px}h2{font-size:15px;margin:0 0 10px;font-weight:600}
.sub{color:var(--muted);font-size:13px;margin-bottom:18px}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));margin-bottom:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;overflow-x:auto}
.big{font-size:28px;font-weight:650;line-height:1.1}.muted{color:var(--muted)}
.row{display:flex;justify-content:space-between;gap:12px;padding:3px 0}
.bar{display:flex;height:14px;border-radius:7px;overflow:hidden;background:var(--pending);margin:10px 0 8px}
.bar span{display:block;height:100%}.legend span{display:inline-block;margin-right:14px;font-size:13px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{text-align:left;padding:6px 10px 6px 0;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--muted);font-weight:500}td.wrap{white-space:normal}
button{font:inherit;padding:6px 14px;border-radius:8px;border:1px solid var(--line);background:var(--closed);color:#fff;cursor:pointer}button.stop{background:var(--retry)}
.pill{display:inline-block;padding:1px 8px;border-radius:9px;font-size:12px;border:1px solid var(--line)}
.ok{color:var(--ok)}.bad{color:var(--bad)}a{color:inherit}
"""


def esc(x):
    return html.escape(str(x))


def local(ts):
    try:
        return datetime.fromisoformat(ts).astimezone().strftime("%d %b %H:%M")
    except ValueError:
        return ts


def parse_log():
    """Runs and case events from the runner log."""
    runs, events, cur = [], [], None
    if not LOG.exists():
        return runs, events
    for line in LOG.read_text().splitlines():
        ts, _, msg = line.partition(" ")
        if msg.startswith("run start"):
            cur = {"start": ts, "stop": "", "end": None}
        elif msg.startswith("stopping:") and cur:
            cur["stop"] = msg.split(":", 1)[1].strip()
        elif msg.startswith("run end") and cur:
            m = re.search(
                r"(\d+) groups, (\d+) cases, ([\d.]+) GB, ([\d,]+) tokens, ([\d.]+) min",
                msg,
            )
            if m:
                cur.update(
                    end=ts,
                    groups=int(m[1]),
                    cases=int(m[2]),
                    gb=float(m[3]),
                    tokens=int(m[4].replace(",", "")),
                    minutes=float(m[5]),
                )
                runs.append(cur)
            cur = None
        elif re.match(r"q\d+ ", msg):
            cid, _, what = msg.partition(" ")
            events.append((ts, cid, what))
    return runs, events


def gates():
    from . import runner  # late import: the runner imports this module too

    cfg = runner.config()
    lock = ROOT / "cases" / "runner.lock"
    running = False
    if lock.exists():
        pid = lock.read_text().strip()
        running = (
            pid.isdigit()
            and subprocess.run(["kill", "-0", pid], capture_output=True).returncode == 0
        )
    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    loaded = (
        subprocess.run(
            ["launchctl", "print", f"gui/{uid}/com.coldcase.runner"],
            capture_output=True,
        ).returncode
        == 0
    )
    return {
        "cfg": cfg,
        "running": running,
        "loaded": loaded,
        "idle_min": runner.idle_seconds() / 60,
        "ac": runner.on_ac_power(),
        "month": runner.month_tokens(),
        "key": readers.api_key() is not None,
    }


def open_cases(con):
    rows = []
    for cid, target, f, d, n_on, status, note in con.execute(
        "select case_id, target, freq_mhz, drift, n_on, status, note from queue "
        "where status in ('open', 'open_unread')"
    ).fetchall():
        pr, cs, verd = [], [], []
        for role in ("investigator", "challenger"):
            p = ROOT / "cases" / "verdicts" / f"{code_for(cid)}_{role}.json"
            if p.exists():
                v = json.loads(p.read_text())
                pr.append(str(v.get("followup_priority") or "").lower() or "-")
                verd.append(v.get("verdict", ""))
                if isinstance(v.get("context_suspicion"), (int, float)):
                    cs.append(v["context_suspicion"])
        on = []
        ev = ROOT / "cases" / "evidence" / cid / "evidence.json"
        if ev.exists():
            b = json.loads(ev.read_text())
            on = [
                s["track"]["snr"]
                for s in b["scans"]
                if s["role"] == "ON" and "error" not in s
            ]
        fam = b.get("family") if ev.exists() else None
        related = "" if not fam else f"+{fam['n_lines'] - 1} over {fam['span_khz'] / 1e3:.2f} MHz"
        best = min((PRIORITY.get(x, 3) for x in pr), default=4)
        try:
            from . import rank as rank_mod

            rk = rank_mod.rank_case(cid, con)
        except Exception:
            rk = None
        disputed = any(v != "unresolved" for v in verd)
        rows.append(
            (
                -(rk["combined"] if rk else 0),
                best,
                -max(on, default=0),
                cid,
                target,
                f,
                d,
                on,
                pr,
                cs,
                status,
                note,
                disputed,
                verd,
                related,
                rk,
            )
        )
    return sorted(rows)


def build():
    con = ledger.connect()
    try:
        q = con.execute("select status, note from queue").fetchall()
    except Exception:
        q = []
    total = len(q)
    n = Counter("open" if s.startswith("open") else s for s, _ in q)
    unread = sum(1 for s, _ in q if s == "open_unread")
    events_open = sum(1 for s, _ in q if s in ("open", "open_unread"))
    reasons = Counter(note for s, note in q if s == "closed")
    runs, events = parse_log()
    g = gates()
    b = g["cfg"]["budget"]
    all_tokens = 0
    if readers.SPEND.exists():
        all_tokens = sum(
            json.loads(x)["total_tokens"]
            for x in readers.SPEND.read_text().splitlines()
            if x.strip()
        )
    gb = sum(r["gb"] for r in runs)
    done = n["closed"] + n["open"]
    pct = lambda k: f"{100 * n[k] / total:.2f}%" if total else "0%"  # noqa: E731

    state = (
        "A run is in progress"
        if g["running"]
        else "Waiting for the next check"
        if g["loaded"]
        else "Trigger not loaded"
    )
    last = runs[-1] if runs else None
    gate_rows = [
        (
            "Trigger",
            "loaded, checks every 30 min" if g["loaded"] else "not loaded",
            g["loaded"],
        ),
        (
            "Power",
            "mains" if g["ac"] else "battery",
            g["ac"] or not g["cfg"]["idle"]["require_ac_power"],
        ),
        (
            "Idle",
            f"{g['idle_min']:.0f} min (needs {g['cfg']['idle']['min_idle_minutes']})",
            g["idle_min"] >= g["cfg"]["idle"]["min_idle_minutes"],
        ),
        ("API key", "present" if g["key"] else "missing", g["key"]),
    ]
    month_pct = min(100, 100 * g["month"] / max(1, b["tokens_per_month"]))

    h = [
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<meta http-equiv='refresh' content='{5 if g['running'] else 60}'>"
        "<title>Cold Case runner</title>"
        f"<style>{CSS}{liveview.CSS}</style></head><body><main>",
        "<h1>Cold Case runner</h1>",
        f"<div class='sub'>Updated {datetime.now().astimezone().strftime('%d %b %Y %H:%M:%S')}. "
        + ("A batch is running: the page reloads every 5 seconds.</div>" if g["running"]
           else "The page reloads every minute; the runner rewrites it at every check.</div>"),
        "<div class='grid'>",
    ]
    # state
    h.append(f"<div class='card'><h2>Runner</h2><div class='big'>{esc(state)}</div>")
    if last:
        h.append(
            f"<div class='muted' style='margin:6px 0 8px'>Last run ended {local(last['end'])}: "
            f"{last['cases']} cases in {last['minutes']:.0f} min"
            f"{', stopped: ' + esc(last['stop']) if last['stop'] else ''}</div>"
        )
    for name, val, ok in gate_rows:
        h.append(
            f"<div class='row'><span class='muted'>{name}</span>"
            f"<span class='{'ok' if ok else 'bad'}'>{esc(val)}</span></div>"
        )
    h.append(
        "<div id='ctl' style='display:none;margin-top:12px'>"
        + ("<button class='stop' onclick=\"act('stop')\">Stop the run</button>" if g["running"]
           else "<button onclick=\"act('run')\">Run a batch now</button>")
        + " <span id='ctlmsg' class='muted' style='margin-left:8px'></span></div>"
        "<div id='ctlhint' class='muted' style='margin-top:8px;font-size:13px'>To start a "
        "batch now, double-click <b>Run now.command</b> in the project folder.</div>"
        "<script>if(location.protocol.indexOf('http')===0){"
        "document.getElementById('ctl').style.display='block';"
        "document.getElementById('ctlhint').style.display='none';}"
        "function act(w){fetch('/'+w,{method:'POST',headers:{'X-ColdCase':'1'}})"
        ".then(function(r){return r.text()}).then(function(t){"
        "document.getElementById('ctlmsg').textContent=t;"
        "setTimeout(function(){location.reload()},2500);});}</script>")
    h.append("</div>")
    # queue
    h.append(
        f"<div class='card'><h2>Queue</h2><div class='big'>{done} of {total} "
        f"<span class='muted' style='font-size:15px;font-weight:400'>cases settled</span></div>"
        f"<div class='bar'><span style='width:{pct('closed')};background:var(--closed)'></span>"
        f"<span style='width:{pct('open')};background:var(--open)'></span>"
        f"<span style='width:{pct('retry')};background:var(--retry)'></span></div>"
        f"<div class='legend'><span><i class='dot' style='background:var(--closed)'></i>"
        f"closed {n['closed']}</span><span><i class='dot' style='background:var(--open)'></i>"
        f"open {n['open']} in {events_open} event{'s' if events_open != 1 else ''}"
        f"{f' ({unread} not yet read)' if unread else ''}</span>"
        f"<span><i class='dot' style='background:var(--retry)'></i>retry {n['retry']}</span>"
        f"<span><i class='dot' style='background:var(--pending)'></i>pending {n['pending']}"
        "</span></div></div>"
    )
    # budget
    h.append(
        f"<div class='card'><h2>Budget</h2>"
        f"<div class='row'><span class='muted'>Runner tokens this month</span>"
        f"<span>{g['month']:,} of {b['tokens_per_month']:,}</span></div>"
        f"<div class='bar'><span style='width:{month_pct:.1f}%;background:var(--closed)'>"
        "</span></div>"
        f"<div class='row'><span class='muted'>Per-run limits</span><span>"
        f"{b['tokens_per_run']:,} tokens, {b['network_gb_per_run']} GB, "
        f"{b['minutes_per_run']} min</span></div>"
        f"<div class='row'><span class='muted'>Downloaded by the runner</span>"
        f"<span>{gb:.1f} GB</span></div>"
        f"<div class='row'><span class='muted'>All API tokens, including tests</span>"
        f"<span>{all_tokens:,}</span></div></div>"
    )
    h.append("</div>")
    h.append(liveview.build(con, g["running"]))
    # closed by reason
    h.append(
        "<div class='grid'><div class='card'><h2>Closed by code, by reason</h2><table>"
    )
    for k, v in reasons.most_common():
        h.append(
            f"<tr><td class='wrap'>{esc(REASONS.get(k, k or 'unspecified'))}</td>"
            f"<td style='text-align:right'>{v}</td></tr>"
        )
    if not reasons:
        h.append("<tr><td class='muted'>Nothing closed yet</td></tr>")
    h.append("</table></div>")
    # runs
    h.append(
        "<div class='card'><h2>Recent runs</h2><table><tr><th>Ended</th><th>Cases</th>"
        "<th>GB</th><th>Tokens</th><th>Min</th><th>Stopped because</th></tr>"
    )
    for r in reversed(runs[-15:]):
        h.append(
            f"<tr><td>{local(r['end'])}</td><td>{r['cases']}</td><td>{r['gb']:.2f}</td>"
            f"<td>{r['tokens']:,}</td><td>{r['minutes']:.0f}</td>"
            f"<td class='wrap'>{esc(r['stop'] or 'batch finished')}</td></tr>"
        )
    if not runs:
        h.append("<tr><td class='muted' colspan='6'>No runs yet</td></tr>")
    h.append("</table></div></div>")
    # blind controls
    try:
        from . import controls as controls_mod

        cs_sum = controls_mod.summary(con)
    except Exception:
        cs_sum = {}
    h.append("<div class='card' style='margin-bottom:14px'><h2>Blind controls (synthetic signals "
             "added to each download group)</h2><table><tr><th>Kind</th><th>Band</th>"
             "<th>Injected</th><th>Recovered</th><th>Left open</th>"
             "<th>Closed by a rule</th><th>What is wanted</th></tr>")
    want = {"sky_persistent": "left open", "sky_intermittent": "left open",
            "leak": "closed by a rule"}
    names = {"sky_persistent": "Sky-only, all scans", "sky_intermittent": "Sky-only, some scans",
             "leak": "Weak leak off-target"}
    for (kind, band), d in sorted(cs_sum.items(), key=lambda kv: (kv[0][0], kv[0][1] != "all")):
        h.append(f"<tr><td>{names.get(kind, kind) if band == 'all' else ''}</td>"
                 f"<td>{'all' if band == 'all' else esc(band)}</td><td>{d['n']}</td>"
                 f"<td>{d['recovered']}</td><td>{d['open']}</td><td>{d['closed_by_rule']}</td>"
                 f"<td class='muted'>{want.get(kind, '') if band == 'all' else ''}</td></tr>")
    if not cs_sum:
        h.append("<tr><td class='muted' colspan='7'>No controls yet; they start with the next "
                 "runner batch</td></tr>")
    h.append("</table></div>")
    # open cases
    h.append(
        "<div class='card' style='margin-bottom:14px'><h2>Open cases, ranked for follow-up"
        "</h2><table><tr><th>Case</th><th>Target</th><th>MHz</th><th>Drift Hz/s</th>"
        "<th>Rank</th><th>Related lines</th><th>On-target S/N per scan</th><th>Priority (two readers)</th>"
        "<th>Context suspicion</th><th>Plot</th></tr>"
    )
    oc = open_cases(con)
    for _, _, _, cid, target, f, d, on, pr, cs, status, note, disputed, verd, related, rk in oc:
        if status == "open_unread":
            prio = f"<span class='muted'>not read yet ({esc(note)})</span>"
        elif disputed:
            prio = (
                f"<span class='pill'>readers disagree: {esc(' / '.join(verd))}</span>"
            )
        else:
            prio = esc(" / ".join(pr))
        h.append(
            f"<tr><td>{cid}</td><td>{esc(target)}</td><td>{f:.6f}</td><td>{d:+.3f}</td>"
            f"<td>{'-' if not rk else f'{rk["combined"]:.2f}'}"
            f"{'' if not rk or rk['jev'] is not None else ' <span class=muted>(code only)</span>'}</td>"
            f"<td>{esc(related) or '-'}</td>"
            f"<td>{esc(', '.join(f'{x:.0f}' for x in on))}</td><td>{prio}</td>"
            f"<td>{'-' if not cs else f'{sum(cs) / len(cs):.2f}'}</td>"
            f"<td><a href='../cases/evidence/{cid}/waterfall.png'>plot</a></td></tr>"
        )
    if not oc:
        h.append("<tr><td class='muted' colspan='10'>No open cases yet</td></tr>")
    h.append(
        "</table><div class='muted' style='margin-top:8px;font-size:13px'>Open means no "
        "direct evidence of interference was found, not that the signal is unexplained by "
        "context. Suspicion runs from 0 (quiet context) to 1. Rank runs from 0 to 1, higher meaning more like a steady signal from the target's direction; it only orders this list.</div></div>"
    )
    # activity
    h.append("<div class='card'><h2>Recent activity</h2><table>")
    for ts, cid, what in reversed(events[-30:]):
        what = what.replace("closed: ", "closed, ")
        for k, v in REASONS.items():
            what = what.replace(k, v[0].lower() + v[1:])
        h.append(
            f"<tr><td class='muted'>{local(ts)}</td><td>{cid}</td>"
            f"<td class='wrap'>{esc(what)}</td></tr>"
        )
    if not events:
        h.append("<tr><td class='muted'>Nothing yet</td></tr>")
    h.append("</table></div></main></body></html>")
    OUT.parent.mkdir(exist_ok=True)
    tmp = OUT.with_suffix(".tmp")  # swap in one step so a reload never sees half a page
    tmp.write_text("\n".join(h))
    tmp.replace(OUT)
    return OUT


if __name__ == "__main__":
    print(build())
