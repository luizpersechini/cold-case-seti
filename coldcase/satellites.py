"""Satellite check: did a catalogued satellite pass close to the telescope's
line of sight at the moment an event's signal peaked?

Orbits come from the yearly files of published element sets that Space-Track
points bulk users to (tleYYYY.txt.zip), and names from CelesTrak's public
catalogue (satcat.csv); both are expected in data/satellites/ and neither is
redistributed. Only payloads are used: debris and rocket stages do not
transmit. The catalogue does not say which payloads were working on a given
date, so dead ones are included.

For an event the test window is the time sample in which the track was
strongest, with PAD_S seconds either side. The statistic is the smallest
angle between the line of sight and any payload during the window. How
unusual that is, is measured: the same statistic in NULL_WINDOWS other
windows of the same night, with the telescope still following the star.
Writes cases/evidence/<id>/satellites.json. A rare close pass is flagged by
triage.py and closes nothing: over the open events such passes turned out no
more common than chance, and the catalogue does not say whether a payload
was still working. A satellite well away from the beam can still be picked
up by the telescope's sidelobes; this check cannot see that.

usage: python -m coldcase.satellites index            pick the element sets
                                                      near every open event
       python -m coldcase.satellites CASE_ID ...      check events
       python -m coldcase.satellites --open           every open queue event
       python -m coldcase.satellites selftest         geostationary sanity check
"""

import csv
import json
import sys
import zipfile
from bisect import bisect_left
from datetime import date

import numpy as np

from . import ledger, triage
from .checks import SITES
from .remote import ROOT

DIR = ROOT / "data" / "satellites"
ELEMENTS = DIR / "elements"
NAME = "satellites.json"
PAD_S, STEP_S = 30.0, 2.0
NULL_WINDOWS, NULL_SPAN_H, NULL_GAP_S = 200, 4.0, 300.0
MAX_AGE_DAYS = 7.0  # an element set older or newer than this is not used
NEAR_DEG = 20.0  # inside this, a pass is followed between time steps
R_GEO_KM = 42164.17


def _mjd_jan1(year):
    return (date(year, 1, 1) - date(1858, 11, 17)).days


def payloads():
    """NORAD number -> catalogue row, for payloads."""
    out = {}
    with open(DIR / "satcat.csv", newline="") as fh:
        for r in csv.DictReader(fh):
            if r["OBJECT_TYPE"] == "PAY":
                out[int(r["NORAD_CAT_ID"])] = r
    return out


def events(con, ids=None):
    """(case_id, MJD of the seed scan's start) for open events, or for ids."""
    if ids is None:
        ids = [
            r[0]
            for r in con.execute(
                "select case_id from queue where status in ('open','open_unread') "
                "order by case_id"
            ).fetchall()
        ]
    out = []
    for cid in ids:
        b = json.loads(
            (ROOT / "cases" / "evidence" / cid / "evidence.json").read_text()
        )
        if not b["seed"].get("recovered"):
            continue
        row = next(s for s in b["scans"] if s["pos"] == b["seed"]["seed_pos"])
        out.append((cid, row["mjd"]))
    return out


def index(ids=None, say=print):
    """One pass over the yearly files: for every event, the element set of
    each payload whose epoch is nearest to the event, within MAX_AGE_DAYS.
    Writes data/satellites/elements/<case_id>.json."""
    pay = payloads()
    evs = sorted(events(ledger.connect(), ids), key=lambda e: e[1])
    times = [t for _, t in evs]
    best = [dict() for _ in evs]  # per event: norad -> (age, line1, line2)
    years = sorted(
        {
            (date(1858, 11, 17).toordinal() + int(t + d))
            for t in times
            for d in (-MAX_AGE_DAYS, MAX_AGE_DAYS)
        }
    )
    years = sorted({date.fromordinal(o).year for o in years})
    for year in years:
        path = DIR / f"tle{year}.txt.zip"
        if not path.exists():
            say(
                f"{path.name} is not in data/satellites/; events needing it are skipped"
            )
            continue
        n = kept = 0
        with zipfile.ZipFile(path) as z, z.open(z.infolist()[0]) as fh:
            l1 = None
            for raw in fh:
                line = raw.decode("ascii", "replace").rstrip("\r\n\\ ")
                if line.startswith("1 "):
                    l1 = line
                    continue
                if not line.startswith("2 ") or l1 is None:
                    continue
                n += 1
                try:
                    norad = int(l1[2:7])
                    if norad not in pay:
                        continue
                    yy = int(l1[18:20])
                    mjd = (
                        _mjd_jan1(2000 + yy if yy < 57 else 1900 + yy)
                        + float(l1[20:32])
                        - 1.0
                    )
                except ValueError:
                    continue
                finally:
                    this, l1 = l1, None
                i = bisect_left(times, mjd - MAX_AGE_DAYS)
                while i < len(times) and times[i] <= mjd + MAX_AGE_DAYS:
                    age = abs(times[i] - mjd)
                    old = best[i].get(norad)
                    if old is None or age < old[0]:
                        best[i][norad] = (age, this[:69], line[:69])
                        kept += 1
                    i += 1
        say(f"{path.name}: {n:,} element sets read, {kept:,} improvements kept")
    ELEMENTS.mkdir(parents=True, exist_ok=True)
    for (cid, t), sets in zip(evs, best):
        (ELEMENTS / f"{cid}.json").write_text(
            json.dumps({"mjd": t, "sets": {k: [v[1], v[2]] for k, v in sets.items()}})
        )
        say(
            f"{cid}: {len(sets)} payloads with an element set within {MAX_AGE_DAYS:.0f} days"
        )


def _gmst(mjd):
    """Greenwich mean sidereal time in radians (IAU 1982), taking UT1 = UTC."""
    t = (np.asarray(mjd) - 51544.5) / 36525.0
    sec = (
        67310.54841
        + (876600.0 * 3600.0 + 8640184.812866) * t
        + 0.093104 * t**2
        - 6.2e-6 * t**3
    )
    return np.deg2rad((sec % 86400.0) / 240.0)


def _site(site="GBT"):
    """Observer position (km) and unit zenith in the Earth-fixed frame."""
    from astropy import units as u
    from astropy.coordinates import EarthLocation

    lat, lon, h = SITES[site]
    loc = EarthLocation(lat=lat * u.deg, lon=lon * u.deg, height=h * u.m)
    pos = np.array([loc.x.to_value(u.km), loc.y.to_value(u.km), loc.z.to_value(u.km)])
    la, lo = np.deg2rad(lat), np.deg2rad(lon)
    up = np.array([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)])
    return pos, up


def star_dirs(ra_hours, dec_deg, mjds):
    """Unit vectors toward a star in the Earth-fixed frame, one per time."""
    from astropy import units as u
    from astropy.coordinates import ITRS, SkyCoord
    from astropy.time import Time

    c = SkyCoord(ra=ra_hours * u.hourangle, dec=dec_deg * u.deg, frame="icrs")
    it = c.transform_to(ITRS(obstime=Time(np.asarray(mjds), format="mjd", scale="utc")))
    v = it.cartesian.xyz.value.T
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def sat_dirs(sats, mjds, obs):
    """Unit vectors from the observer toward each satellite and the ranges in
    km: arrays [satellite, time, 3] and [satellite, time]. Failed propagations
    come back as NaN."""
    mjds = np.asarray(mjds, dtype=float)
    jd = np.floor(mjds) + 2400000.5
    fr = mjds - np.floor(mjds)
    e, r, _ = sats.sgp4(jd, fr)
    r = np.where((e != 0)[:, :, None], np.nan, r)
    th = _gmst(mjds)
    c, s = np.cos(th), np.sin(th)
    x = c * r[:, :, 0] + s * r[:, :, 1]
    y = -s * r[:, :, 0] + c * r[:, :, 1]
    d = np.stack([x, y, r[:, :, 2]], axis=2) - obs
    rng = np.linalg.norm(d, axis=2)
    return d / rng[:, :, None], rng


def closest(dirs, star):
    """Smallest angle (degrees) between each satellite and the line of sight
    over the time steps, and the step where it happens. Inside NEAR_DEG the
    path between two steps is followed, since a low satellite can cross a
    degree in two seconds."""
    cosang = np.einsum("stk,tk->st", dirs, star)
    ang = np.degrees(np.arccos(np.clip(cosang, -1.0, 1.0)))
    ang = np.where(np.isnan(ang), 999.0, ang)
    best = ang.min(axis=1)
    step = ang.argmin(axis=1)
    near = np.nonzero((ang < NEAR_DEG).any(axis=1))[0]
    for i in near:
        # tangent-plane offsets from the line of sight, in degrees
        p = np.degrees(dirs[i] - cosang[i][:, None] * star)
        ok = (ang[i][:-1] < NEAR_DEG) & (ang[i][1:] < NEAR_DEG)
        a, b = p[:-1][ok], p[1:][ok]
        if not len(a):
            continue
        ab = b - a
        t = np.clip(
            -(a * ab).sum(axis=1) / np.maximum((ab * ab).sum(axis=1), 1e-12), 0, 1
        )
        dist = np.linalg.norm(a + t[:, None] * ab, axis=1)
        j = int(dist.argmin())
        if dist[j] < best[i]:
            best[i] = dist[j]
            step[i] = np.nonzero(ok)[0][j] + (1 if t[j] > 0.5 else 0)
    return best, step


def _belt_deg(star, obs):
    """Angle between a line of sight and the nearest point of the
    geostationary ring, as seen from the observer."""
    lon = np.deg2rad(np.arange(0.0, 360.0, 0.05))
    ring = (
        R_GEO_KM * np.stack([np.cos(lon), np.sin(lon), np.zeros_like(lon)], axis=1)
        - obs
    )
    ring /= np.linalg.norm(ring, axis=1, keepdims=True)
    return float(np.degrees(np.arccos(np.clip(ring @ star, -1, 1))).min())


def run(case_id, save=True, say=print):
    from sgp4.api import Satrec, SatrecArray

    folder = ROOT / "cases" / "evidence" / case_id
    b = json.loads((folder / "evidence.json").read_text())
    ep = ELEMENTS / f"{case_id}.json"
    out = {"case_id": case_id}

    def done(note=None):
        text, stats = triage.satellite_rule(out)
        if note:
            out["summary"] = {"outcome": "not_checked", "text": note}
        else:
            c = out["closest"][0]
            out["summary"] = {
                "outcome": "pass_close_to_the_beam" if text else "nothing_unusual",
                "text": (
                    f"closest payload {c['name']} at {c['separation_deg']} deg, "
                    f"{c['seconds_from_peak']:+.0f} s from the peak; a pass this close in "
                    f"{out['null']['share_this_close']:.0%} of other times that night"
                ),
                **stats,
                "direct_evidence": text,
            }
        if save:
            (folder / NAME).write_text(json.dumps(out, indent=1))
        say(f"{case_id}: {out['summary']['text']}")
        return out

    if (
        not b["seed"].get("recovered")
        or "pointing" not in b
        or b.get("site", "GBT") != "GBT"
    ):
        return done(
            "not checked: needs a recovered Green Bank event with a known pointing"
        )
    if not ep.exists():
        return done("not checked: no element sets picked for this event (run index)")
    sets = json.loads(ep.read_text())["sets"]
    if not sets:
        return done("not checked: the orbit file for that year is missing")
    pay = payloads()
    row = next(s for s in b["scans"] if s["pos"] == b["seed"]["seed_pos"])
    w = row.get("track_without_strongest_sample") or {}
    at = w.get(
        "sample_left_out_at_s", (row.get("track_time_profile") or {}).get("at_time_s")
    )
    if at is None:
        return done("not checked: the time of the signal's peak is not recorded")
    tsamp = row["tsamp_s"]
    peak = row["mjd"] + (at + tsamp / 2) / 86400.0
    half = tsamp / 2 + PAD_S
    offs = np.arange(-half, half + STEP_S / 2, STEP_S)
    ra, dec = b["pointing"]["ra_hours"], b["pointing"]["dec_deg"]
    obs, up = _site()
    ids = sorted(int(k) for k in sets)
    sats = SatrecArray([Satrec.twoline2rv(*sets[str(k)]) for k in ids])

    def window(centre):
        t = centre + offs / 86400.0
        star = star_dirs(ra, dec, t)
        dirs, rng = sat_dirs(sats, t, obs)
        sep, step = closest(dirs, star)
        return sep, step, rng, star

    sep, step, rng, star = window(peak)
    el0 = float(np.degrees(np.arcsin(star[len(offs) // 2] @ up)))
    order = np.argsort(sep)[:5]
    out.update(
        peak_mjd=round(peak, 6),
        window_s=[round(float(offs[0]), 1), round(float(offs[-1]), 1)],
        payloads_with_orbits=len(ids),
        star_elevation_deg=round(el0, 1),
        geo_belt_deg=round(_belt_deg(star[len(offs) // 2], obs), 2),
        closest=[
            {
                "name": pay[ids[i]]["OBJECT_NAME"],
                "norad": ids[i],
                "owner": pay[ids[i]]["OWNER"],
                "separation_deg": round(float(sep[i]), 3),
                "seconds_from_peak": round(float(offs[step[i]]), 0),
                "range_km": round(float(rng[i, step[i]]), 0),
                "revolutions_per_day": float(sets[str(ids[i])][1][52:63]),
            }
            for i in order
        ],
    )
    # the same statistic at other times of the night, the telescope still on the star
    cand = peak + np.linspace(-NULL_SPAN_H, NULL_SPAN_H, 4 * NULL_WINDOWS) / 24.0
    cand = cand[np.abs(cand - peak) * 86400.0 > NULL_GAP_S]
    el = np.degrees(np.arcsin(star_dirs(ra, dec, cand) @ up))
    cand = cand[(el >= max(10.0, el0 - 15.0)) & (el <= el0 + 15.0)]
    if len(cand) > NULL_WINDOWS:
        cand = cand[np.round(np.linspace(0, len(cand) - 1, NULL_WINDOWS)).astype(int)]
    mins = np.array([float(window(c)[0].min()) for c in cand])
    obs_min = float(sep[order[0]])
    out["null"] = {
        "windows": int(len(mins)),
        "share_this_close": round(float((mins <= obs_min).mean()), 3)
        if len(mins)
        else None,
        "median_closest_deg": round(float(np.median(mins)), 2) if len(mins) else None,
    }
    if len(mins) < 50:
        return done("not checked: too few comparable times that night")
    return done()


def selftest(say=print):
    """Geostationary payloads must hang still in the Earth-fixed frame and
    sit on the geostationary ring as this code draws it. Uses the element
    sets of the first indexed event."""
    from sgp4.api import Satrec, SatrecArray

    ep = sorted(ELEMENTS.glob("*.json"))
    if not ep:
        say("no element sets yet: run index first")
        return False
    d = json.loads(ep[0].read_text())
    geo = [
        k
        for k, (l1, l2) in d["sets"].items()
        if abs(float(l2[52:63]) - 1.0027) < 0.0005
        and float(l2[8:16]) < 0.1
        and float("0." + l2[26:33]) < 0.0005
    ]
    sats = SatrecArray([Satrec.twoline2rv(*d["sets"][k]) for k in geo])
    obs, up = _site()
    t = d["mjd"] + np.array([0.0, 1800.0, 3600.0]) / 86400.0
    dirs, _ = sat_dirs(sats, t, obs)
    vis = dirs[:, 0, :] @ up > 0.1
    move = np.degrees(np.arccos(np.clip((dirs[:, 0] * dirs[:, 2]).sum(axis=1), -1, 1)))
    belt = np.array([_belt_deg(v, obs) for v in dirs[vis, 0]])
    ok = bool(vis.sum() >= 20 and np.median(move[vis]) < 0.1 and np.median(belt) < 0.1)
    say(
        f"{len(geo)} geostationary payloads, {int(vis.sum())} above the horizon: they move "
        f"{np.median(move[vis]):.3f} deg in an hour (median) and sit {np.median(belt):.3f} deg "
        f"from the ring (median). {'PASS' if ok else 'FAIL'}"
    )
    return ok


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    if args[0] == "index":
        index(args[1:] or None)
        return
    if args == ["selftest"]:
        sys.exit(0 if selftest() else 1)
    if args == ["--open"]:
        args = [cid for cid, _ in events(ledger.connect())]
    for cid in args:
        run(cid)


if __name__ == "__main__":
    main()
