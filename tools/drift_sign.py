"""Which way do the hit tables' drift rates point?

A line that drifts is catalogued again, at a shifted frequency, when the
telescope returns to the star one scan later. If the catalogue's drift d
(Hz/s) had the usual sign, the line would be found d * dt higher after dt
seconds; if the sign is flipped, d * dt lower. This counts both among lines
that are catalogued in two on-star scans of one cadence, about 11 minutes
apart, using only the hit tables.

A pair counts when the first hit has S/N 20 or more and a drift of 0.05 to
1 Hz/s either way, and exactly one hit of similar drift lies within 1 kHz of
it in the later scan, so the match is not a guess in a crowd.

usage: python tools/drift_sign.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coldcase import db  # noqa: E402

SQL = """
with pairs as (
  select c0.cadence_id, c0.file_id as fa, c2.file_id as fb,
         (s2.mjd - s0.mjd) * 86400 as dt, s0.band
  from cadences c0
  join cadences c2 on c2.cadence_id = c0.cadence_id and c2.pos = c0.pos + 2
  join scans s0 on s0.file_id = c0.file_id
  join scans s2 on s2.file_id = c2.file_id
  where c0.pos in (0, 2)
),
m as (
  select p.band, p.fa, a.freq as f_a, a.drift as d, p.dt,
         (b.freq - a.freq) * 1e6 as df
  from pairs p
  join hits_u a on a.file_id = p.fa
  join hits_u b on b.file_id = p.fb
       and b.freq between a.freq - 0.001 and a.freq + 0.001
  where a.snr >= 20 and abs(a.drift) between 0.05 and 1.0
    and abs(b.drift - a.drift) <= 0.02 + 0.1 * abs(a.drift)
),
u as (
  select band, any_value(d) as d, any_value(dt) as dt, any_value(df) as df
  from m group by band, fa, f_a having count(*) = 1
)
select band,
       count(*) as pairs,
       sum(case when abs(df - d * dt) <= 15 + 0.15 * abs(d) * dt
                 and abs(df + d * dt) > 15 + 0.15 * abs(d) * dt then 1 else 0 end)
         as moves_with_the_catalogue_sign,
       sum(case when abs(df + d * dt) <= 15 + 0.15 * abs(d) * dt
                 and abs(df - d * dt) > 15 + 0.15 * abs(d) * dt then 1 else 0 end)
         as moves_against_it
from u group by band order by band
"""


def main(say=print):
    con = db.connect()
    tot = [0, 0, 0]
    for band, n, with_, against in con.execute(SQL).fetchall():
        say(
            f"band {band}: {n} lines catalogued in two on-star scans; "
            f"{with_} moved the way the catalogue's drift says, {against} the other way"
        )
        tot = [tot[0] + n, tot[1] + int(with_), tot[2] + int(against)]
    decided = tot[1] + tot[2]
    say(
        f"all: {tot[0]} lines, {decided} moved by the amount their drift predicts; "
        f"{tot[2]} of those against the catalogue's sign "
        f"({tot[2] / decided:.1%})"
        if decided
        else "no pair could be decided"
    )
    return tot


if __name__ == "__main__":
    main()
