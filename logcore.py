#!/usr/bin/env python3
"""log_check core — parse ham-contest logs and run two integrity checks.

This is the pure-stdlib, GUI-free engine behind log_check. It is imported by
log_check.py (the PyQt5 GUI) and exercised directly by test_log_check.py.

The ADIF / Cabrillo parsing and the ARRL-DXCC -> ITU callsign-series country
resolution chain are ported from the Contest_Plan project (which in turn took
them from logan) so a callsign resolves to a DXCC entity and, if it is on the
"most wanted" list, a rarity rank.

Two checks:
  1. Rare DXCC      — flag QSOs whose worked entity is on the rare.json
                      most-wanted list (a likely busted call in a domestic log).
  2. Exchange       — for a chosen exchange field, flag QSOs whose received
                      exchange disagrees with what the same station gave on its
                      other QSOs (a station sends one exchange all contest), and
                      propose automatic fixes for the clear-cut cases.

Pure standard library — no third-party imports here.
"""

import json
import re
from functools import lru_cache
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent


# ==========================================================================
# ADIF parsing / serialization (ported from Contest_Plan / ADIF_import)
# ==========================================================================

# A field looks like <CALL:5>W1AW. The number is the value length; an optional
# second :T is a data-type hint we ignore.
TAG_RE = re.compile(r"<([A-Za-z0-9_]+)(?::(\d+))?(?::[A-Za-z])?>")


def parse_adif_records(text):
    """Parse ADIF text into a list of per-QSO dicts (upper-cased field names).

    Manual scan (not finditer): after reading a counted value we resume AFTER
    the value, so tag-like text inside a value can't corrupt the parse.
    """
    eoh = re.search(r"<EOH>", text, re.IGNORECASE)
    pos = eoh.end() if eoh else 0
    records, current = [], {}
    while True:
        m = TAG_RE.search(text, pos)
        if not m:
            break
        name = m.group(1).upper()
        length = m.group(2)
        pos = m.end()
        if name == "EOR":
            if current:
                records.append(current)
                current = {}
            continue
        if name == "EOH":
            continue
        if length is not None:
            ln = int(length)
            current[name] = text[pos:pos + ln]
            pos += ln
        else:
            current[name] = ""
    if current:
        records.append(current)
    return records


def serialize_qso(qso):
    """Turn a QSO dict back into one line of ADIF ending with <EOR>.

    Internal bookkeeping keys (leading underscore) are not written out.
    """
    parts = []
    for key, val in qso.items():
        if key.startswith("_"):
            continue
        val = "" if val is None else str(val)
        parts.append(f"<{key.upper()}:{len(val)}>{val}")
    parts.append("<EOR>")
    return " ".join(parts)


def serialize_adif(records, header_comment="log_check export"):
    """Serialize records to a full ADIF document with a minimal header."""
    stamp = datetime.now().strftime("%Y%m%d %H%M%S")
    head = (f"{header_comment}\n"
            f"<ADIF_VER:5>3.1.0 <PROGRAMID:9>log_check "
            f"<CREATED_TIMESTAMP:15>{stamp} <EOH>\n")
    body = "\n".join(serialize_qso(r) for r in records)
    return head + body + "\n"


def qso_datetime(qso):
    """UTC datetime for a QSO, or None if date/time missing/invalid."""
    d = (qso.get("QSO_DATE", "") or "").strip()
    t = (qso.get("TIME_ON", "") or "").strip()
    if len(d) != 8:
        return None
    t = (t + "000000")[:6]
    try:
        return datetime.strptime(d + t, "%Y%m%d%H%M%S")
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Cabrillo (.log) parsing — many contest dirs only ship a Cabrillo log.
# --------------------------------------------------------------------------

_BAND_EDGES = [
    (1800, 2000, "160M"), (3500, 4000, "80M"), (5250, 5450, "60M"),
    (7000, 7300, "40M"), (10100, 10150, "30M"), (14000, 14350, "20M"),
    (18068, 18168, "17M"), (21000, 21450, "15M"), (24890, 24990, "12M"),
    (28000, 29700, "10M"), (50000, 54000, "6M"), (70000, 71000, "4M"),
    (144000, 148000, "2M"), (420000, 450000, "70CM"),
]
_CAB_MODE = {"CW": "CW", "PH": "SSB", "SSB": "SSB", "RY": "RTTY",
             "RTTY": "RTTY", "FM": "FM", "DG": "DATA", "DI": "DATA"}


def khz_to_band(khz):
    """ADIF band name for a frequency in kHz (or '' if out of any ham band)."""
    try:
        f = float(khz)
    except (TypeError, ValueError):
        return ""
    for lo, hi, name in _BAND_EDGES:
        if lo <= f <= hi:
            return name
    return ""


def _is_callsign(tok):
    """A token shaped like a callsign: has a letter and a digit, sane length."""
    t = (tok or "").upper()
    return (3 <= len(t) <= 12 and any(c.isalpha() for c in t)
            and any(c.isdigit() for c in t)
            and all(c.isalnum() or c == "/" for c in t))


def _cabrillo_split(body):
    """Split a Cabrillo QSO: line body into (their_call, exchange_tokens).

    Layout is symmetric: freq mode date time MYCALL <sent...> THEIR <rcvd...>.
    The received call sits at index 5 + floor((n-6)/2); everything after it is
    the received exchange. Falls back to the 2nd callsign-shaped token."""
    if len(body) < 7:
        return None, []
    idx = 5 + (len(body) - 6) // 2
    cand = body[idx] if idx < len(body) else None
    if not (cand and _is_callsign(cand)):
        shaped = [t for t in body[4:] if _is_callsign(t)]   # MYCALL onward
        cand = shaped[1] if len(shaped) >= 2 else cand
        try:
            idx = body.index(cand)
        except (ValueError, TypeError):
            return cand, []
    return cand, body[idx + 1:]


def parse_cabrillo_records(text):
    """Parse Cabrillo QSO:/X-QSO: lines into ADIF-shaped dicts.

    Same dict shape as parse_adif_records (CALL, QSO_DATE, TIME_ON, BAND, MODE,
    FREQ, RST_RCVD) plus SRX_STRING holding the received exchange tokens after
    the report, so the exchange check has something to work on. X-QSO lines are
    skipped."""
    records = []
    for line in text.splitlines():
        s = line.strip()
        if not s or not s.upper().startswith("QSO:"):
            continue
        body = s.split()[1:]
        if len(body) < 6:
            continue
        freq, mode, d, t = body[0], body[1].upper(), body[2], body[3]
        call, rcvd = _cabrillo_split(body)
        if not call:
            continue
        ds = d.replace("-", "")
        if len(ds) != 8 or len(t) not in (4, 6):
            continue
        rst = rcvd[0] if rcvd and re.fullmatch(r"\d{2,3}", rcvd[0]) else ""
        exch = rcvd[1:] if rst else rcvd
        records.append({
            "CALL": call.upper(),
            "QSO_DATE": ds,
            "TIME_ON": (t + "00")[:6] if len(t) == 4 else t,
            "BAND": khz_to_band(freq),
            "FREQ": freq,
            "MODE": _CAB_MODE.get(mode, mode),
            "RST_RCVD": rst,
            "SRX_STRING": " ".join(exch),
        })
    return records


def records_from_text(text):
    """Parse a log file's text as ADIF if it looks like ADIF, else Cabrillo."""
    if re.search(r"<EOH>", text, re.IGNORECASE) or re.search(r"<CALL", text, re.IGNORECASE):
        return parse_adif_records(text)
    if re.search(r"^\s*QSO:", text, re.IGNORECASE | re.MULTILINE) or \
       "START-OF-LOG" in text.upper():
        return parse_cabrillo_records(text)
    recs = parse_adif_records(text)
    return recs if recs else parse_cabrillo_records(text)


def load_log(path):
    """Read a .adi/.adif/.log file into a list of QSO dicts."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return records_from_text(text)


# ==========================================================================
# DXCC / ITU resolution + rarity (ported from Contest_Plan)
# ==========================================================================

def _load_lookup(name):
    path = HERE / name
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("lookup", {})


DXCC = _load_lookup("dxcc.json")
ITU = _load_lookup("itu.json")


def _load_dxcc_codes():
    path = HERE / "dxcc.json"
    if not path.exists():
        return {}
    ents = json.loads(path.read_text(encoding="utf-8")).get("entities", [])
    return {e["entity"]: e.get("code") for e in ents}


DXCC_CODE = _load_dxcc_codes()
RARE = (json.loads((HERE / "rare.json").read_text(encoding="utf-8")).get("rare", {})
        if (HERE / "rare.json").exists() else {})

_SUFFIXES = {"P", "M", "MM", "AM", "QRP", "A", "B"}


def _call_cores(call):
    call = (call or "").upper().strip()
    if "/" not in call:
        return [call] if call else []
    parts = [p for p in call.split("/")
             if p and p not in _SUFFIXES and any(c.isalpha() for c in p)]
    return sorted(parts, key=len) or [call.replace("/", "")]


def _leading_alpha(head):
    """How many leading letters the prefix head has (e.g. 'TM2' -> 2)."""
    i = 0
    while i < len(head) and head[i].isalpha():
        i += 1
    return i


def _lookup_head(core):
    """Longest-prefix DXCC match for one call core: (rec, key, head) or Nones.

    The key is the prefix string that matched, so callers can judge confidence:
    a match that dropped leading letters (e.g. France 'TM' collapsing to the
    catch-all 'T' = Kiribati) is unreliable."""
    m = re.match(r"([A-Z0-9]+?\d)", core)
    head = m.group(1) if m else core
    for n in range(len(head), 0, -1):
        key = head[:n]
        if key in DXCC:
            return DXCC[key], key, head
    return None, None, None


# The shared dxcc.json (built from the ARRL DXCC PDF) keeps only coarse prefix
# keys for a few entities, so an umbrella key resolves common calls to a rare
# neighbour: bare 'KP' -> Navassa (but KP3/KP4 are Puerto Rico, KP2 the Virgin
# Is.) and 'R1' -> Franz Josef Land (but only R1F* is Franz Josef; other R1
# calls are European Russia). These curated overrides veto those false rare
# flags. Add a row here if another umbrella prefix turns up over-flagged.
_PREFIX_OVERRIDES = [
    (re.compile(r"^KP[34]"), "Puerto Rico"),
    (re.compile(r"^KP2"), "Virgin Is."),
    (re.compile(r"^R1(?!F)"), "European Russia"),   # R1FJ stays Franz Josef Land
]


def _override_entity(call):
    """Curated entity for a call whose coarse DXCC prefix mis-resolves, else ''."""
    for core in _call_cores(call):
        for rx, ent in _PREFIX_OVERRIDES:
            if rx.match(core):
                return ent
    return ""


def _dxcc_match(call):
    """First DXCC match across a call's cores, with confidence.

    Confident == the matched key kept every leading letter of the prefix, so a
    multi-letter prefix can't be mis-resolved by a 1-letter catch-all entry.
    A curated override (see _PREFIX_OVERRIDES) wins outright when it applies."""
    ent = _override_entity(call)
    if ent:
        return {"entity": ent}, True
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


@lru_cache(maxsize=20000)
def entity_of(call):
    """DXCC entity (country) name for a callsign, or '' if unresolved.

    A low-confidence DXCC match (one that dropped leading prefix letters) is
    overridden by the ITU call-sign-series country when ITU has an answer, so
    e.g. a French 'TM' call isn't mislabelled Kiribati."""
    rec, confident = _dxcc_match(call)
    if rec and confident:
        return rec.get("entity") or ""
    irec = resolve_itu(call)
    if irec and irec.get("country"):
        return irec["country"]
    return (rec.get("entity") or "") if rec else ""


@lru_cache(maxsize=20000)
def rare_rank(call):
    """Most-wanted rank (1 = rarest) for a callsign's entity, or None.

    Only a *confident* ARRL-DXCC match counts: the entity code keys rare.json,
    and a low-confidence/ITU-only resolution must never raise a rare flag (that
    would turn ordinary calls into false busts)."""
    rec, confident = _dxcc_match(call)
    if not (rec and confident):
        return None
    code = DXCC_CODE.get(rec.get("entity"))
    if code is not None and str(code) in RARE:
        return int(RARE[str(code)])
    return None


# ==========================================================================
# Exchange field analysis
# ==========================================================================

# Fields that are never the contest's received exchange: identity, time, the
# band/freq/mode triplet, the sent side, and logger bookkeeping. Anything else
# present in the log is offered as an exchange-field candidate.
_NON_EXCHANGE = {
    "CALL", "QSO_DATE", "QSO_DATE_OFF", "TIME_ON", "TIME_OFF",
    "BAND", "BAND_RX", "FREQ", "FREQ_RX", "MODE", "SUBMODE",
    "RST_SENT", "STX", "STX_STRING", "STATION_CALLSIGN", "OPERATOR",
    "OWNER_CALLSIGN", "MY_GRIDSQUARE", "CONTEST_ID", "PFX", "QSL_RCVD",
    "QSL_SENT", "ID",
}

# Preferred received-exchange fields, rarest-first-guess order. The first one
# present in a useful share of the log becomes the auto-detected default.
_EXCHANGE_PRIORITY = [
    "SRX_STRING", "SRX", "RX_PWR", "STATE", "ARRL_SECT", "VE_PROV",
    "WPX_PREFIX", "CQZ", "ITUZ", "GRIDSQUARE", "CLASS", "PRECEDENCE",
    "CHECK", "NAME", "AGE", "RST_RCVD",
]


def _val(qso, field):
    return (qso.get(field, "") or "").strip()


def exchange_candidates(records):
    """Field names that could be the received exchange, present-count first.

    Excludes the universal/bookkeeping set and any APP_* logger field. Ordered
    by how often the field is populated (a real exchange is on most QSOs)."""
    counts = Counter()
    for r in records:
        for k, v in r.items():
            if k.startswith(("_", "APP_")) or k in _NON_EXCHANGE:
                continue
            if (v or "").strip():
                counts[k] += 1
    return [k for k, _ in counts.most_common()]


def detect_exchange_field(records):
    """Best-guess received-exchange field for a log, or '' if none look usable.

    Prefers the priority list (when populated on >=40% of QSOs), else falls
    back to the most-populated candidate field."""
    if not records:
        return ""
    cands = exchange_candidates(records)
    present = set(cands)
    n = len(records)
    for field in _EXCHANGE_PRIORITY:
        if field in present:
            filled = sum(1 for r in records if _val(r, field))
            if filled >= 0.4 * n:
                return field
    return cands[0] if cands else ""


def _distinct_ratio(records, field):
    vals = [_val(r, field) for r in records]
    vals = [v for v in vals if v]
    if not vals:
        return 1.0
    return len(set(vals)) / len(vals)


def analyze(records, exchange_field=None, fixed_threshold=0.9,
            serial_threshold=0.5, force_exchange=False):
    """Run both checks over `records`.

    Returns a dict whose `per_record` list is index-aligned with `records`:
        per_record[i] = {
            "entity": str, "rank": int|None,            # rare check
            "exch": str, "exch_bust": bool,             # exchange check
        }
    plus log-level summary fields:
        exchange_field, majority_value, majority_share, is_fixed,
        is_serial, exch_applicable, rare_count, bust_count, fixes
    `fixes` is a list of (index, old_value, new_value) proposals (see
    propose_fixes); apply them with apply_fixes().
    """
    n = len(records)
    field = exchange_field if exchange_field is not None else detect_exchange_field(records)

    per = [{"entity": entity_of(r.get("CALL", "")), "rank": rare_rank(r.get("CALL", "")),
            "exch": _val(r, field) if field else "", "exch_bust": False}
           for r in records]
    rare_count = sum(1 for p in per if p["rank"] is not None)

    # --- exchange check -------------------------------------------------
    maj_val, maj_share, is_fixed, is_serial, applicable = "", 0.0, False, False, False
    fixes = []
    if field:
        dist = Counter(p["exch"] for p in per if p["exch"])
        total = sum(dist.values())
        if total:
            maj_val, maj_n = dist.most_common(1)[0]
            maj_share = maj_n / total
        is_fixed = maj_share >= fixed_threshold
        is_serial = _distinct_ratio(records, field) > serial_threshold
        # A near-unique field (serial numbers) can't be cross-checked unless the
        # user forces it; a fixed field always can.
        applicable = force_exchange or is_fixed or not is_serial

        if applicable:
            groups = defaultdict(list)
            for i, r in enumerate(records):
                groups[(r.get("CALL", "") or "").upper().strip()].append(i)
            for call, idxs in groups.items():
                idxs.sort(key=lambda i: (qso_datetime(records[i]) or datetime.min, i))
                vals = [per[i]["exch"] for i in idxs]
                nonempty = [v for v in vals if v]
                # Within-station disagreement: minority values are suspicious.
                if len(set(nonempty)) > 1 or ("" in vals and nonempty):
                    grp_majority = Counter(nonempty).most_common(1)[0][0]
                    for i in idxs:
                        if per[i]["exch"] != grp_majority:
                            per[i]["exch_bust"] = True
                # Globally-fixed contest: a lone value that differs from the
                # contest-wide majority is suspicious even with no repeat.
                if is_fixed:
                    for i in idxs:
                        if per[i]["exch"] and per[i]["exch"] != maj_val:
                            per[i]["exch_bust"] = True
                fixes.extend(_group_fixes(idxs, vals))

    bust_count = sum(1 for p in per if p["exch_bust"])
    return {
        "per_record": per,
        "exchange_field": field,
        "majority_value": maj_val,
        "majority_share": maj_share,
        "is_fixed": is_fixed,
        "is_serial": is_serial,
        "exch_applicable": applicable,
        "rare_count": rare_count,
        "bust_count": bust_count,
        "fixes": fixes,
    }


def _group_fixes(idxs, vals):
    """Auto-fix proposals for one callsign group (idxs/vals sorted by time).

    Implements the rule: if the early exchange(s) differ but every later QSO
    carries the same value X (a contiguous trailing run), and X occurs more
    often than all the others combined, correct the early ones to X. The
    contiguous-suffix requirement is what encodes "logged it wrong at first,
    then got it right and kept getting it right" while refusing the ambiguous
    cases (scattered or tied values)."""
    n = len(vals)
    if n < 2:
        return []
    x = vals[-1]
    if not x:                                   # can't fix toward a blank
        return []
    suffix = 0
    for v in reversed(vals):
        if v == x:
            suffix += 1
        else:
            break
    if vals.count(x) != suffix:                 # X must appear only in the tail
        return []
    n_other = n - suffix
    if n_other == 0 or suffix <= n_other:       # need a strict majority tail
        return []
    return [(idxs[i], vals[i], x) for i in range(n_other) if vals[i] != x]


def propose_fixes(records, exchange_field=None, **kw):
    """Convenience: just the list of (index, old, new) exchange fixes."""
    return analyze(records, exchange_field=exchange_field, **kw)["fixes"]


def apply_fixes(records, field, fixes):
    """Apply (index, old, new) fixes to `records[i][field]`. Returns count."""
    applied = 0
    for i, _old, new in fixes:
        if 0 <= i < len(records):
            records[i][field] = new
            applied += 1
    return applied
