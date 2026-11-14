import os
import sys

import astropy.units as u
import numpy as np
from astroplan import Observer
from astropy.coordinates import EarthLocation
from astropy.time import Time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.astro_utils import AstroCalculator
from backend.models import Location


def test_sunset_boundary_never_produces_two_day_session():
    """
    Regression test for Lund (55.7058N, 13.1932E) right before sunset (14:53:45-14:55:30 UTC),
    where astroplan previously skipped today's sunset and returned a 40.87h 2-night span.
    """
    loc = Location(latitude=55.7058, longitude=13.1932)
    obs = Observer(
        location=EarthLocation(lat=loc.latitude * u.deg, lon=loc.longitude * u.deg)
    )
    calc = AstroCalculator()

    t0 = Time("2026-11-14T14:53:00")
    starts = []
    ends = []
    for sec in range(0, 240, 15):
        t = t0 + sec * u.second
        s, e = calc.get_observing_session(obs, t)
        dur_h = (e - s).to(u.hour).value
        assert 15.0 < dur_h < 19.0, f"Unexpected duration {dur_h:.2f}h at {t.isot}"
        starts.append(s.unix)
        ends.append(e.unix)

        mid = s + 0.5 * (e - s)
        tw = calc.get_twilight_periods(loc, mid)
        assert "night" in tw
        nt_start = Time(tw["night"][0])
        nt_end = Time(tw["night"][1])
        assert s <= nt_start < nt_end <= e

        # M 45 (Pleiades) visible hours during single night must be ~11h, never ~22.2h
        ephem = calc.prepare_session_ephemeris(
            location=loc,
            start_time=s,
            end_time=e,
            num_points=60,
            twilight_periods=tw,
        )
        graphs, hours = calc.batch_compute_altitude_graphs(
            ra_array=np.array([56.87]),
            dec_array=np.array([24.11]),
            ephemeris=ephem,
            min_altitude=20.0,
        )
        assert len(graphs[0]) == 60
        assert 8.0 <= hours[0] <= 13.0, f"Expected single-night hours, got {hours[0]}"

    # Session start/end must be continuous across the exact moment of sunset (< 5s jitter)
    assert max(starts) - min(starts) < 5.0
    assert max(ends) - min(ends) < 5.0


def test_sunrise_boundary_and_early_morning_twilight():
    """
    Regression test around sunrise (06:47-06:50 UTC) and early morning (03:00-05:30 UTC),
    ensuring sunrise does not fall back to 24h and early-morning twilight stays on the active night.
    """
    loc = Location(latitude=55.7058, longitude=13.1932)
    obs = Observer(
        location=EarthLocation(lat=loc.latitude * u.deg, lon=loc.longitude * u.deg)
    )
    calc = AstroCalculator()

    # Sweep across sunrise transition
    t_sr = Time("2026-11-14T06:46:30")
    for sec in range(0, 240, 15):
        t = t_sr + sec * u.second
        s, e = calc.get_observing_session(obs, t)
        dur_h = (e - s).to(u.hour).value
        assert 15.0 < dur_h < 19.0, f"Sunrise fallback triggered ({dur_h:.2f}h) at {t.isot}"

    # Early morning (03:00Z and 05:00Z on Nov 14 -> active night is Nov 13 evening to Nov 14 morning)
    for iso in ("2026-11-14T03:00:00", "2026-11-14T05:00:00"):
        t = Time(iso)
        s, e = calc.get_observing_session(obs, t)
        tw_direct = calc.get_twilight_periods(loc, t)
        assert "night" in tw_direct
        nt_start = Time(tw_direct["night"][0])
        nt_end = Time(tw_direct["night"][1])
        assert s <= nt_start < nt_end <= e
        assert nt_start.isot.startswith("2026-11-13T")
        assert nt_end.isot.startswith("2026-11-14T")


def test_polar_day_and_polar_night_fallbacks():
    """Verify continuous polar summer day and polar winter night fall back cleanly to 24h."""
    loc = Location(latitude=69.6492, longitude=18.9553)  # Tromso
    obs = Observer(
        location=EarthLocation(lat=loc.latitude * u.deg, lon=loc.longitude * u.deg)
    )
    calc = AstroCalculator()

    # Midnight sun (June)
    t_summer = Time("2026-06-21T12:00:00")
    s_sum, e_sum = calc.get_observing_session(obs, t_summer)
    assert abs((e_sum - s_sum).to(u.hour).value - 24.0) < 0.01
    tw_sum = calc.get_twilight_periods(loc, t_summer)
    assert "day" in tw_sum

    # Polar night (December)
    t_winter = Time("2026-12-21T12:00:00")
    s_win, e_win = calc.get_observing_session(obs, t_winter)
    assert abs((e_win - s_win).to(u.hour).value - 24.0) < 0.01
    tw_win = calc.get_twilight_periods(loc, t_winter)
    assert any(k in tw_win for k in ("civil", "nautical", "astronomical", "night"))
