# ---------------------------------------------------------------------------
# VENDORED from hamcore 1.0.0 -- do not edit here.
# Edit AI/hamcore/hamcore/solar/swpc.py and re-run:
#     python3 -m hamcore.vendor sync
# ---------------------------------------------------------------------------
#!/usr/bin/env python3
"""Live space-weather indices from NOAA SWPC, cached and self-refreshing.

Feeds propagation models and dashboards with the
numbers an HF operator actually reads: 10.7 cm solar flux, sunspot number,
planetary A / Kp, GOES X-ray flux (flare class), solar wind speed and Bz, the
NOAA R/S/G scales, and the 27-day outlook.

Every product is fetched independently with its own TTL, so one dead endpoint
never blanks the rest of the board — each keeps its last good value and reports
its age. With no network at all the snapshot falls back to quiet-sun defaults
and flags itself `live: false`, so the dashboard and the model still run.

Sources (all public NOAA SWPC, no key needed):
  /products/summary/10cm-flux.json        current F10.7
  /text/daily-solar-indices.txt           daily SSN, spot area, flare counts
  /json/planetary_k_index_1m.json         estimated Kp, 1-minute, 24 h
  /text/daily-geomagnetic-indices.txt     planetary A index
  /json/goes/primary/xrays-1-day.json     GOES 0.1-0.8 nm X-ray flux, 24 h
  /products/summary/solar-wind-speed.json     ACE/DSCOVR wind speed
  /products/summary/solar-wind-mag-field.json Bt / Bz (GSM)
  /products/noaa-scales.json              R / S / G scales now
  /text/27-day-outlook.txt                27-day F10.7 / A / Kp forecast

Pure standard library.
"""

import json
import math
import re
import threading
import time
import urllib.request
from datetime import datetime, timezone

BASE = "https://services.swpc.noaa.gov"
UA = "hamcore/1.0 (+amateur radio, stdlib urllib)"

# Quiet-sun stand-ins used when a product has never been fetched successfully.
# Deliberately unremarkable: a dashboard that cannot reach NOAA should look
# boring, not alarming, and the model still produces a usable prediction.
FALLBACK = {
    "sfi": 90.0, "ssn": 30.0, "a_index": 8.0, "kp": 2.0,
    "xray_flux": 1e-7, "wind_speed": 400.0, "bt": 5.0, "bz": 0.0,
}


# --------------------------------------------------------------------------
# Solar-index conversions
# --------------------------------------------------------------------------

def ssn_from_sfi(sfi):
    """Sunspot number implied by a 10.7 cm flux, inverting the Sakurai relation
    SFI = 63.75 + 0.728*R + 0.00089*R^2 (the usual ham-radio conversion)."""
    if sfi is None:
        return None
    a, b, c = 0.00089, 0.728, 63.75 - float(sfi)
    disc = b * b - 4 * a * c
    if disc <= 0:
        return 0.0
    return max(0.0, (-b + math.sqrt(disc)) / (2 * a))


def xray_class(flux):
    """GOES long-channel flux (W/m^2) -> flare class string, e.g. 1.5e-6 -> 'C1.5'."""
    if flux is None or flux <= 0:
        return None
    for letter, base in (("X", 1e-4), ("M", 1e-5), ("C", 1e-6), ("B", 1e-7)):
        if flux >= base:
            return f"{letter}{flux / base:.1f}"
    return f"A{flux / 1e-8:.1f}"


def _iso(s):
    """SWPC timestamps: '2026-07-26T05:09:00Z' or without the Z -> epoch."""
    s = (s or "").strip().replace("Z", "")
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Per-product parsers: raw body -> partial snapshot dict
# --------------------------------------------------------------------------

def parse_flux(body):
    rows = json.loads(body)
    return {"sfi": float(rows[0]["flux"]), "sfi_t": _iso(rows[0]["time_tag"])}


def parse_dsd(body):
    """Daily Solar Data (DSD.txt). 16 columns: y m d, F10.7, SESC sunspot
    number, spot area, new regions, Stanford mean field, GOES X-ray background,
    X-ray flare counts C M X S, optical flare counts 1 2 3. Uses the last row
    that parses (today's row appears as soon as the day opens)."""
    for line in reversed(body.splitlines()):
        line = line.strip()
        if not line or line[0] in ":#":
            continue
        f = line.split()
        if len(f) < 16 or not f[0].isdigit():
            continue
        return {"ssn": float(f[4]), "sfi_daily": float(f[3]),
                "spot_area": float(f[5]), "xray_bkgd": f[8],
                "flares": {"C": int(f[9]), "M": int(f[10]), "X": int(f[11])},
                "dsd_date": f"{f[0]}-{f[1]}-{f[2]}"}
    return {}


def parse_kp(body):
    """1-minute estimated Kp -> current value plus a 24 h series for the
    sparkline (thinned to 15-minute points; 1440 raw points is noise)."""
    rows = json.loads(body)
    if not rows:
        return {}
    series = []
    for r in rows[::15]:
        t = _iso(r["time_tag"])
        if t is not None:
            series.append([round(t), round(float(r["estimated_kp"]), 2)])
    return {"kp": round(float(rows[-1]["estimated_kp"]), 2),
            "kp_t": _iso(rows[-1]["time_tag"]), "kp_series": series}


def parse_dgd(body):
    """Daily Geomagnetic Data (DGD.txt): 30 columns — y m d, Fredericksburg A
    and 8 K, College A and 8 K, then planetary A and 8 estimated Kp. Today's
    planetary A reads -1 until the day closes, so walk back to the last real
    one rather than reporting a negative index."""
    for line in reversed(body.splitlines()):
        line = line.strip()
        if not line or line[0] in ":#":
            continue
        f = line.split()
        if len(f) < 22 or not f[0].isdigit():
            continue
        a_plan = int(f[21])
        if a_plan >= 0:
            return {"a_index": float(a_plan), "a_date": f"{f[0]}-{f[1]}-{f[2]}"}
    return {}


def parse_xray(body):
    """GOES X-rays, 1-day. Keeps only the 0.1-0.8 nm (long) channel: that is
    the channel flare classes are defined on."""
    rows = [r for r in json.loads(body) if r.get("energy") == "0.1-0.8nm"]
    if not rows:
        return {}
    series = []
    for r in rows[::10]:                       # ~1 min raw -> 10 min points
        t = _iso(r["time_tag"])
        if t is not None and r.get("flux"):
            series.append([round(t), float(r["flux"])])
    peak = max(rows, key=lambda r: r.get("flux") or 0)
    return {"xray_flux": float(rows[-1]["flux"]),
            "xray_t": _iso(rows[-1]["time_tag"]),
            "xray_peak": float(peak["flux"]),
            "xray_peak_t": _iso(peak["time_tag"]),
            "xray_series": series}


def parse_wind(body):
    rows = json.loads(body)
    return {"wind_speed": float(rows[0]["proton_speed"])}


def parse_mag(body):
    rows = json.loads(body)
    return {"bt": float(rows[0]["bt"]), "bz": float(rows[0]["bz_gsm"])}


def parse_scales(body):
    """noaa-scales.json key '0' is the current observed R/S/G."""
    d = json.loads(body).get("0", {})
    out = {}
    for k in ("R", "S", "G"):
        v = d.get(k) or {}
        out[k] = {"scale": int(v.get("Scale") or 0),
                  "text": (v.get("Text") or "none").strip()}
    return {"scales": out}


def parse_outlook(body):
    """27-day outlook table: date, F10.7, A index, largest Kp."""
    out = []
    for line in body.splitlines():
        f = line.split()
        if len(f) == 6 and f[0].isdigit() and len(f[0]) == 4:
            try:
                out.append({"date": f"{f[0]} {f[1]} {f[2]}", "sfi": int(f[3]),
                            "a": int(f[4]), "kp": int(f[5])})
            except ValueError:
                continue
    return {"outlook": out} if out else {}


PRODUCTS = [
    # name,            path,                                        ttl_s, parser
    ("flux",    "/products/summary/10cm-flux.json",                  1800, parse_flux),
    ("dsd",     "/text/daily-solar-indices.txt",                     3600, parse_dsd),
    ("kp",      "/json/planetary_k_index_1m.json",                    300, parse_kp),
    ("dgd",     "/text/daily-geomagnetic-indices.txt",               1800, parse_dgd),
    ("xray",    "/json/goes/primary/xrays-1-day.json",                300, parse_xray),
    ("wind",    "/products/summary/solar-wind-speed.json",            300, parse_wind),
    ("mag",     "/products/summary/solar-wind-mag-field.json",        300, parse_mag),
    ("scales",  "/products/noaa-scales.json",                         900, parse_scales),
    ("outlook", "/text/27-day-outlook.txt",                          7200, parse_outlook),
]


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


class SolarData:
    """Self-refreshing cache of the NOAA products above.

    `snapshot()` is cheap and thread-safe — the dashboard hits it every few
    seconds while a single daemon thread does the fetching."""

    def __init__(self, offline=False, fetch=http_get, base=BASE):
        self.offline = offline
        self._fetch = fetch
        self._base = base
        self._lock = threading.Lock()
        self._values = {}           # merged partial dicts, last good wins
        self._meta = {}             # per product: ok / t / err
        self._stop = threading.Event()
        self._thread = None

    # -- fetching -----------------------------------------------------------

    def refresh_one(self, name, path, parser):
        """Fetch and merge one product. Returns True on success."""
        try:
            data = parser(self._fetch(self._base + path))
        except Exception as e:                      # noqa: BLE001 — any failure
            with self._lock:                        # keeps the last good value
                m = self._meta.setdefault(name, {})
                m["err"] = f"{type(e).__name__}: {e}"
                m["tried"] = time.time()
            return False
        with self._lock:
            self._values.update(data)
            self._meta[name] = {"t": time.time(), "err": "",
                                "tried": time.time()}
        return True

    def refresh(self, force=False):
        """Refresh every product whose TTL has expired. Returns how many were
        fetched successfully."""
        if self.offline:
            return 0
        now, ok = time.time(), 0
        for name, path, ttl, parser in PRODUCTS:
            m = self._meta.get(name, {})
            due = force or now - m.get("t", 0) >= ttl
            # A failing product retries on its own short cycle, not every loop.
            if not due or (not force and m.get("err")
                           and now - m.get("tried", 0) < 120):
                continue
            ok += self.refresh_one(name, path, parser)
        return ok

    def start(self, interval=60):
        if self.offline or self._thread:
            return
        def loop():
            while not self._stop.is_set():
                self.refresh()
                self._stop.wait(interval)
        self._thread = threading.Thread(target=loop, daemon=True, name="solar")
        self._thread.start()

    def stop(self):
        self._stop.set()

    # -- reading ------------------------------------------------------------

    def snapshot(self):
        """Everything the dashboard and the model need, with provenance.

        Values missing from every successful fetch fall back to FALLBACK and
        the snapshot reports `live: false` so the page can say so."""
        with self._lock:
            v = dict(self._values)
            meta = {k: dict(m) for k, m in self._meta.items()}
        now = time.time()
        live = any(m.get("t") for m in meta.values())
        out = {k: v.get(k, FALLBACK[k]) for k in FALLBACK}
        for k in ("sfi_daily", "spot_area", "flares", "dsd_date", "a_date",
                  "kp_series", "xray_series", "xray_peak", "xray_peak_t",
                  "outlook", "scales", "sfi_t", "kp_t", "xray_t"):
            if k in v:
                out[k] = v[k]
        # SSN is a daily product: if it has not landed yet, imply it from flux
        # so the model always has a solar-activity driver.
        if "ssn" in v:
            out["ssn"], out["ssn_src"] = v["ssn"], "observed"
        else:
            out["ssn"], out["ssn_src"] = round(ssn_from_sfi(out["sfi"]), 1), "from SFI"
        out["xray_class"] = xray_class(out.get("xray_flux"))
        out["xray_peak_class"] = xray_class(out.get("xray_peak"))
        out["live"] = live
        out["offline"] = self.offline
        out["now"] = now
        out["sources"] = {
            name: {"ok": bool(meta.get(name, {}).get("t")),
                   "age": (round(now - meta[name]["t"])
                           if meta.get(name, {}).get("t") else None),
                   "err": meta.get(name, {}).get("err", "")}
            for name, _, _, _ in PRODUCTS}
        return out


if __name__ == "__main__":  # pragma: no cover
    sd = SolarData()
    sd.refresh(force=True)
    s = sd.snapshot()
    print(f"SFI {s['sfi']:.0f}  SSN {s['ssn']:.0f} ({s['ssn_src']})  "
          f"A {s['a_index']:.0f}  Kp {s['kp']:.2f}  X-ray {s['xray_class']}  "
          f"wind {s['wind_speed']:.0f} km/s  Bz {s['bz']:+.1f} nT")
    print("scales:", s.get("scales"))
    for name, m in s["sources"].items():
        print(f"  {name:8s} {'ok' if m['ok'] else 'FAIL':4s} "
              f"age={m['age']} {m['err']}")
