"""Shared access to the survey hit database built by tools/strata.py."""

import duckdb

from .remote import DATA

DB_PATH = DATA / "derived" / "work" / "hits.duckdb"
MAX_GAP_S = 15 * 60
SCAN_COLS = ("file_id", "name", "source", "mjd", "band", "n_hits", "ra", "dec")


def connect(read_only=True):
    return duckdb.connect(str(DB_PATH), read_only=read_only)


def unique_scans(con):
    """Scans sorted by time, one per (source, start time, band). The archive
    ships every scan twice."""
    rows = con.execute(
        f"select {', '.join(SCAN_COLS)} from scans where mjd is not null "
        "and source is not null order by mjd, file_id"
    ).fetchall()
    seen, uniq = set(), []
    for r in rows:
        d = dict(zip(SCAN_COLS, r))
        key = (d["source"], round(d["mjd"], 5), d["band"])
        if key not in seen:
            seen.add(key)
            uniq.append(d)
    return uniq


def find_cadences(scans):
    """Greedy detection of six-scan ON/OFF cadences (A B A C A D, or
    A A_OFF A A_OFF A A_OFF) in a time-sorted scan list."""
    out, i, n = [], 0, len(scans)
    while i + 5 < n:
        s = scans[i : i + 6]
        a = s[0]["source"]
        ok = (
            not a.upper().endswith("_OFF")
            and s[2]["source"] == a
            and s[4]["source"] == a
            and all(s[k]["source"] != a for k in (1, 3, 5))
            and len({x["band"] for x in s}) == 1
            and all(
                (s[k + 1]["mjd"] - s[k]["mjd"]) * 86400 < MAX_GAP_S for k in range(5)
            )
        )
        if ok:
            out.append(s)
            i += 6
        else:
            i += 1
    return out


def write_cadences(con, uniq, cads):
    """Persist cadence membership and a de-duplicated, frequency-sorted hit table."""
    con.execute(
        "create or replace table cadences (cadence_id int, pos int, file_id int)"
    )
    rows = [(ci, p, s["file_id"]) for ci, c in enumerate(cads) for p, s in enumerate(c)]
    con.executemany("insert into cadences values (?, ?, ?)", rows)
    con.execute("create or replace temp table uniq_ids (file_id int)")
    con.executemany("insert into uniq_ids values (?)", [(s["file_id"],) for s in uniq])
    con.execute(
        "create or replace table hits_u as select h.* from hits h "
        "join uniq_ids using (file_id) order by freq"
    )


def cadence_for(con, target, mjd):
    """The cadence whose ON target matches and that contains time `mjd`
    (within 20 minutes of one of its scans). Returns six scan dicts or None."""
    rows = con.execute(
        f"""with c as (
              select c.cadence_id from cadences c join scans s using (file_id)
              where upper(s.source) = upper(?) and abs(s.mjd - ?) < 20.0/1440
              order by abs(s.mjd - ?) limit 1)
            select c2.pos, {", ".join("s." + x for x in SCAN_COLS)}, c2.cadence_id
            from cadences c2 join scans s using (file_id)
            where c2.cadence_id = (select cadence_id from c) order by c2.pos""",
        [target, mjd, mjd],
    ).fetchall()
    if len(rows) != 6:
        return None
    return [dict(zip(("pos",) + SCAN_COLS + ("cadence_id",), r)) for r in rows]
