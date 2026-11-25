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


def test_d8_flags_sidebands_and_closes_nothing_on_them():
    f0 = 1600.0
    sym = {"leads": [_lead(f0 - 0.011), _lead(f0 + 0.011 + 3e-6)]}
    asym = {"leads": [_lead(f0 - 0.011198), _lead(f0 + 0.010599)]}
    text, flag = triage.companion_rule(_bundle(f0), sym)
    assert text is None and "sidebands" in flag  # modulation is not locality
    text, flag = triage.companion_rule(_bundle(f0), asym)
    assert text is None and flag and "sidebands" not in flag


def test_d8_is_silent_without_companions():
    comp = {"leads": [{"freq_mhz": 1600.02, "outcome": "no distinct line"}]}
    assert triage.companion_rule(_bundle(), comp) == (None, None)


def _inject_one(scan, cut, f0, drift, snr):
    """A tone in one scan only, starting at f0 at that scan's start."""
    investigate.apply_injection(
        {
            "inject": {
                "f0_mhz": f0,
                "drift": drift,
                "snr": snr,
                "present_on": [1],
                "scint": [1.0],
            }
        },
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
        {
            "id": i,
            "target": "T",
            "f": n * clk,
            "drift": -0.07,
            "scan": "s",
            "mjd": 1.0,
            "pos": 0,
        }
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
        {
            "id": "event",
            "target": "T",
            "f": 59 * clk,
            "drift": 0,
            "scan": "s",
            "mjd": 1.0,
            "pos": 0,
        },
        {
            "id": "c1",
            "target": "T",
            "f": 58 * clk + 0.0137,
            "drift": 0,
            "scan": "s",
            "mjd": 1.0,
            "pos": 0,
        },
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


def _flash(cut, k, sample=5, amp=300.0):
    """A one-sample flash a few channels wide."""
    cut["data"][sample, k - 1 : k + 2] += amp


def test_seed_search_passes_over_a_flash_for_a_steady_tone():
    """A stronger flash in the seed's window is not taken for the event when a
    steady tone of at least half its S/N is there too."""
    scans, cuts = cadence(20)
    k0 = 2000
    f0 = chan_mhz(k0)
    inject(scans, cuts, f0 + 25e-6, 0.3, 90)
    _flash(cuts[0], k0)
    case = {"freq_mhz": f0, "drift": -0.3, "mjd": MJD0, "freq_exact": True}
    i, info = investigate.pick_seed(case, scans, cuts, 5.0)
    assert info["recovered"]
    assert "flash_passed_over" in info, info
    assert info["flash_passed_over"]["snr"] > info["snr"]
    assert abs(info["offset_from_catalogue_hz"] - 25) < 15
    tp = dd.time_profile(
        cuts[i]["data"],
        FOFF_MHZ * 1e6,
        TSAMP,
        round((info["freq_mhz"] - F_LOW) / FOFF_MHZ),
        info["drift"],
    )
    assert tp["largest_single_sample_share"] <= investigate.FLASH_SHARE


def test_seed_search_keeps_a_flash_alone():
    scans, cuts = cadence(21)
    k0 = 2000
    _flash(cuts[0], k0)
    case = {"freq_mhz": chan_mhz(k0), "drift": -0.3, "mjd": MJD0, "freq_exact": True}
    _, info = investigate.pick_seed(case, scans, cuts, 5.0)
    assert info["recovered"]
    assert "flash_passed_over" not in info


def test_followup_events():
    import pytest

    from coldcase import followup

    if not (followup.TABLES / "lband2019" / "lband2019_events.csv").exists():
        pytest.skip("survey event tables not downloaded")
    ev = followup.events()
    assert len(ev) == 34
    assert sum(e["band"] == "L" for e in ev) == 32
    assert all(57000 < e["mjd"] < 58500 for e in ev)
    # the two-hit event is entered at its stronger hit
    two = [e for e in ev if e["n_hits"] == 2][0]
    assert two["freq_mhz"] == 2257.476725


def test_probe_finds_the_line_in_one_scan_of_another_star(monkeypatch):
    from coldcase import probe

    monkeypatch.setattr(probe, "WINDOW_HZ", 50.0)
    rng = np.random.default_rng(7)
    nf, k0, step = 16384, 8192, 1100
    f0 = chan_mhz(k0)
    nulls = [chan_mhz(k0 + s * m * step) for m in range(1, 6) for s in (-1, 1)]
    rows = []
    for i in range(6):
        cut = cutout(i, rng)
        cut["data"] = rng.normal(100.0, 1.0, (NT, nf)).astype(np.float32)
        if i == 2:
            _inject_one({"pos": i}, cut, f0, -0.12, 60.0)
        got = probe.measure(cut, [f0] + nulls, -0.12, 0.08)
        rows.append({"source": f"S{i}", "event": got[0], "nulls": got[1:]})
    s = probe.summarise(rows)
    assert s["scans_measured"] == 6 and s["null_trials"] == 60
    assert s["scans_matched"] == 1 and s["stars_matched"] == ["S2"]
    assert rows[2]["event"]["margin"] >= triage.OFF_MARGIN
    assert s["chance_of_this_many"] < 0.5


def test_probe_count_near_keeps_window_time_and_drift():
    from coldcase import probe

    f, t = 2000.0, 58000.0
    hits = [
        (f + 500e-6, -0.12, t + 0.5, "A", ("A", 1)),  # in window, drift fits
        (f - 1500e-6, 0.30, t + 2.0, "B", ("B", 2)),  # in window, other drift
        (f + 3000e-6, -0.12, t, "C", ("C", 3)),  # outside 2 kHz
        (f, -0.12, t + 9.0, "D", ("D", 4)),  # outside 7 days
    ]
    near, scans, same = probe.count_near(hits, f, -0.12, t, 7.0)
    assert scans == {("A", 1), ("B", 2)} and same == {("A", 1)}


def test_probe_perm_ranks_a_line_that_persists_and_not_a_crowded_band():
    from coldcase import probe_perm

    rng = np.random.default_rng(3)
    fo, nf, k_e = 2.8, 40000, 20000.0

    def scans(n, line, crowd):
        out = []
        for _ in range(n):
            best = rng.normal(3.0, 1.0, nf)
            if crowd:  # strong lines everywhere, one window in ten
                for k in rng.choice(nf, nf // 360, replace=False):
                    best[k] = rng.uniform(50, 500)
            if line:
                best[int(k_e) + rng.integers(-5, 6)] = 40.0
            out.append((best, k_e, fo))
        return out

    clean = probe_perm.one_width(scans(12, True, False), 50.0)
    assert clean["P"] <= 1 / clean["windows"] * 1.01  # nothing else does as well
    assert clean["scans_standing_out"] == 12
    empty = probe_perm.one_width(scans(12, False, False), 50.0)
    assert empty["P"] > 0.02
    crowded = probe_perm.one_width(scans(12, False, True), 50.0)
    assert crowded["P"] > 0.02


def _session(margins):
    return {
        "scans": [
            {
                "source": f"S{i}",
                "minutes_from_event": -6.0 * (i + 1),
                "offset_hz": 10.0,
                "separation_deg": 5.0,
                "margin_over_control_max": m,
                "controls": {"n": 46, "max_snr": 5.0, "second_snr": 4.8},
            }
            for i, m in enumerate(margins)
        ]
    }


def test_d7_needs_two_matching_scans():
    one, st = triage.session_rule(_bundle(), _session([9.0, -1.0, -0.5, -0.8]))
    assert one is None and st["scans_matched"] == 1  # one scan is only a flag
    assert st["chance_of_this_many"] < triage.CHANCE_MAX
    two, _ = triage.session_rule(_bundle(), _session([9.0, 4.0, -0.5, -0.8]))
    assert two.startswith("D7")


def _burst_bundle(without):
    row = {
        "pos": 0,
        "role": "ON",
        "track": {"snr": 60.0},
        "control_windows": {"max_snr": 5.0},
        "track_time_profile": {"largest_single_sample_share": 0.55},
        "track_without_strongest_sample": without,
    }
    return {"seed": {"recovered": True, "seed_pos": 0, "freq_mhz": 1600.0, "drift": 0.1}, "scans": [row]}


def test_d4_share_alone_does_not_close_a_line_that_survives():
    survives = {"snr": 40.0, "control_max_snr": 5.0, "full_snr": 60.0,
                "full_control_max_snr": 5.0, "share_of_full_snr": 0.67}
    dec = triage.decide(_burst_bundle(survives))
    assert dec["status"] == "open" and any("still stands" in f for f in dec["flags"])
    gone = {"snr": 4.0, "control_max_snr": 5.0, "full_snr": 60.0,
            "full_control_max_snr": 5.0, "share_of_full_snr": 0.07}
    dec = triage.decide(_burst_bundle(gone))
    assert dec["status"] == "closed" and dec["verdict"] == "not_a_tone"
