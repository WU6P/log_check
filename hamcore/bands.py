"""The amateur band plan, in one table.

Six projects each grew their own version of this, in four different units and
three different naming conventions:

    RBN/rbn.py           ("160m", 1790, 2010)      name + padded edges, kHz
    RBN/propagation.py   {"160m": 1.825}           CW-end representative, MHz
    rig/knob.py          ("160", 1840000)          band-change target, Hz
    flexradio/…/enums.py {"160": 1.9}              band-button target, MHz
    sonify/spots.py      ["160m", "80m", …]        just the order
    cw_band              its own edges again

They agree on the physics and disagree on everything else, which is exactly
the kind of thing that is fine until you need to change one. This module
carries all of it once: legal edges, the deliberately-padded edges a skimmer
needs, the CW-end frequency a propagation model wants, and the dial frequency
a radio should land on.

Frequencies are kHz throughout — the unit the RBN, ADIF and every log use.
`_hz` and `_mhz` helpers exist for the two projects that need otherwise.
"""
# ---------------------------------------------------------------------------
# VENDORED from hamcore 1.0.0 -- do not edit here.
# Edit AI/hamcore/hamcore/bands.py and re-run:
#     python3 -m hamcore.vendor sync
# ---------------------------------------------------------------------------

# name, lo, hi, cw_khz, dial_khz
#
# lo/hi are IARU Region 2 (US) edges. cw_khz is the CW end, used as the
# representative frequency for propagation work. dial_khz is where a radio
# should actually land on a band change — the popular activity centre, which
# is *not* the band edge and *not* the CW end (it is usually the FT8 watering
# hole, because that is where a band change is most often headed).
_PLAN = [
    ("2200m",   135.7,    137.8,    136.0,    136.0),
    ("630m",    472.0,    479.0,    474.2,    474.2),
    ("160m",   1800.0,   2000.0,   1825.0,   1840.0),
    ("80m",    3500.0,   4000.0,   3525.0,   3573.0),
    # 60 m is the ragged one: five discrete channels in the US, a contiguous
    # 5351.5-5366.5 globally, and something wider again in the UK and much of
    # Europe. This is a *classification* window covering the lot — not a
    # statement about what you are licensed to transmit on.
    ("60m",    5250.0,   5450.0,   5357.0,   5357.0),
    ("40m",    7000.0,   7300.0,   7025.0,   7074.0),
    ("30m",   10100.0,  10150.0,  10115.0,  10136.0),
    ("20m",   14000.0,  14350.0,  14025.0,  14074.0),
    ("17m",   18068.0,  18168.0,  18075.0,  18100.0),
    ("15m",   21000.0,  21450.0,  21025.0,  21074.0),
    ("12m",   24890.0,  24990.0,  24900.0,  24915.0),
    ("10m",   28000.0,  29700.0,  28025.0,  28074.0),
    ("6m",    50000.0,  54000.0,  50090.0,  50313.0),
    ("4m",    70000.0,  70500.0,  70100.0,  70154.0),
    ("2m",   144000.0, 148000.0, 144050.0, 144174.0),
    ("1.25m",222000.0, 225000.0, 222050.0, 222100.0),
    ("70cm", 420000.0, 450000.0, 432050.0, 432174.0),
    ("33cm", 902000.0, 928000.0, 902100.0, 903100.0),
    ("23cm",1240000.0,1300000.0,1296050.0,1296174.0),
]

BANDS = [b[0] for b in _PLAN]
EDGES = {b[0]: (b[1], b[2]) for b in _PLAN}
CW_KHZ = {b[0]: b[3] for b in _PLAN}
DIAL_KHZ = {b[0]: b[4] for b in _PLAN}
INDEX = {name: i for i, name in enumerate(BANDS)}

# HF only, the set most of these projects actually work with.
HF = [b for b in BANDS if EDGES[b][1] <= 30000.0 and EDGES[b][0] >= 1800.0]
# The bands that behave as night bands — used for absorption and storm effects.
LOW = ["160m", "80m", "60m", "40m"]

# Skimmers and busted log entries land a little outside the legal edges, so
# classification uses a padded window. Without it a spot at 14000.0 - 0.1 kHz
# is silently dropped, and those are real spots.
PAD_KHZ = 10.0


def _pad(lo, hi):
    """Padding for one band. 10 kHz suits HF, but 2200 m is only 2.1 kHz wide
    and a flat 10 kHz would swallow four times the band — so it is capped at a
    quarter of the width, which leaves every HF band at the full 10 kHz."""
    return min(PAD_KHZ, max(0.5, (hi - lo) / 4.0))


def band_for(freq_khz, padded=True):
    """'20m' for 14040.1, or None if it is on no band.

    `padded` widens each band by PAD_KHZ, which is what you want for spots
    and logs; pass False when you need to know whether a frequency is
    actually in-band.
    """
    if freq_khz is None:
        return None
    try:
        f = float(freq_khz)
    except (TypeError, ValueError):
        return None
    for name, lo, hi, _cw, _dial in _PLAN:
        pad = _pad(lo, hi) if padded else 0.0
        if lo - pad <= f <= hi + pad:
            return name
    return None


def band_for_hz(freq_hz, padded=True):
    return band_for(None if freq_hz is None else float(freq_hz) / 1000.0, padded)


def band_for_mhz(freq_mhz, padded=True):
    return band_for(None if freq_mhz is None else float(freq_mhz) * 1000.0, padded)


def normalize(name):
    """'20', '20M', '20m', ' 20 m ' -> '20m'; unknown -> None.

    Logs, radios and skimmers each write the band differently; this is the
    one place that has to know.
    """
    s = str(name or "").strip().lower().replace(" ", "")
    if not s:
        return None
    if s in INDEX:
        return s
    if not s.endswith("m") and (s + "m") in INDEX:
        return s + "m"
    if s.endswith("cm") or s.endswith("mm"):
        return s if s in INDEX else None
    return None


def in_band(freq_khz):
    """True if the frequency is inside a real band edge (no padding)."""
    return band_for(freq_khz, padded=False) is not None


def is_low(band):
    """The night bands — the ones a flare or a storm takes away first."""
    return normalize(band) in LOW


def sort_key(band):
    """Sort bands by frequency rather than alphabetically, so 10m does not
    come before 160m."""
    return INDEX.get(normalize(band), len(BANDS))


def sorted_bands(names):
    return sorted((n for n in names if normalize(n)), key=sort_key)
