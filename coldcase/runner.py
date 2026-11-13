"""Unattended runner. Works through the queue of near-miss hits one data chunk
group at a time: gather evidence, let code decide open or closed, and have the
two readers rank only the cases that stay open.

It runs only when the Mac is idle and on mains power, never overlaps itself,
and stops at whichever limit comes first: tokens per run, tokens per month,
download volume per run, or minutes per run. Limits are in runner.toml.

usage: python -m coldcase.runner build-queue
       python -m coldcase.runner status
       python -m coldcase.runner run [--now] [--force] [--dry-run] [--max-groups N]

--now starts a batch while the Mac is in use (the power rule still applies);
--force skips both the idle and the power rule.
"""

import json
import os
import subprocess
import sys
import threading
import time
import tomllib
from datetime import datetime, timezone

from . import brief, controls, db, investigate, jev, ledger, readers, remote, triage

ROOT = remote.ROOT
CONFIG, LOCK, LOG = (
    ROOT / "runner.toml",
    ROOT / "cases" / "runner.lock",
    ROOT / "cases" / "runner.log",
)
TRACKS = remote.DATA / "derived" / "work" / "tracks_nonrfi_drifting.parquet"
DEFAULTS = {
    "budget": {
        "tokens_per_run": 150_000,
        "tokens_per_month": 1_000_000,
        "network_gb_per_run": 2.5,
        "minutes_per_run": 45,
    },
    "idle": {"min_idle_minutes": 10, "require_ac_power": True},
    "controls": {"per_group": 3},
    "jev": {"tokens_per_month": 5_000_000, "use_plot": True},
}
RESERVE = 35_000  # most one reading has cost, with margin
QUEUE_SQL = f"""
with t as (select * from '{TRACKS}'),
s2 as (select * from t where abs(drift) < 3.9 and (
    (band = 'L' and freq between 1100 and 1900 and not freq between 1200 and 1340) or
    (band = 'S' and freq between 1800 and 2800 and not freq between 2300 and 2360))),
s3 as (select * from s2 where nsrc <= 1),
s4 as (select * from s3 where (cadence, seed_scan) in
    (select cadence, seed_scan from s3 group by 1, 2 having count(*) <= 20))
select cadence, target, band, seed_scan, freq, drift, snr, n_on, cls from s4 where snr >= 25
"""


STATE = ROOT / "cases" / "runner_state.json"
_state = {}


def set_state(**kw):
    """Publish what the run is doing, for the dashboard's live view."""
    _state.update(kw, updated=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(_state))
    tmp.replace(STATE)


def _refresher(stop):
    """Rewrite the dashboard every 5 seconds while a batch is active."""
    from . import dashboard

    while not stop.wait(5):
        try:
            dashboard.build()
        except Exception:
            pass


def config():
    cfg = {k: dict(v) for k, v in DEFAULTS.items()}
    if CONFIG.exists():
        for k, v in tomllib.loads(CONFIG.read_text()).items():
            cfg.setdefault(k, {}).update(v)
    return cfg


def log(msg):
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    with open(LOG, "a") as fh:
        fh.write(line + "\n")


def idle_seconds():
    out = subprocess.run(
        ["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True
    ).stdout
    for line in out.splitlines():
        if "HIDIdleTime" in line:
            return int(line.split("=")[-1].strip()) / 1e9
    return 0.0


def on_ac_power():
    out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True).stdout
    return "AC Power" in out


def month_tokens():
    """Tokens the runner itself has spent this calendar month (UTC)."""
    if not readers.SPEND.exists():
        return 0
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    total = 0
    for line in readers.SPEND.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            if r.get("source") == "runner" and r["ts"].startswith(month):
                total += r["total_tokens"]
    return total


def queue_db():
    con = ledger.connect()
    con.execute("""create table if not exists queue(
        case_id text primary key, grp text, rank int, target text, freq_mhz real, drift real,
        mjd real, cls text, n_on int, snr real, status text, note text, updated text)""")
    con.commit()
    return con


def build_queue():
    hits = db.connect()
    cols = (
        "cadence",
        "target",
        "band",
        "seed_scan",
        "freq",
        "drift",
        "snr",
        "n_on",
        "cls",
    )
    rows = [dict(zip(cols, r)) for r in hits.execute(QUEUE_SQL).fetchall()]
    scan_of = {
        (c, p): (f, m)
        for c, p, f, m in hits.execute(
            "select c.cadence_id, c.pos, c.file_id, s.mjd from cadences c "
            "join scans s using (file_id)"
        ).fetchall()
    }
    done = set()  # real cases already handled in the pilot
    for f in (ROOT / "cases" / "pilot").glob("p*.json"):
        c = json.loads(f.read_text())
        if "inject" not in c:
            done.add((c["target"], round(c["freq_mhz"], 6)))
    groups = {}
    for q in rows:
        if (q["target"], round(q["freq"], 6)) in done:
            continue
        fid, mjd = scan_of[(q["cadence"], int(q["seed_scan"]))]
        coarse = hits.execute(
            "select coarse from hits where file_id = ? order by abs(freq - ?) limit 1",
            [fid, q["freq"]],
        ).fetchall()[0][0]
        q["mjd"] = mjd
        groups.setdefault(f"{q['cadence']}:{int(coarse)}", []).append(q)
    # two-scan cases first, then the groups that settle the most cases per download
    order = sorted(
        groups,
        key=lambda g: (
            -max(x["n_on"] for x in groups[g]),
            -len(groups[g]),
            -max(x["snr"] for x in groups[g]),
        ),
    )
    con = queue_db()
    n = con.execute("select count(*) from queue").fetchone()[0]
    if n:
        print(f"queue already holds {n} cases; not rebuilt")
        return
    i = 0
    for rank, g in enumerate(order):
        for q in sorted(groups[g], key=lambda x: -x["snr"]):
            i += 1
            con.execute(
                "insert into queue values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"q{i:05d}",
                    g,
                    rank,
                    q["target"],
                    q["freq"],
                    float(q["drift"]),
                    q["mjd"],
                    q["cls"],
                    int(q["n_on"]),
                    float(q["snr"]),
                    "pending",
                    "",
                    "",
                ),
            )
    con.commit()
    print(
        f"queue built: {i} cases in {len(order)} download groups "
        f"(about {len(order) * 0.36:.0f} GB of partial reads in total)"
    )


def status():
    con = queue_db()
    rows = con.execute(
        "select status, count(*) from queue group by 1 order by 2 desc"
    ).fetchall()
    cfg = config()
    print("queue:", dict(rows) or "empty (run build-queue)")
    print(
        f"runner tokens this month: {month_tokens():,} of {cfg['budget']['tokens_per_month']:,}"
    )
    print(
        f"idle now: {idle_seconds() / 60:.1f} min; on mains power: {on_ac_power()}; "
        f"API key present: {readers.api_key() is not None}"
    )
    print(f"Jev tokens this month: {jev.month_tokens():,} of "
          f"{cfg['jev']['tokens_per_month']:,}; Jev key present: {jev.api_key() is not None}")
    print("limits:", json.dumps(cfg))


def set_status(con, case_id, status, note=""):
    con.execute(
        "update queue set status=?, note=?, updated=? where case_id=?",
        (
            status,
            note,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            case_id,
        ),
    )
    con.commit()


def read_open_case(client, system, case_id, spent_run, cfg):
    """Both readers on one open case, within the caps. Returns tokens used."""
    used = 0
    code = investigate.code_for(case_id)
    led = ledger.connect()
    for role in ("investigator", "challenger"):
        if (
            spent_run + used + RESERVE > cfg["budget"]["tokens_per_run"]
            or month_tokens() + RESERVE > cfg["budget"]["tokens_per_month"]
        ):
            return used, False
        v = readers.read_case(client, code, role, system[role], source="runner")
        used += v["usage"]["total_tokens"]
        v["case_id"] = case_id
        ledger.add_verdict(led, v)
    return used, True


def _presence(bundle):
    """Which ON scans hold the signal: track S/N of at least 6 and at least
    15% of the case's strongest scan. Control windows are not used here,
    because inside a family the neighbouring lines sit in them."""
    snrs = [s["track"].get("snr") or 0 for s in bundle["scans"]
            if s["role"] == "ON" and "error" not in s]
    top = max(snrs, default=0)
    return tuple(x >= 6 and x >= 0.15 * top for x in snrs)


def _strength(bundle):
    return max((s["track"]["snr"] or 0 for s in bundle["scans"]
                if s["role"] == "ON" and "error" not in s), default=0)


def make_families(items):
    """items: (case_id, bundle) for the open cases of one download group. Hits
    that show up in the same ON scans are one event (one emitter with several
    carriers), so only the strongest is read. Returns lists, strongest first."""
    fams = {}
    for cid, b in items:
        fams.setdefault((b["seed"]["seed_pos"], _presence(b)), []).append((cid, b))
    return [sorted(m, key=lambda x: -_strength(x[1])) for m in fams.values()]


def family_info(members):
    rep = members[0][1]
    f0 = rep["seed"]["freq_mhz"]
    freqs = [b["seed"]["freq_mhz"] for _, b in members]
    return {
        "n_lines": len(members),
        "span_khz": round((max(freqs) - min(freqs)) * 1e3, 1),
        "on_scans_present": list(_presence(rep)),
        "other_lines": [{"offset_khz": round((b["seed"]["freq_mhz"] - f0) * 1e3, 3),
                         "drift": b["seed"]["drift"],
                         "strongest_on_snr": round(_strength(b), 1)} for _, b in members[1:41]],
        "note": "other catalogue hits in the same data chunk that are open and appear in the "
                "same ON scans as this one; all are treated as one event and this is the "
                "strongest line",
    }


def _publish_brief(case):
    (ROOT / "cases" / "queue").mkdir(exist_ok=True)
    path = ROOT / "cases" / "queue" / f"{case['id']}.json"
    path.write_text(json.dumps(case))
    code, _ = brief.build(path)
    keyp = ROOT / "cases" / "briefs" / "KEY.json"
    k = json.loads(keyp.read_text()) if keyp.exists() else {}
    k[code] = str(path)
    keyp.write_text(json.dumps(k, indent=1))


def _set_family(con, members, cases):
    """Settle one family. If its lines sit on a comb at a round clock rate the
    whole event is closed by code (returns None). Otherwise the strongest line
    stands for the event, the rest are marked as members, and its case id is
    returned for reading."""
    rep_id, rep_b = members[0]
    comb = None
    if len(members) >= 4:
        f0 = rep_b["seed"]["freq_mhz"]
        comb = triage.family_comb([(b["seed"]["freq_mhz"] - f0) * 1e6 for _, b in members[1:]])
        if comb and comb["round_clock"]:
            why = (f"D2: {comb['lines_on_comb'] + 1} of {len(members)} lines of this event are "
                   f"spaced by multiples of {comb['spacing_khz']} kHz ({comb['round_clock']}), "
                   "a human clock rate")
            for cid, _ in members:
                ledger.add_verdict(ledger.connect(), {
                    "case_id": cid, "role": "triage", "agent": "code",
                    "verdict": "rfi_identified", "confidence": 1.0, "rationale": why,
                    "evidence_refs": [why]})
                set_status(con, cid, "closed", "rfi_identified")
            log(f"{rep_id} closed with {len(members) - 1} related lines: rfi_identified, "
                f"comb at {comb['spacing_khz']} kHz ({comb['round_clock']})")
            return None
    if len(members) > 1:
        rep_b["family"] = family_info(members)
        if comb:
            rep_b["family"]["comb"] = comb
        (ROOT / "cases" / "evidence" / rep_id / "evidence.json").write_text(
            json.dumps(rep_b, indent=1))
        for cid, _ in members[1:]:
            set_status(con, cid, "open_member", rep_id)
            log(f"{cid} open; same event as {rep_id}")
    _publish_brief(cases[rep_id])
    return rep_id


def _case_from_queue(con, case_id):
    target, f, d, mjd, cls, n_on = con.execute(
        "select target, freq_mhz, drift, mjd, cls, n_on from queue where case_id=?",
        (case_id,)).fetchone()
    return {"id": case_id, "origin": f"queue: {cls}, {n_on} ON scan(s)", "target": target,
            "site": "GBT", "freq_mhz": f, "drift": d, "mjd": mjd, "freq_exact": True}


def regroup():
    """Apply the family rule to open cases that were handled one by one."""
    con = queue_db()
    grps = [r[0] for r in con.execute(
        "select distinct grp from queue where status in ('open', 'open_unread')").fetchall()]
    for g in grps:
        ids = [r[0] for r in con.execute(
            "select case_id from queue where grp=? and status like 'open%'", (g,)).fetchall()]
        items = [(cid, json.loads((ROOT / "cases" / "evidence" / cid / "evidence.json").read_text()))
                 for cid in ids]
        cases = {cid: _case_from_queue(con, cid) for cid in ids}
        for members in make_families(items):
            if len(members) < 2:
                continue
            already = "family" in members[0][1]
            rep_id = _set_family(con, members, cases)
            if rep_id is None or already:
                continue
            # earlier single-line readings of the representative are set aside so
            # it is read again as one event
            code = investigate.code_for(rep_id)
            for role in ("investigator", "challenger"):
                f = ROOT / "cases" / "verdicts" / f"{code}_{role}.json"
                if f.exists():
                    f.rename(f.with_name(f"{code}_{role}_before_family.json"))
            set_status(con, rep_id, "open_unread", "to be read as one event")
            log(f"{rep_id} open; now stands for {len(members)} lines")
    print("regrouped", len(grps), "group(s)")


def _run(force=False, dry=False, max_groups=None, now=False):
    cfg = config()
    b, idle_cfg = cfg["budget"], cfg["idle"]
    if not force:
        if not now and idle_seconds() < idle_cfg["min_idle_minutes"] * 60:
            print(f"not idle long enough ({idle_seconds() / 60:.1f} min); nothing done")
            return
        if idle_cfg["require_ac_power"] and not on_ac_power():
            print("on battery; nothing done")
            return
    con = queue_db()
    groups = [r[0] for r in con.execute(
        "select grp from queue where status in ('pending', 'retry') group by grp "
        "order by min(rank)").fetchall()]
    if dry:
        print(f"would work on up to {max_groups or 'all'} of {len(groups)} pending groups; "
              f"first: {groups[:3]}; runner tokens this month {month_tokens():,}")
        return
    if LOCK.exists():
        pid = LOCK.read_text().strip()
        if pid.isdigit() and subprocess.run(["kill", "-0", pid], capture_output=True).returncode == 0:
            print(f"another run is active (pid {pid}); nothing done")
            return
    LOCK.write_text(str(os.getpid()))
    _state.clear()
    set_state(pid=os.getpid(), step="starting", groups_done=0,
              run_started=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    stop_refresh = threading.Event()
    threading.Thread(target=_refresher, args=(stop_refresh,), daemon=True).start()
    t0, net_mb, tokens, n_groups, n_cases = time.time(), 0.0, 0, 0, 0
    key = readers.api_key()
    client = None
    if key:
        import anthropic

        client = anthropic.Anthropic(api_key=key)
    system = readers.prompts()
    jev_client = None
    if jev.api_key():
        from typesafe_sdk import TypeSafeClient

        jev_client = TypeSafeClient(api_key=jev.api_key(), model=jev.MODEL)
    log(f"run start: {len(groups)} groups pending; limits {json.dumps(b)}")

    def read(case_id):
        nonlocal tokens
        set_state(step="read", case=case_id)
        if client is None:
            set_status(con, case_id, "open_unread", "no API key")
            log(f"{case_id} open; not read (no API key)")
            return True
        try:
            used, done = read_open_case(client, system, case_id, tokens, cfg)
        except Exception as e:
            set_status(con, case_id, "open_unread", f"reader failed: {type(e).__name__}")
            log(f"{case_id} open; reader failed: {type(e).__name__}: {e}"[:200])
            return True
        tokens += used
        set_status(con, case_id, "open" if done else "open_unread", "" if done else "token limit")
        log(f"{case_id} open; {'read' if done else 'not read, token limit'} ({used} tokens)")
        return done

    try:
        # open cases left unread earlier come first
        for (case_id,) in con.execute(
                "select case_id from queue where status='open_unread' order by case_id").fetchall():
            if not read(case_id):
                break
        for g in groups:
            if max_groups is not None and n_groups >= max_groups:
                break
            if time.time() - t0 > b["minutes_per_run"] * 60:
                log("stopping: time limit for this run")
                break
            if net_mb / 1000 + 0.4 > b["network_gb_per_run"]:
                log("stopping: download limit for this run")
                break
            if not force and not now and idle_seconds() < 60:
                log("stopping: the Mac is in use again")
                break
            complete, opened, cases = True, [], {}
            ids = [r[0] for r in con.execute(
                "select case_id from queue where grp=? and status in ('pending', 'retry') "
                "order by case_id", (g,)).fetchall()]
            if ids:
                first = _case_from_queue(con, ids[0])
                set_state(group=g, target=first["target"], freq_mhz=first["freq_mhz"],
                          n_cases=len(ids), i_case=0, step="download", groups_done=n_groups)
            for i_case, case_id in enumerate(ids):
                set_state(case=case_id, i_case=i_case, step="download" if i_case == 0 else "measure")
                case = _case_from_queue(con, case_id)
                try:
                    bundle = investigate.run(case, verbose=False)
                except BaseException as e:
                    if isinstance(e, KeyboardInterrupt):
                        raise
                    if type(e).__name__ == "BrokenProcessPool":  # workers left: a stop
                        raise KeyboardInterrupt from e
                    set_status(con, case_id, "retry", f"{type(e).__name__}: {e}"[:200])
                    log(f"{case_id} evidence failed: {type(e).__name__}: {e}"[:200])
                    complete = False
                    continue
                set_state(last_case=case_id)
                net_mb += sum(s.get("net_mb", 0) for s in bundle["scans"] if "error" not in s)
                if any("error" in s for s in bundle["scans"]):
                    set_status(con, case_id, "retry", "a scan could not be read")
                    complete = False
                    continue
                n_cases += 1
                dec = triage.decide(bundle)
                ledger.add_verdict(ledger.connect(), {
                    "case_id": case_id, "role": "triage", "agent": "code",
                    "verdict": dec["verdict"], "confidence": 1.0,
                    "rationale": "; ".join(dec["direct_evidence"]) or "no direct evidence",
                    "evidence_refs": dec["direct_evidence"]})
                if dec["status"] == "closed":
                    set_status(con, case_id, "closed", dec["verdict"])
                    log(f"{case_id} closed: {dec['verdict']}")
                else:
                    opened.append((case_id, bundle))
                    cases[case_id] = case
            # open hits seen in the same ON scans are one event: read the strongest
            set_state(step="decide")
            to_rank = []
            for members in make_families(opened):
                rep = _set_family(con, members, cases)
                if rep:
                    read(rep)
                    to_rank.append(rep)
            # blind controls ride on the chunks already downloaded for this group
            n_ctrl = cfg["controls"]["per_group"]
            if complete and n_ctrl:
                set_state(step="controls")
            if complete and n_ctrl:
                avoid = [r[0] for r in con.execute(
                    "select freq_mhz from queue where grp=?", (g,)).fetchall()]
                try:
                    for ccase, _, cdec in controls.run_group(g, avoid, n_ctrl):
                        log(f"control {ccase['id']}: {cdec['status']} ({cdec['verdict']})")
                        if cdec["status"] == "open":
                            path = controls.DIR / f"{ccase['id']}.json"
                            code, _ = brief.build(path)
                            keyp = ROOT / "cases" / "briefs" / "KEY.json"
                            k = json.loads(keyp.read_text()) if keyp.exists() else {}
                            k[code] = str(path)
                            keyp.write_text(json.dumps(k, indent=1))
                            to_rank.append(ccase["id"])
                except Exception as e:
                    log(f"controls failed for group {g}: {type(e).__name__}: {e}"[:200])
            # Jev answers its fixed questions about every open event and open control
            if jev_client is not None:
                if to_rank:
                    set_state(step="rank")
                for cid in to_rank:
                    try:
                        jev.score_case(cid, client=jev_client, use_plot=cfg["jev"]["use_plot"],
                                       cap=cfg["jev"]["tokens_per_month"])
                    except Exception as e:
                        log(f"Jev scoring failed for {cid}: {type(e).__name__}: {e}"[:200])
            if complete:
                remote.clear_chunks()
            n_groups += 1
    except KeyboardInterrupt:
        log("stopping: stopped by request")
    finally:
        stop_refresh.set()
        set_state(step="idle")
        LOCK.unlink(missing_ok=True)
        log(f"run end: {n_groups} groups, {n_cases} cases, {net_mb / 1000:.2f} GB, "
            f"{tokens:,} tokens, {(time.time() - t0) / 60:.1f} min")


def run(force=False, dry=False, max_groups=None, now=False):
    """One runner check, then refresh the dashboard whatever happened."""
    try:
        _run(force=force, dry=dry, max_groups=max_groups, now=now)
    finally:
        if not dry:
            try:
                from . import dashboard

                dashboard.build()
            except Exception as e:  # the dashboard must never break a run
                print(f"dashboard not refreshed: {type(e).__name__}: {e}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "build-queue":
        build_queue()
    elif cmd == "status":
        status()
    elif cmd == "regroup":
        regroup()
    elif cmd == "run":
        mg = (
            int(sys.argv[sys.argv.index("--max-groups") + 1])
            if "--max-groups" in sys.argv
            else None
        )
        run(force="--force" in sys.argv, dry="--dry-run" in sys.argv, max_groups=mg,
            now="--now" in sys.argv)


if __name__ == "__main__":
    main()
