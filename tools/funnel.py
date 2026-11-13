"""Cut the non-RFI drifting tracks from strata.py down to a realistic case queue.

Cuts, in order (each documented, none tuned on outcomes):
  band      inside the receiver band and outside its notch filter
            (L: 1100-1900 MHz minus 1200-1340; S: 1800-2800 minus 2300-2360;
             from memory of the published surveys, to be verified in phase 1)
  drift     |drift| < 3.9 Hz/s (edge of the +-4 Hz/s search produces artefacts)
  isolated  no other target in the survey has a hit within about +-1 kHz
  quiet     the seeding scan contributes at most BURST_MAX surviving tracks
            (a scan that sprays hundreds of one-off hits is a bad scan)
  strong    S/N >= 25

usage: funnel.py TRACKS_PARQUET [BURST_MAX=20]
"""

import sys

import duckdb


def main():
    path = sys.argv[1]
    burst_max = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    con = duckdb.connect()
    con.execute(f"create view t as select * from '{path}'")
    con.execute("""create view s1 as select * from t where
        (band = 'L' and freq between 1100 and 1900 and not freq between 1200 and 1340) or
        (band = 'S' and freq between 1800 and 2800 and not freq between 2300 and 2360)""")
    con.execute("create view s2 as select * from s1 where abs(drift) < 3.9")
    con.execute("create view s3 as select * from s2 where nsrc <= 1")
    con.execute(f"""create view s4 as select * from s3 where (cadence, seed_scan) in
        (select cadence, seed_scan from s3 group by 1, 2 having count(*) <= {burst_max})""")
    con.execute("create view s5 as select * from s4 where snr >= 25")
    steps = [
        ("all non-RFI drifting", "t"),
        ("in band, off notch", "s1"),
        ("|drift| < 3.9", "s2"),
        ("isolated", "s3"),
        (f"quiet scan (<= {burst_max})", "s4"),
        ("S/N >= 25", "s5"),
    ]
    classes = ["E3", "S1", "S2", "S2w"]
    print(f"{'step':26}" + "".join(f"{c:>10}" for c in classes) + f"{'S1 n_on=2':>11}")
    for label, v in steps:
        row = dict(con.execute(f"select cls, count(*) from {v} group by 1").fetchall())
        two = con.execute(
            f"select count(*) from {v} where cls = 'S1' and n_on = 2"
        ).fetchall()[0][0]
        print(
            f"{label:26}"
            + "".join(f"{row.get(c, 0):>10}" for c in classes)
            + f"{two:>11}"
        )
    n_t = con.execute(
        "select count(distinct target), count(distinct cadence) from s5"
    ).fetchall()[0]
    print(f"\nfinal queue spans {n_t[0]} targets, {n_t[1]} cadences")
    print("\nstrongest survivors per class (target, band, MHz, Hz/s, S/N, n_on):")
    for c in classes:
        rows = con.execute(f"""select target, band, round(freq, 6), round(drift::double, 3),
            round(snr::double, 1), n_on from s5 where cls = '{c}' order by n_on desc, snr desc
            limit 5""").fetchall()
        for r in rows:
            print(f"  {c:4} {r[0]:14} {r[1]} {r[2]:>12} {r[3]:>7} {r[4]:>9} {r[5]}")


if __name__ == "__main__":
    main()
