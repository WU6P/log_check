# ---------------------------------------------------------------------------
# VENDORED from hamcore 1.0.0 -- do not edit here.
# Edit AI/hamcore/hamcore/sun.py and re-run:
#     python3 -m hamcore.vendor sync
# ---------------------------------------------------------------------------
#!/usr/bin/env python3
"""Sunrise / sunset and a sunrise-anchored "solar-aligned hour" for a QTH.

Contest_Plan aligns historic QSOs to the *solar* day, not the wall clock:
band openings track the sun, so a contact made 30 minutes after sunrise should
inform the same recommendation regardless of the season or year the sunrise
happened to fall on. This module gives, for any UTC date and station location:

  * `sun_times(d, lat, lon)`  -> (sunrise_utc_h, sunset_utc_h) as float hours,
                                 or (None, None) at polar day / night.
  * `solar_aligned_hour(...)` -> a 0..24 scalar where sunrise maps to 6, solar
                                 noon to 12, sunset to 18 and solar midnight to
                                 0/24 — so two instants with the same value sit
                                 at the same point of their respective solar
                                 days even if the clock times differ.
  * `grayline_offset(...)`    -> hours to the nearer of sunrise / sunset (so the
                                 propagation-rich ±1 h grayline can be weighted).

Pure standard library. Algorithm: the NOAA solar-position equations (same ones
as the NOAA sunrise/sunset spreadsheet); accurate to well under a minute, which
is far finer than our hourly buckets need.
"""

import math
from datetime import date


def _julian_day(y, m, d):
    """Julian Day number at 0h UT of the given calendar date."""
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + d + b - 1524.5


def _solar_geom(jd):
    """Equation of time (minutes) and solar declination (deg) for a Julian Day."""
    jc = (jd - 2451545.0) / 36525.0
    L0 = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360.0
    M = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    e = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    Mr = math.radians(M)
    C = (math.sin(Mr) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
         + math.sin(2 * Mr) * (0.019993 - 0.000101 * jc)
         + math.sin(3 * Mr) * 0.000289)
    true_long = L0 + C
    omega = 125.04 - 1934.136 * jc
    app_long = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    obliq = 23.0 + (26.0 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60.0) / 60.0
    obliq_corr = obliq + 0.00256 * math.cos(math.radians(omega))
    declin = math.degrees(math.asin(math.sin(math.radians(obliq_corr))
                                    * math.sin(math.radians(app_long))))
    y = math.tan(math.radians(obliq_corr / 2.0)) ** 2
    L0r = math.radians(L0)
    eq_time = 4.0 * math.degrees(
        y * math.sin(2 * L0r) - 2 * e * math.sin(Mr)
        + 4 * e * y * math.sin(Mr) * math.cos(2 * L0r)
        - 0.5 * y * y * math.sin(4 * L0r) - 1.25 * e * e * math.sin(2 * Mr))
    return eq_time, declin


# Standard sunrise/sunset uses the sun's centre at 90.833° zenith (refraction +
# semidiameter). lon is east-positive.
_ZENITH = 90.833


def sun_times(d, lat, lon):
    """(sunrise, sunset) in UTC float hours for date `d` at (lat, lon).

    Returns (None, None) when the sun stays up or down all day (|lat| polar)."""
    jd = _julian_day(d.year, d.month, d.day)
    eq_time, declin = _solar_geom(jd)
    latr, decr = math.radians(lat), math.radians(declin)
    cos_ha = (math.cos(math.radians(_ZENITH)) / (math.cos(latr) * math.cos(decr))
              - math.tan(latr) * math.tan(decr))
    if cos_ha > 1.0 or cos_ha < -1.0:
        return None, None
    ha = math.degrees(math.acos(cos_ha))           # half-day arc, degrees
    solar_noon = (720.0 - 4.0 * lon - eq_time) / 60.0   # UTC hours
    return solar_noon - ha / 15.0, solar_noon + ha / 15.0


def _wrap24(x):
    return x % 24.0


def solar_aligned_hour(t_hours, sunrise, sunset):
    """Map UTC hour-of-day `t_hours` to a solar-aligned hour in [0, 24).

    sunrise->6, solar noon->12, sunset->18, solar midnight->0/24, with daytime
    and nighttime each stretched/squeezed to span 12 aligned-hours. If sunrise/
    sunset are None (polar), the wall-clock hour is returned unchanged so the
    pipeline still works (it just won't be solar-aligned that day)."""
    if sunrise is None or sunset is None:
        return _wrap24(t_hours)
    day_len = _wrap24(sunset - sunrise)            # daylight length, hours
    night_len = 24.0 - day_len
    since_rise = _wrap24(t_hours - sunrise)
    if since_rise <= day_len and day_len > 0:
        return 6.0 + 12.0 * (since_rise / day_len)
    since_set = _wrap24(t_hours - sunset)
    if night_len <= 0:
        return _wrap24(t_hours)
    return _wrap24(18.0 + 12.0 * (since_set / night_len))


def circular_dist(a, b, period=24.0):
    """Shortest distance between two points on a `period`-length circle."""
    d = abs(a - b) % period
    return min(d, period - d)


def grayline_offset(t_hours, sunrise, sunset):
    """Hours from `t_hours` to the nearer of sunrise / sunset (0 = right on it).

    Used to weight the propagation-rich grayline (±1 h). None at polar day/night."""
    if sunrise is None or sunset is None:
        return None
    return min(circular_dist(t_hours, sunrise), circular_dist(t_hours, sunset))


def grayline_hour(t_hours, sunrise, sunset):
    """True iff the clock-hour bin containing `t_hours` is the one in which
    sunrise or sunset actually happens (the single UTC hour the grayline
    crosses — replaces the old +/-1 h band). False at polar day/night."""
    if sunrise is None or sunset is None:
        return False
    # Wrap BEFORE truncating: sun_times returns un-wrapped hours that go
    # negative for eastern longitudes, and int() truncates toward zero, so
    # int(-4.57) % 24 = 20 while sunrise really falls in hour 19.
    hb = int(t_hours % 24)
    return hb == int(sunrise % 24) or hb == int(sunset % 24)


if __name__ == "__main__":
    # Quick sanity check: sunrise/sunset for a couple of known places/dates.
    for name, lat, lon, d in [
        ("San Francisco 2026-06-21", 37.77, -122.42, date(2026, 6, 21)),
        ("London 2026-12-21", 51.5, -0.13, date(2026, 12, 21)),
    ]:
        sr, ss = sun_times(d, lat, lon)
        print(f"{name}: sunrise {sr:.2f}Z sunset {ss:.2f}Z  daylen {(ss-sr):.2f}h")


# --------------------------------------------------------------------------
# RBN additions: subsolar point + day/night terminator for the world map.
# Reuses the NOAA solar geometry above; accuracy far exceeds map-pixel needs.
# --------------------------------------------------------------------------

def subsolar_point(dt):
    """(lat, lon) directly under the sun at UTC datetime `dt`."""
    jd = _julian_day(dt.year, dt.month, dt.day)
    eq_time, declin = _solar_geom(jd)
    utc_h = dt.hour + dt.minute / 60.0 + dt.second / 3600.0
    lon = 15.0 * (12.0 - utc_h - eq_time / 60.0)
    lon = ((lon + 180.0) % 360.0) - 180.0
    return declin, lon


def terminator(dt, step_deg=2):
    """Day/night terminator as [(lat, lon)] for lon -180..180 at `step_deg`.

    For each longitude the latitude where the sun's (geometric) elevation is
    zero: tan(lat) = -cos(H) / tan(declination), H = local hour angle. The
    night side is the hemisphere away from subsolar_point()'s latitude."""
    jd = _julian_day(dt.year, dt.month, dt.day)
    eq_time, declin = _solar_geom(jd)
    utc_h = dt.hour + dt.minute / 60.0 + dt.second / 3600.0
    decr = math.radians(declin if abs(declin) > 0.01 else 0.01)
    pts = []
    lon = -180
    while lon <= 180:
        ha = math.radians((utc_h + eq_time / 60.0 - 12.0) * 15.0 + lon)
        lat = math.degrees(math.atan(-math.cos(ha) / math.tan(decr)))
        pts.append((round(lat, 2), lon))
        lon += step_deg
    return pts
