"""Space weather, from the two sources that mean different things.

Both logan/Contest_Plan and RBN shipped a file called `solar.py`, and they
were not the same module — one reads GFZ Potsdam's definitive *daily* record
going back to 1932, the other polls NOAA SWPC for what the sun is doing
*right now*. Same name, different data, different cadence, not
interchangeable. Here they get names that say which is which:

    from hamcore.solar import gfz    # the definitive daily record (past)
    from hamcore.solar import swpc   # live NOAA products (present)

When the two disagree about today's SFI that is expected: they are different
measurements, not a bug.
"""
# ---------------------------------------------------------------------------
# VENDORED from hamcore 1.0.0 -- do not edit here.
# Edit AI/hamcore/hamcore/solar/__init__.py and re-run:
#     python3 -m hamcore.vendor sync
# ---------------------------------------------------------------------------

from . import gfz, swpc  # noqa: F401

__all__ = ["gfz", "swpc"]
