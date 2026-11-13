"""Deterministic checks around a candidate: lookalikes in the survey hit
tables, clock harmonics, spectrum allocation, sky geometry, expected Doppler
drift. Each returns plain data for the ledger; none returns a verdict."""

import csv
from pathlib import Path

import numpy as np

SITES = {  # lat deg, lon deg east, height m
    "GBT": (38.4331, -79.8398, 807.0),
    "Parkes": (-32.9984, 148.2635, 415.0),
}
C_KMS = 299792.458
# Generic reference oscillators (MHz). Not site-specific knowledge.
CLOCKS_MHZ = [
    1,
    2,
    5,
    10,
    12.5,
    16,
    20,
    25,
    33.3333333,
    40,
    48,
    50,
    64,
    80,
    100,
    125,
    133.3333333,
    150,
    200,
]
_ALLOC = Path(__file__).with_name("allocations.csv")


def lookalikes(con, freq_mhz, drift, target, mjd, window_hz=1000.0, top=12):
    """Hits near this frequency elsewhere in the survey (table hits_u). Hits
    toward this target on the day of the observation are left out: they are the
    candidate itself, and counting them would mark out cases taken from the
    catalogue."""
    w = window_hz * 1e-6
    base = target.upper().removesuffix("_OFF")
    rows = con.execute(
        """select regexp_replace(upper(s.source), '_OFF$', '') tgt, s.mjd, h.freq,
                  h.drift, h.snr
           from hits_u h join scans s using (file_id)
           where h.freq between ? and ?""", [freq_mhz - w, freq_mhz + w]).fetchall()
    other = [r for r in rows if r[0] != base]
    own_other_days = [r for r in rows if r[0] == base and abs(r[1] - mjd) >= 0.5]
    same_day = [r for r in other if abs(r[1] - mjd) < 0.5]
    tol = 0.05 + 0.25 * abs(drift)
    same_drift = [r for r in other if abs(r[3] - drift) <= tol]
    other.sort(key=lambda r: -r[4])
    return {
        "window_hz": window_hz,
        "hits_other_targets": len(other),
        "hits_this_target_other_days": len(own_other_days),
        "other_targets": len({r[0] for r in other}),
        "other_targets_same_day": len({r[0] for r in same_day}),
        "other_targets_similar_drift": len({r[0] for r in same_drift}),
        "other_days": len({int(r[1]) for r in other}),
        "strongest_other": [
            {"target": r[0], "mjd": round(r[1], 5), "freq_mhz": round(r[2], 6),
             "drift": round(float(r[3]), 4), "snr": round(float(r[4]), 1)}
            for r in other[:top]],
    }


def neighbourhood(con, freq_mhz, drift, file_ids, span_khz=50.0):
    """Other catalogue hits in the same cadence within +-span_khz (is it one
    line or a comb?). Hits on the candidate's own path are left out."""
    w = span_khz * 1e-3
    own = (150.0 + abs(drift) * 2500.0) * 1e-6
    ids = ",".join(str(int(i)) for i in file_ids)
    rows = con.execute(
        f"""select file_id, freq from hits where file_id in ({ids})
            and freq between ? and ? and abs(freq - ?) > ?""",
        [freq_mhz - w, freq_mhz + w, freq_mhz, own]).fetchall()
    per_scan = {}
    for fid, _ in rows:
        per_scan[fid] = per_scan.get(fid, 0) + 1
    return {"span_khz": span_khz, "other_hits_in_cadence": len(rows),
            "other_hits_per_scan": [per_scan.get(i, 0) for i in file_ids]}


def harmonics(freq_mhz, tol_khz=5.0):
    """Distance to the nearest integer multiple of common clock frequencies.
    p_chance is the probability a random frequency lands this close."""
    out = []
    for c in CLOCKS_MHZ:
        n = round(freq_mhz / c)
        resid_khz = (freq_mhz - n * c) * 1e3
        out.append(
            {
                "clock_mhz": round(c, 4),
                "multiple": int(n),
                "residual_khz": round(resid_khz, 3),
                "p_chance": round(min(1.0, 2 * abs(resid_khz) / (c * 1e3)), 4),
                "within_tol": abs(resid_khz) <= tol_khz,
            }
        )
    return out


def allocation(freq_mhz):
    rows = []
    with open(_ALLOC, newline="") as f:
        for r in csv.DictReader(row for row in f if not row.startswith("#")):
            if float(r["lo_mhz"]) <= freq_mhz <= float(r["hi_mhz"]):
                rows.append(r)
    return rows


def _altaz(ra_hours, dec_deg, mjd, site):
    from astropy import units as u
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord
    from astropy.time import Time

    lat, lon, h = SITES[site]
    loc = EarthLocation(lat=lat * u.deg, lon=lon * u.deg, height=h * u.m)
    sc = SkyCoord(ra=ra_hours * u.hourangle, dec=dec_deg * u.deg)
    aa = sc.transform_to(AltAz(obstime=Time(mjd, format="mjd"), location=loc))
    return float(aa.alt.deg), float(aa.az.deg), loc, sc


def geometry(ra_hours, dec_deg, mjd, site):
    """Elevation of the target and its angular distance from the geostationary
    belt as seen from the site. Low-orbit satellites need orbit data (not done)."""
    alt, az, _, _ = _altaz(ra_hours, dec_deg, mjd, site)
    lat, lon, h = np.radians(SITES[site][0]), np.radians(SITES[site][1]), SITES[site][2]
    re, rg = 6378.137 + h / 1000, 42164.0
    obs = re * np.array(
        [np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)]
    )
    east = np.array([-np.sin(lon), np.cos(lon), 0.0])
    north = np.array(
        [-np.sin(lat) * np.cos(lon), -np.sin(lat) * np.sin(lon), np.cos(lat)]
    )
    up = obs / np.linalg.norm(obs)
    L = np.radians(np.arange(-180, 180, 0.1))
    sat = rg * np.stack([np.cos(L), np.sin(L), np.zeros_like(L)], axis=1) - obs
    sat /= np.linalg.norm(sat, axis=1, keepdims=True)
    a, z = np.radians(alt), np.radians(az)
    tgt = np.cos(a) * np.sin(z) * east + np.cos(a) * np.cos(z) * north + np.sin(a) * up
    vis = sat @ up > 0
    sep = np.degrees(np.arccos(np.clip(sat[vis] @ tgt, -1, 1)))
    return {
        "site": site,
        "elevation_deg": round(alt, 2),
        "azimuth_deg": round(az, 2),
        "geo_belt_separation_deg": round(float(sep.min()), 2) if vis.any() else None,
        "low_orbit_check": "not done (needs historical orbit data)",
    }


def expected_drift(ra_hours, dec_deg, mjd, site, freq_mhz):
    """Drift (Hz/s) a transmitter at rest relative to the solar system
    barycentre would show at this site and time, from Earth's rotation and
    orbit alone. Ground interference shows 0; low-orbit satellites show much
    larger, fast-changing values."""
    from astropy import units as u
    from astropy.time import Time

    _, _, loc, sc = _altaz(ra_hours, dec_deg, mjd, site)
    t0 = Time(mjd, format="mjd")
    dt = 120.0
    v0 = (
        sc.radial_velocity_correction("barycentric", obstime=t0, location=loc)
        .to(u.km / u.s)
        .value
    )
    v1 = (
        sc.radial_velocity_correction(
            "barycentric", obstime=t0 + dt * u.s, location=loc
        )
        .to(u.km / u.s)
        .value
    )
    # v is the observer's velocity toward the source; approaching raises frequency
    drift = freq_mhz * 1e6 * (v1 - v0) / dt / C_KMS
    return {
        "drift_hz_s": round(float(drift), 4),
        "observer_velocity_kms": round(float(v0), 3),
    }
