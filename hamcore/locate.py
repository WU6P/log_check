# ---------------------------------------------------------------------------
# VENDORED from hamcore 1.0.0 -- do not edit here.
# Edit AI/hamcore/hamcore/locate.py and re-run:
#     python3 -m hamcore.vendor sync
# ---------------------------------------------------------------------------
#!/usr/bin/env python3
"""Callsign -> location for RBN spotters and the monitored station.

Ported from Contest_Plan/core.py (itself built on logan): the ARRL-DXCC ->
ITU call-sign-series resolution chain, the curated call-area-split prefix
overrides (Hawaii/Alaska/Asiatic Russia/Canaries/...), and the US/Canada
call-area centroid refinement, so a bare spotter call like "W3LPL-#"
resolves to a plottable lat/lon. Also maidenhead grid decoding plus
great-circle bearing/distance for the map's home->spotter arcs.

Pure standard library. dxcc.json / itu.json ship with hamcore, so logan,
log_check, Contest_Plan, RBN and sonify all resolve callsigns from one copy.
"""

import json
import math
import re
from . import data_path


def _load_dxcc():
    """prefix -> entity record, from the shared ARRL-PDF-derived dxcc.json."""
    path = data_path("dxcc.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for rec in data.get("entities", []):
        for pf in rec.get("prefixes", []):
            out.setdefault(pf.upper(), rec)
    for k, v in data.get("lookup", {}).items():
        out.setdefault(k.upper(), v)
    return out


DXCC = _load_dxcc()


def _load_itu():
    path = data_path("itu.json")
    return json.loads(path.read_text(encoding="utf-8")).get("lookup", {})


ITU = _load_itu()


def _load_entity_recs():
    """entity name -> full record, so a prefix override resolves to a
    *located* entity rather than just a name."""
    path = data_path("dxcc.json")
    ents = json.loads(path.read_text(encoding="utf-8")).get("entities", [])
    return {e["entity"]: e for e in ents}


ENTITY_REC = _load_entity_recs()

_SUFFIXES = {"P", "M", "MM", "AM", "QRP", "A", "B"}


def _call_cores(call):
    call = (call or "").upper().strip()
    if "/" not in call:
        return [call] if call else []
    parts = [p for p in call.split("/")
             if p and p not in _SUFFIXES and any(c.isalpha() for c in p)]
    return sorted(parts, key=len) or [call.replace("/", "")]


def _leading_alpha(head):
    """How many leading letters a prefix head has ('TM2' -> 2)."""
    i = 0
    while i < len(head) and head[i].isalpha():
        i += 1
    return i


def _lookup_head(core):
    """Longest-prefix DXCC match for a call core: (rec, key, head) or Nones.

    Full-call exception keys (e.g. 'KH7K' = Kure I.) are honoured before the
    head-prefix search. `key` lets callers judge confidence (a match that
    dropped leading letters, like France 'TM' collapsing to the 'T' catch-all
    = Kiribati, is unreliable)."""
    if core in DXCC:
        return DXCC[core], core, core
    m = re.match(r"([A-Z0-9]+?\d)", core)
    head = m.group(1) if m else core
    for n in range(len(head), 0, -1):
        key = head[:n]
        if key in DXCC:
            return DXCC[key], key, head
    return None, None, None


# The shared dxcc.json keeps only coarse prefix keys, so call-area-split
# families resolve to the wrong entity -> spot plotted on the wrong
# continent. Mirror of log_check's _PREFIX_OVERRIDES.
_PREFIX_OVERRIDES = [
    (re.compile(r"^KP[34]"), "Puerto Rico"),
    (re.compile(r"^KP2"), "Virgin Is."),
    (re.compile(r"^[AKNW]H6"), "Hawaii"),
    (re.compile(r"^[AKNW]H7(?!K)"), "Hawaii"),         # KH7K = rare Kure, leave it
    (re.compile(r"^[AKNW]H2"), "Guam"),
    (re.compile(r"^[AKNW]H0"), "Mariana Is."),
    (re.compile(r"^[AKNW]L\d"), "Alaska"),
    (re.compile(r"^E[A-H]8"), "Canary Is."),
    (re.compile(r"^E[A-H]9"), "Ceuta & Melilla"),
    (re.compile(r"^(?:R[A-Z]?|U[A-I])[890]"), "Asiatic Russia"),
    (re.compile(r"^(?:R[A-Z]?|U[A-I])2F"), "Kaliningrad"),
    (re.compile(r"^R1(?!F)"), "European Russia"),      # R1FJ stays Franz Josef
    (re.compile(r"^(?:R[A-Z]?|U[A-I])[2-7]"), "European Russia"),
]


def _override_entity(call):
    for core in _call_cores(call):
        for rx, ent in _PREFIX_OVERRIDES:
            if rx.match(core):
                return ent
    return ""


def _dxcc_match(call):
    """First DXCC match across a call's cores: (rec, confident).

    A curated override wins outright; otherwise confident == the matched key
    kept every leading prefix letter."""
    ent = _override_entity(call)
    if ent:
        return ENTITY_REC.get(ent, {"entity": ent}), True
    for core in _call_cores(call):
        rec, key, head = _lookup_head(core)
        if rec:
            return rec, len(key) >= _leading_alpha(head)
    return None, False


def resolve_dxcc(call):
    rec, _confident = _dxcc_match(call)
    return rec


def resolve_itu(call):
    for core in _call_cores(call):
        rec = ITU.get(core[:2])
        if rec and rec.get("cont"):
            return rec
    return None


def _call_core(call):
    for c in _call_cores(call):
        if _lookup_head(c)[0]:
            return c
    cores = _call_cores(call)
    return cores[0] if cores else (call or "").upper().strip()


US_AREAS = {
    "0": (41.5, -96.0), "1": (43.5, -71.5), "2": (42.0, -75.0),
    "3": (40.0, -77.0), "4": (33.0, -82.0), "5": (32.0, -97.0),
    "6": (37.0, -120.0), "7": (44.0, -114.0), "8": (40.5, -82.5),
    "9": (42.0, -89.0),
}
CA_AREAS = {
    "VE1": (45.0, -63.0), "VE2": (49.0, -72.0), "VE3": (45.5, -80.0),
    "VE4": (53.0, -98.0), "VE5": (53.0, -106.0), "VE6": (54.0, -115.0),
    "VE7": (53.0, -123.0), "VE8": (64.0, -120.0), "VE9": (46.5, -66.0),
    "VO1": (47.6, -53.0), "VO2": (53.5, -60.0),
    "VY1": (63.0, -135.0), "VY2": (46.3, -63.1), "VY0": (64.0, -95.0),
}


def refine_domestic(call, entity, lat, lon):
    """USA-mainland / Canada -> call-area centroid (lat, lon, is_domestic)."""
    core = _call_core(call)
    if entity == "United States of America":
        m = re.search(r"\d", core)
        if m and m.group() in US_AREAS:
            return US_AREAS[m.group()][0], US_AREAS[m.group()][1], True
        return lat, lon, True
    if entity == "Canada":
        m = re.match(r"(VE|VA|VO|VY)(\d)", core)
        if m:
            grp = "VE" if m.group(1) in ("VE", "VA") else m.group(1)
            key = grp + m.group(2)
            if key in CA_AREAS:
                return CA_AREAS[key][0], CA_AREAS[key][1], True
        return lat, lon, True
    return lat, lon, False


def strip_spotter(call):
    """RBN spotter tag -> plain call: 'W3LPL-#' -> 'W3LPL', 'DK9IP-1-#' -> 'DK9IP'."""
    c = (call or "").upper().strip()
    c = re.sub(r"-#$", "", c)
    c = re.sub(r"-\d+$", "", c)
    return c


def locate_call(call):
    """Location dict for any callsign: entity/lat/lon/cont/src.

    lat/lon/cont may be None when unresolved. Spotter '-#' tags are handled
    by the caller via strip_spotter()."""
    rec, confident = _dxcc_match(call)

    def _from_dxcc(rec):
        entity = rec.get("entity")
        lat, lon, _ = refine_domestic(call, entity, rec.get("lat"), rec.get("lon"))
        cont = (rec.get("cont") or "").split("/")[0] or None
        return {"entity": entity, "lat": lat, "lon": lon, "cont": cont,
                "src": "dxcc"}

    if rec and confident:
        return _from_dxcc(rec)
    irec = resolve_itu(call)
    if irec:
        # ITU answers with the country centroid; US/Canada calls that landed
        # here (e.g. 'WU6P': key 'W' dropped a letter of head 'WU6') still
        # deserve their call-area centroid.
        lat, lon, _ = refine_domestic(call, irec.get("country"),
                                      irec.get("lat"), irec.get("lon"))
        return {"entity": irec.get("country"), "lat": lat, "lon": lon,
                "cont": irec.get("cont"), "src": "itu"}
    if rec:
        return _from_dxcc(rec)
    return {"entity": None, "lat": None, "lon": None, "cont": None, "src": "none"}


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------

def bearing(clat, clon, plat, plon):
    """Great-circle initial bearing (deg, 0..360) from center to point."""
    r = math.pi / 180.0
    dl = (plon - clon) * r
    la1, la2 = clat * r, plat * r
    y = math.sin(dl) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def gc_km(clat, clon, plat, plon):
    """Great-circle distance in km."""
    r = math.pi / 180.0
    dl = (plon - clon) * r
    la1, la2 = clat * r, plat * r
    a = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin(dl / 2) ** 2)
    return 6371.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def maiden_to_latlon(grid):
    """Maidenhead grid -> (lat, lon) center. Accepts 4- or 6-char."""
    g = (grid or "").strip().upper()
    if len(g) < 4:
        return None
    lon = (ord(g[0]) - 65) * 20 - 180
    lat = (ord(g[1]) - 65) * 10 - 90
    lon += (ord(g[2]) - 48) * 2
    lat += (ord(g[3]) - 48) * 1
    if len(g) >= 6 and g[4].isalpha() and g[5].isalpha():
        lon += (ord(g[4]) - 65) * (2.0 / 24.0) + (1.0 / 24.0)
        lat += (ord(g[5]) - 65) * (1.0 / 24.0) + (0.5 / 24.0)
    else:
        lon += 1.0
        lat += 0.5
    return lat, lon


def parse_qth(s):
    """'CM87' / 'CM87xi' / 'lat,lon' / 'lat lon' -> (lat, lon) or None."""
    s = (s or "").strip()
    if not s:
        return None
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)[,\s]+(-?\d+(?:\.\d+)?)\s*$", s)
    if m:
        return float(m.group(1)), float(m.group(2))
    if re.match(r"^[A-Ra-r]{2}\d{2}([A-Xa-x]{2})?$", s):
        return maiden_to_latlon(s)
    return None
