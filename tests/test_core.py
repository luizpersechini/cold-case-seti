"""Small tests on synthetic data for the parts that went wrong before: which
way a drift points, where the seed search looks, and the rules' arithmetic.
No network, no stored cases.

    uv run pytest -q
"""

import numpy as np
import pytest

from coldcase import checks, companions, investigate, triage
from coldcase import dedoppler as dd

FOFF_MHZ = 2.7939677238464355e-6  # the high-resolution product: 2.79 Hz
TSAMP = 18.253611008
NT, NF = 16, 4096
F_LOW = 1500.0  # MHz of channel 0
MJD0 = 58000.0
SCAN_S = 300.0


def cutout(t_index, rng):
    """Noise for one five-minute scan; scan t_index starts t_index scans in."""
    return {
        "data": rng.normal(100.0, 1.0, (NT, NF)).astype(np.float32),
        "fch1": F_LOW,
        "foff": FOFF_MHZ,
        "tsamp": TSAMP,
        "tstart": MJD0 + t_index * SCAN_S / 86400.0,
        "source_name": "STAR" if t_index % 2 == 0 else "OTHER",
    }


def cadence(seed=1):
    rng = np.random.default_rng(seed)
    scans = [{"pos": i, "role": "ON" if i % 2 == 0 else "OFF"} for i in range(6)]
    return scans, [cutout(i, rng) for i in range(6)]


def inject(scans, cuts, f0, drift, snr, present_on=(1, 1, 1)):
    investigate.apply_injection(
        {
            "inject": {
                "f0_mhz": f0,
                "drift": drift,
                "snr": snr,
                "present_on": list(present_on),
                "scint": [1.0, 1.0, 1.0],
            }
        },
        scans,
        cuts,
    )


def chan_mhz(k):
    return F_LOW + k * FOFF_MHZ


def test_catalogue_sign_is_opposite_to_the_data():
    assert checks.CATALOGUE_DRIFT_SIGN == -1.0


def test_search_reports_the_drift_with_the_data_sign():
    scans, cuts = cadence()
    f0 = chan_mhz(2000)
    inject(scans, cuts, f0, +0.5, 60)
    c = cuts[0]
    hits = dd.search(
        c["data"], c["foff"] * 1e6, c["tsamp"], max_drift=1.0, snr_min=10, top=3
    )
    assert hits, "the injected tone was not found"
    h = hits[0]
    assert h["drift"] > 0
    assert abs(h["drift"] - 0.5) < 0.05
    assert abs(investigate._freq(c, h["chan"]) - f0) * 1e6 < 10


def test_seed_search_takes_the_tone_at_the_catalogue_position():
    """The hit table lists the drift with the other sign; a stronger line of
    the same drift 600 Hz away is another line, not the candidate."""
    scans, cuts = cadence(2)
    f0 = chan_mhz(2000)
    inject(scans, cuts, f0, 0.3, 40)
    inject(scans, cuts, f0 + 600e-6, 0.3, 200)
    case = {"freq_mhz": f0, "drift": -0.3, "mjd": MJD0, "freq_exact": True}
    _, info = investigate.pick_seed(case, scans, cuts, 5.0)
    assert info["recovered"]
    assert abs(info["offset_from_catalogue_hz"]) < 15
    assert info["drift"] > 0


def test_seed_search_keeps_the_strongest_on_scan():
    """A tone absent from the catalogue's scan but present in the later ON
    scans is found there, where it is strong, not as a weak hit in the
    first scan."""
    scans, cuts = cadence(3)
    f0 = chan_mhz(2000)
    inject(scans, cuts, f0, 0.2, 60, present_on=(0, 1, 1))
    inject(scans, cuts, f0 + 20e-6, 0.2, 14, present_on=(1, 0, 0))
    case = {"freq_mhz": f0, "drift": -0.2, "mjd": MJD0, "freq_exact": True}
    _, info = investigate.pick_seed(case, scans, cuts, 5.0)
    assert info["recovered"]
    assert info["seed_pos"] in (2, 4)
    assert info["snr"] > 30


def test_seed_search_finds_nothing_in_noise():
    scans, cuts = cadence(4)
    case = {"freq_mhz": chan_mhz(2000), "drift": -0.3, "mjd": MJD0, "freq_exact": True}
    _, info = investigate.pick_seed(case, scans, cuts, 5.0)
    assert not info["recovered"]


def test_flash_rule():
    burst = {
        "track_without_strongest_sample": {
            "share_of_full_snr": 0.3,
            "full_snr": 20.0,
            "full_control_max_snr": 10.0,
            "snr": 6.0,
            "control_max_snr": 8.0,
        }
    }
    steady = {
        "track_without_strongest_sample": {
            "share_of_full_snr": 0.9,
            "full_snr": 20.0,
            "full_control_max_snr": 10.0,
            "snr": 18.0,
            "control_max_snr": 8.0,
        }
    }
    assert triage.flash(burst)[0].startswith("D4")
    assert triage.flash(steady) == (None, None)


@pytest.mark.parametrize(
    "there, want, corr, expected",
    [
        (set(), {0}, 0.9, "no distinct line"),
        ({0, 1}, {0}, 0.9, "a line, in other scans than the event"),
        ({0}, {0}, 0.2, "a line in the same scans, not following the event"),
        ({0}, {0}, None, "a line in the same scans, not following the event"),
        ({2}, {2, 4}, 0.8, "companion"),
    ],
)
def test_companion_outcome(there, want, corr, expected):
    assert companions.outcome(there, want, corr) == expected


def _bundle(f0=1600.0):
    return {
        "seed": {"freq_mhz": f0, "seed_pos": 0},
        "scans": [{"pos": 0, "chan_hz": 2.79}],
    }


def _lead(f, r=0.95):
    return {"freq_mhz": f, "outcome": "companion", "correlation_with_event": r}


def test_d8_closes_on_a_local_member():
    comp = {
        "leads": [_lead(1600.011)],
        "members": [
            {
                "freq_mhz": 1600.011,
                "status": "closed",
                "direct_evidence": [
                    "D4: 58% of the track's power is in one time sample"
                ],
            }
        ],
    }
    text, flag = triage.companion_rule(_bundle(), comp)
    assert text and text.startswith("D8")


def test_d8_needs_truly_equal_spacing():
    f0 = 1600.0
    sym = {"leads": [_lead(f0 - 0.011), _lead(f0 + 0.011 + 3e-6)]}
    asym = {"leads": [_lead(f0 - 0.011198), _lead(f0 + 0.010599)]}
    assert triage.companion_rule(_bundle(f0), sym)[0].startswith("D8")
    text, flag = triage.companion_rule(_bundle(f0), asym)
    assert text is None and flag


def test_d8_is_silent_without_companions():
    comp = {"leads": [{"freq_mhz": 1600.02, "outcome": "no distinct line"}]}
    assert triage.companion_rule(_bundle(), comp) == (None, None)


def _inject_one(scan, cut, f0, drift, snr):
    """A tone in one scan only, starting at f0 at that scan's start."""
    investigate.apply_injection(
        {"inject": {"f0_mhz": f0, "drift": drift, "snr": snr, "present_on": [1], "scint": [1.0]}},
        [{"pos": scan["pos"], "role": "ON"}],
        [cut],
    )


def test_follow_line_reaches_an_off_scan_when_the_drift_changes():
    scans, cuts = cadence(5)
    f0 = chan_mhz(2000)
    _inject_one(scans[0], cuts[0], f0, 0.5, 60)
    # in the next scan the same emitter carries on, drifting faster now (a
    # tone smeared over several channels per sample needs more power to show)
    _inject_one(scans[1], cuts[1], f0 + (0.5 * SCAN_S + 30) * 1e-6, 0.8, 150)
    info = {"recovered": True, "freq_mhz": f0, "drift": 0.5, "snr": 60}
    fl = investigate.follow_line(scans, cuts, 0, info)
    assert [x["pos"] for x in fl["chain"]][:2] == [0, 1]
    assert fl["chance"] is not None and fl["chance"] < triage.CHANCE_MAX


def test_follow_line_stops_in_noise():
    scans, cuts = cadence(6)
    f0 = chan_mhz(2000)
    _inject_one(scans[0], cuts[0], f0, 0.5, 60)
    info = {"recovered": True, "freq_mhz": f0, "drift": 0.5, "snr": 60}
    fl = investigate.follow_line(scans, cuts, 0, info)
    assert fl["off_scans_reached"] == 0 and fl["chance"] is None


def test_d6_finds_an_event_and_its_companions_on_one_clock():
    from coldcase import harmonics

    clk = 24.0 * (1 - 108e-6)
    lines = [
        {"id": i, "target": "T", "f": n * clk, "drift": -0.07, "scan": "s", "mjd": 1.0, "pos": 0}
        for i, n in (("event", 59), ("c1", 58), ("c2", 62))
    ]
    fams = harmonics.find_families(lines)
    assert len(fams) == 1 and fams[0]["passes"]
    assert fams[0]["clock_mhz"] == 24 and abs(fams[0]["ppm"] + 108) < 0.01
    assert sorted(x["n"] for x in fams[0]["lines"]) == [58, 59, 62]


def test_d6_ignores_lines_off_the_grid():
    from coldcase import harmonics

    clk = 24.0 * (1 - 108e-6)
    lines = [
        {"id": "event", "target": "T", "f": 59 * clk, "drift": 0, "scan": "s", "mjd": 1.0, "pos": 0},
        {"id": "c1", "target": "T", "f": 58 * clk + 0.0137, "drift": 0, "scan": "s", "mjd": 1.0, "pos": 0},
    ]
    assert not any(f["passes"] for f in harmonics.find_families(lines))


def test_a_steady_fast_tone_is_not_a_burst():
    """A tone drifting 4 Hz/s crosses about 26 channels in one sample; its
    power must come out spread over the samples, not in one of them."""
    for seed in range(5):
        scans, cuts = cadence(10 + seed)
        f0 = chan_mhz(1000)
        inject(scans, cuts, f0, 4.0, 40 * 27)
        c = cuts[0]
        tp = dd.time_profile(c["data"], c["foff"] * 1e6, c["tsamp"], 1000, 4.0)
        assert tp["largest_single_sample_share"] < triage.BURST_SHARE, tp

