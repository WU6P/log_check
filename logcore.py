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
    """Split a Cabrillo QSO: body into (their_call, exchange_tokens, call_idx).

    Layout is symmetric: freq mode date time MYCALL <sent...> THEIR <rcvd...>.
    The received call sits at index 5 + floor((n-6)/2); everything after it is
    the received exchange. Falls back to the 2nd callsign-shaped token.
    call_idx is the body index of the received call (-1 if not located), used to
    rebuild the line verbatim on save."""
    if len(body) < 7:
        return None, [], -1
    idx = 5 + (len(body) - 6) // 2
    cand = body[idx] if idx < len(body) else None
    if not (cand and _is_callsign(cand)):
        shaped = [t for t in body[4:] if _is_callsign(t)]   # MYCALL onward
        cand = shaped[1] if len(shaped) >= 2 else cand
        try:
            idx = body.index(cand)
        except (ValueError, TypeError):
            return cand, [], -1
    return cand, body[idx + 1:], idx


def parse_cabrillo_records(text):
    """Parse Cabrillo QSO:/X-QSO: lines into ADIF-shaped dicts.

    Same dict shape as parse_adif_records (CALL, QSO_DATE, TIME_ON, BAND, MODE,
    FREQ, RST_RCVD) plus SRX_STRING holding the received exchange tokens after
    the report, so the exchange check has something to work on. X-QSO lines are
    skipped."""
    records = []
    for lineno, line in enumerate(text.splitlines()):
        s = line.strip()
        if not s or not s.upper().startswith("QSO:"):
            continue
        body = s.split()[1:]
        if len(body) < 6:
            continue
        freq, mode, d, t = body[0], body[1].upper(), body[2], body[3]
        call, rcvd, call_idx = _cabrillo_split(body)
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
            # Bookkeeping for verbatim Cabrillo round-trip on save (see
            # serialize_cabrillo); never written to ADIF (leading underscore).
            "_CAB_BODY": body,
            "_CAB_CALL_IDX": call_idx,
            "_CAB_RCVD_IDX": call_idx + 1,
            "_CAB_LINE": lineno,
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


def detect_format(text):
    """'adif' or 'cabrillo' for a log's text, mirroring records_from_text.

    Determines which format to write back out so Save preserves the input
    format (ADIF in → ADIF out, Cabrillo .log in → Cabrillo out)."""
    if re.search(r"<EOH>", text, re.IGNORECASE) or re.search(r"<CALL", text, re.IGNORECASE):
        return "adif"
    if re.search(r"^\s*QSO:", text, re.IGNORECASE | re.MULTILINE) or \
       "START-OF-LOG" in text.upper():
        return "cabrillo"
    return "adif" if parse_adif_records(text) else "cabrillo"


def load_log(path):
    """Read a .adi/.adif/.log file into a list of QSO dicts."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return records_from_text(text)


def _rebuild_cabrillo_line(rec, original_line):
    """One Cabrillo QSO: line rebuilt from `rec`, preserving every untouched
    token (frequency, mode, the whole sent side) from the original body and
    writing back only the fields this tool edits: call, date, time, and the
    received report + exchange. Records with no stored body are left verbatim."""
    orig = rec.get("_CAB_BODY")
    if not orig:
        return original_line
    body = list(orig)
    ci = rec.get("_CAB_CALL_IDX", -1)
    ri = rec.get("_CAB_RCVD_IDX", len(body))
    if 0 <= ci < len(body):
        body[ci] = rec.get("CALL", "") or ""
    d = rec.get("QSO_DATE", "") or ""
    if len(d) == 8 and len(body) > 2:
        body[2] = f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
    t = (rec.get("TIME_ON", "") or "").replace(":", "")
    if t and len(body) > 3:
        body[3] = t[:4]
    rcvd = []
    rst = rec.get("RST_RCVD", "") or ""
    if rst:
        rcvd.append(rst)
    rcvd.extend((rec.get("SRX_STRING", "") or "").split())
    body = body[:ri] + rcvd
    # Untouched QSO: pass the original line through verbatim so a no-edit save
    # preserves the file's column alignment and trailing whitespace exactly.
    if body == orig:
        return original_line
    # Edited QSO: splice the new values into the original line in place so the
    # column alignment matches the untouched lines (only when the token count is
    # unchanged, which holds for every value edit; else fall back to single space).
    spans = _token_spans(original_line, orig)
    if spans is None or len(body) != len(orig):
        return "QSO: " + " ".join(body)
    out = original_line[:spans[0][0]]                  # "QSO:" + leading pad
    for i, (s, e) in enumerate(spans):
        out += body[i]
        gap_end = spans[i + 1][0] if i + 1 < len(spans) else len(original_line)
        gap = original_line[e:gap_end]                 # spaces after this token
        delta = len(body[i]) - len(orig[i])            # absorb length change in
        if delta and gap:                              # the trailing pad so the
            floor = 0 if i + 1 == len(spans) else 1    # next column stays put
            gap = " " * max(floor, len(gap) - delta)
        out += gap
    return out


def _token_spans(line, tokens):
    """(start, end) char span of each token in `line`, scanning left to right,
    or None if a token can't be located (then the caller reformats instead)."""
    spans, pos = [], 0
    for tok in tokens:
        i = line.find(tok, pos)
        if i < 0:
            return None
        spans.append((i, i + len(tok)))
        pos = i + len(tok)
    return spans


def serialize_cabrillo(records, original_text):
    """Write `records` back as a Cabrillo log, preserving the original file.

    The header, footer, comments and any X-QSO lines are kept verbatim; each
    QSO: line is rebuilt in place from its (possibly edited) record, deleted
    QSOs drop their line, and QSO: lines the parser couldn't read are left
    untouched. `original_text` is the text the log was loaded from."""
    nl = "\r\n" if "\r\n" in original_text else "\n"
    lines = original_text.splitlines()
    parsed_lines = {r.get("_CAB_LINE") for r in parse_cabrillo_records(original_text)}
    surviving = {r["_CAB_LINE"]: r for r in records if r.get("_CAB_LINE") is not None}
    out = []
    for i, line in enumerate(lines):
        if line.strip().upper().startswith("QSO:"):
            if i in surviving:
                out.append(_rebuild_cabrillo_line(surviving[i], line))
            elif i in parsed_lines:
                continue                       # deleted QSO
            else:
                out.append(line)               # unparsed QSO line, keep as-is
        else:
            out.append(line)
    text = nl.join(out)
    if original_text.endswith(("\n", "\r")):   # preserve trailing newline if any
        text += nl
    return text


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


def _load_entity_recs():
    """entity name -> full record (with cq/itu zones), for prefix overrides."""
    path = HERE / "dxcc.json"
    if not path.exists():
        return {}
    ents = json.loads(path.read_text(encoding="utf-8")).get("entities", [])
    return {e["entity"]: e for e in ents}


DXCC_CODE = _load_dxcc_codes()
ENTITY_REC = _load_entity_recs()
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
    if core in DXCC:                 # full-call exception key (e.g. KH7K = Kure I.)
        return DXCC[core], core, core
    m = re.match(r"([A-Z0-9]+?\d)", core)
    head = m.group(1) if m else core
    for n in range(len(head), 0, -1):
        key = head[:n]
        if key in DXCC:
            return DXCC[key], key, head
    return None, None, None


# The shared dxcc.json (built from the ARRL DXCC PDF) keeps only coarse prefix
# keys, so a call-area-split family resolves to the wrong entity (and hence the
# wrong CQ/ITU zone): Hawaii/Guam/Alaska map to "USA", Asiatic Russia and
# Kaliningrad to "European Russia", the Canaries/Ceuta to "Spain", and bare
# R<digit> Russian calls don't resolve at all. These curated overrides map each
# split to the right entity (which fixes the zone and rarity checks too). Order
# matters only where patterns overlap (the Russian digit rules are disjoint).
# Add a row here if another umbrella/split prefix turns up mis-resolved.
_PREFIX_OVERRIDES = [
    (re.compile(r"^KP[34]"), "Puerto Rico"),
    (re.compile(r"^KP2"), "Virgin Is."),
    (re.compile(r"^[AKNW]H6"), "Hawaii"),
    (re.compile(r"^[AKNW]H7(?!K)"), "Hawaii"),         # KH7K = rare Kure, leave it
    (re.compile(r"^[AKNW]H2"), "Guam"),
    (re.compile(r"^[AKNW]H0"), "Mariana Is."),
    (re.compile(r"^[AKNW]L\d"), "Alaska"),
    (re.compile(r"^E[A-H]8"), "Canary Is."),           # Spanish call area 8
    (re.compile(r"^E[A-H]9"), "Ceuta & Melilla"),      # Spanish call area 9
    (re.compile(r"^(?:R[A-Z]?|U[A-I])[890]"), "Asiatic Russia"),  # districts 8/9/0
    (re.compile(r"^(?:R[A-Z]?|U[A-I])2F"), "Kaliningrad"),        # the "2F" series
    (re.compile(r"^R1(?!F)"), "European Russia"),      # R1x except Franz Josef R1F
    (re.compile(r"^(?:R[A-Z]?|U[A-I])[2-7]"), "European Russia"), # other districts 2-7
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
# Zone vs. entity   /   callsign plausibility   /   near-dupe (UBN) checks
# ==========================================================================

def _parse_zones(spec):
    """A CQ/ITU zone field -> set of ints, or None if indeterminate.

    Handles '25', '3,4,5' and '1-5'. Parenthetical markers like '(G)' (a
    footnote in the ARRL table) and empty fields yield None, so multi-zone or
    unknown entities are skipped rather than mis-flagged."""
    if not spec:
        return None
    out = set()
    for tok in str(spec).split(","):
        tok = tok.strip()
        m = re.fullmatch(r"(\d+)-(\d+)", tok)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            out.update(range(min(a, b), max(a, b) + 1))
        elif tok.isdigit():
            out.add(int(tok))
        # anything else (e.g. "(G)") is a marker we can't resolve -> ignore
    return out or None


def expected_zones(call, kind):
    """Set of valid CQ ('cq') or ITU ('itu') zones for a call's entity, or None.

    Only a confident DXCC match is used; an unknown/override entity returns
    None so the zone check stays silent rather than guessing."""
    rec, confident = _dxcc_match(call)
    if not (rec and confident):
        return None
    return _parse_zones(rec.get(kind))


def zone_problem(qso):
    """(field, logged_zone, expected_set) if a logged zone conflicts with the
    worked entity, else None. Uses CQZ (vs CQ zone) first, then ITUZ.

    Only fires when the entity's zone set is known *and* the logged value is a
    plain integer outside it — so a wrong call that N1MM auto-zoned, or a
    hand-typed zone, stands out, while multi-zone giants never false-flag."""
    call = qso.get("CALL", "")
    for field, kind in (("CQZ", "cq"), ("ITUZ", "itu")):
        v = (qso.get(field, "") or "").strip()
        if not (v and v.isdigit()):
            continue
        exp = expected_zones(call, kind)
        if exp is not None and int(v) not in exp:
            return field, int(v), exp
        return None                      # primary zone field present & OK/unknown
    return None


_CALL_OK = re.compile(r"^[A-Z0-9/]+$")


@lru_cache(maxsize=20000)
def call_problem(call):
    """'' if the callsign is fine, else 'malformed' or 'unresolved'.

    malformed  — wrong shape (no letter+digit, illegal chars, bad length).
    unresolved — well-formed but maps to no DXCC *or* ITU country (an exotic
                 prefix that's almost always a typo in a contest log)."""
    c = (call or "").upper().strip()
    if not (3 <= len(c) <= 15 and _CALL_OK.match(c)
            and any(ch.isalpha() for ch in c) and any(ch.isdigit() for ch in c)):
        return "malformed"
    return "" if entity_of(c) else "unresolved"


def _suffix_split(call):
    """(prefix-through-last-digit, trailing-letter suffix), or None.

    'K3EST' -> ('K3', 'EST'); 'JR2HCZ' -> ('JR2', 'HCZ'). Comparing only within
    a shared prefix keeps the near-dupe check from matching calls that differ in
    their region/number (almost always a *different* station, not a mis-copy)."""
    last = -1
    for i, ch in enumerate(call):
        if ch.isdigit():
            last = i
    if last < 0 or last == len(call) - 1:
        return None
    return call[:last + 1], call[last + 1:]


def near_dupes(records, freq_min=3, suffix_min=3):
    """Map {busted_call -> likely_correct_call} for UBN-style near-dupes.

    A call worked exactly once whose suffix is one character (a single
    substitution) off a call worked `freq_min`+ times with the *same prefix* is
    flagged as a probable mis-copy of that busier station. The suffix must be at
    least `suffix_min` letters: with 1-2 letter contest calls (S53A vs S53D)
    a one-letter difference is just two distinct stations, so only longer
    suffixes carry a real copy-error signal."""
    counts = Counter((r.get("CALL", "") or "").upper().strip() for r in records)
    counts.pop("", None)
    by_prefix = defaultdict(list)
    for c in sorted((c for c, n in counts.items() if n >= freq_min),
                    key=lambda c: -counts[c]):
        sp = _suffix_split(c)
        if sp and len(sp[1]) >= suffix_min:
            by_prefix[sp[0]].append((c, sp[1]))
    out = {}
    for call, n in counts.items():
        if n != 1:
            continue
        sp = _suffix_split(call)
        if not sp or len(sp[1]) < suffix_min:
            continue
        pre, suf = sp
        for anchor, asuf in by_prefix.get(pre, ()):
            if len(asuf) == len(suf) and sum(x != y for x, y in zip(suf, asuf)) == 1:
                out[call] = anchor
                break
    return out


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
            "zone_bust": bool, "zone_exp": str, "zone_logged": str,  # zone check
            "call_bad": str,                            # ''/'malformed'/'unresolved'
            "dupe_of": str,                             # suggested correct call, or ''
        }
    plus log-level summary fields:
        exchange_field, majority_value, majority_share, is_fixed,
        is_serial, exch_applicable, rare_count, bust_count, fixes,
        zone_count, callbad_count, dupe_count
    `fixes` is a list of (index, old_value, new_value) proposals (see
    propose_fixes); apply them with apply_fixes().
    """
    n = len(records)
    field = exchange_field if exchange_field is not None else detect_exchange_field(records)

    per = [{"entity": entity_of(r.get("CALL", "")), "rank": rare_rank(r.get("CALL", "")),
            "exch": _val(r, field) if field else "", "exch_bust": False,
            "zone_bust": False, "zone_exp": "", "zone_logged": "",
            "call_bad": call_problem(r.get("CALL", "")), "dupe_of": ""}
           for r in records]
    rare_count = sum(1 for p in per if p["rank"] is not None)

    # --- zone vs. entity, and UBN near-dupe -----------------------------
    for i, r in enumerate(records):
        zp = zone_problem(r)
        if zp:
            fld, logged, exp = zp
            per[i]["zone_bust"] = True
            per[i]["zone_logged"] = f"{fld}={logged}"
            per[i]["zone_exp"] = ",".join(str(z) for z in sorted(exp))
    dupes = near_dupes(records)
    for i, r in enumerate(records):
        sug = dupes.get((r.get("CALL", "") or "").upper().strip())
        if sug:
            per[i]["dupe_of"] = sug
    zone_count = sum(1 for p in per if p["zone_bust"])
    callbad_count = sum(1 for p in per if p["call_bad"])
    dupe_count = sum(1 for p in per if p["dupe_of"])

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
        "zone_count": zone_count,
        "callbad_count": callbad_count,
        "dupe_count": dupe_count,
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
    # Exactly two QSOs that disagree: no majority to lean on, so treat the more
    # recent exchange as correct and propose changing the older QSO to match it.
    if n == 2:
        old, new = vals[0], vals[1]
        if old and new and old != new:
            return [(idxs[0], old, new)]
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


# --------------------------------------------------------------------------
# Change report — a plain-text record of exactly what was edited, written
# alongside the saved log so the operator (and a contest committee) can see the
# before/after at a glance. Mirrored in docs/logcore.js.
# --------------------------------------------------------------------------
def summary_text(result, count):
    """Plain-text version of the on-screen summary line (no HTML markup)."""
    r = result
    parts = [f"{count} QSOs", f"{r['rare_count']} rare DXCC"]
    if r["exchange_field"]:
        if r["exch_applicable"]:
            pct = round(r["majority_share"] * 100)
            fixed = (f"FIXED at '{r['majority_value']}' ({pct}%)" if r["is_fixed"]
                     else f"top '{r['majority_value']}' {pct}%")
            parts.append(f"exchange '{r['exchange_field']}' [{fixed}] — "
                         f"{r['bust_count']} busts, {len(r['fixes'])} auto-fixable")
        else:
            parts.append(f"exchange '{r['exchange_field']}' looks like serial "
                         f"numbers — check skipped")
    else:
        parts.append("no exchange field selected")
    parts.append(f"{r['zone_count']} zone, {r['callbad_count']} bad-call, "
                 f"{r['dupe_count']} near-dupe")
    return " | ".join(parts)


def _qso_desc(r):
    return " ".join(str(r[k]) for k in ("BAND", "MODE", "QSO_DATE", "TIME_ON")
                    if r.get(k))


def change_details(orig, cur):
    """Field-level changes between two record lists tagged with a stable _LCID.

    Returns {"modified", "removed", "added"}; `n` is the 1-based row in the
    original log (removed/modified) or the saved log (added).
    """
    def fields_of(r):
        return [k for k in r if not k.startswith("_")]

    cur_by_id = {r["_LCID"]: r for r in cur if r.get("_LCID") is not None}
    orig_ids = {r["_LCID"] for r in orig if r.get("_LCID") is not None}

    modified, removed = [], []
    for i, o in enumerate(orig):
        c = cur_by_id.get(o.get("_LCID")) if o.get("_LCID") is not None else None
        if c is None:
            removed.append({"n": i + 1, "call": o.get("CALL", "?"),
                            "desc": _qso_desc(o)})
            continue
        fields = []
        for k in sorted(set(fields_of(o)) | set(fields_of(c))):
            a = "" if o.get(k) is None else str(o.get(k, ""))
            b = "" if c.get(k) is None else str(c.get(k, ""))
            if a != b:
                fields.append({"key": k, "from": a, "to": b})
        if fields:
            modified.append({"n": i + 1, "call": c.get("CALL") or o.get("CALL", "?"),
                             "fields": fields})
    added = []
    for i, c in enumerate(cur):
        if c.get("_LCID") is None or c["_LCID"] not in orig_ids:
            added.append({"n": i + 1, "call": c.get("CALL", "?"),
                          "desc": _qso_desc(c)})
    return {"modified": modified, "removed": removed, "added": added}


def _lcs_diff(a, b):
    n, m = len(a), len(b)
    if n == 0:
        return [("+", s) for s in b]
    if m == 0:
        return [("-", s) for s in a]
    if n * m > 4_000_000:                       # pathological: coarse replace
        return [("-", s) for s in a] + [("+", s) for s in b]
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            dp[i][j] = (dp[i + 1][j + 1] + 1 if a[i] == b[j]
                        else max(dp[i + 1][j], dp[i][j + 1]))
    out = []
    i = j = 0
    while i < n and j < m:
        if a[i] == b[j]:
            out.append((" ", a[i])); i += 1; j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            out.append(("-", a[i])); i += 1
        else:
            out.append(("+", b[j])); j += 1
    while i < n:
        out.append(("-", a[i])); i += 1
    while j < m:
        out.append(("+", b[j])); j += 1
    return out


def unified_diff(a, b, from_label="original", to_label="modified", context=3):
    """Unified text diff of two lists of lines; "" when they are identical.

    Trims the common prefix/suffix first (cheap for near-identical files), then
    runs an LCS on the middle.
    """
    lo = 0
    while lo < len(a) and lo < len(b) and a[lo] == b[lo]:
        lo += 1
    ha, hb = len(a), len(b)
    while ha > lo and hb > lo and a[ha - 1] == b[hb - 1]:
        ha -= 1; hb -= 1

    # ops: [tag, text, a_lineno, b_lineno]
    ops = [[" ", a[i], 0, 0] for i in range(lo)]
    ops += [[t, s, 0, 0] for t, s in _lcs_diff(a[lo:ha], b[lo:hb])]
    ops += [[" ", a[i], 0, 0] for i in range(ha, len(a))]
    if not any(o[0] != " " for o in ops):
        return ""

    al = bl = 0
    for o in ops:
        if o[0] != "+":
            al += 1; o[2] = al
        if o[0] != "-":
            bl += 1; o[3] = bl

    wins = []
    for k, o in enumerate(ops):
        if o[0] == " ":
            continue
        s = max(0, k - context)
        e = min(len(ops) - 1, k + context)
        if wins and s <= wins[-1][1] + 1:
            wins[-1][1] = max(wins[-1][1], e)
        else:
            wins.append([s, e])

    out = [f"--- {from_label}", f"+++ {to_label}"]
    for s, e in wins:
        a_start = a_count = b_start = b_count = 0
        for k in range(s, e + 1):
            tag, _txt, an, bn = ops[k]
            if tag != "+":
                if not a_start:
                    a_start = an
                a_count += 1
            if tag != "-":
                if not b_start:
                    b_start = bn
                b_count += 1
        out.append(f"@@ -{a_start or 0},{a_count} +{b_start or 0},{b_count} @@")
        for k in range(s, e + 1):
            out.append(ops[k][0] + ops[k][1])
    return "\n".join(out) + "\n"


def build_change_report(orig, cur, file_name="log", fmt="adif", src_text="",
                        check_summary=""):
    """Full change-report text: header, the check-results line, a human summary
    of every edit, and a unified diff of the original vs the saved log."""
    ext = ".log" if fmt == "cabrillo" else ".adi"
    old_text = (serialize_cabrillo(orig, src_text) if fmt == "cabrillo"
                else serialize_adif(orig))
    new_text = (serialize_cabrillo(cur, src_text) if fmt == "cabrillo"
                else serialize_adif(cur))
    det = change_details(orig, cur)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    L = ["log_check — change report",
         f"File:      {file_name}{ext}",
         f"Generated: {stamp}",
         f"Format:    {'Cabrillo' if fmt == 'cabrillo' else 'ADIF'}",
         ""]
    if check_summary:
        L += ["== Check results ==", check_summary, ""]

    L.append("== Summary of changes ==")
    L.append(f"QSOs: {len(orig)} (original) -> {len(cur)} (saved)")
    L.append(f"  modified: {len(det['modified'])}   removed: {len(det['removed'])}"
             f"   added: {len(det['added'])}")
    L.append("")
    if det["modified"]:
        L.append("Modified QSOs:")
        for m in det["modified"]:
            chg = ", ".join(f"{x['key']}: '{x['from']}' -> '{x['to']}'"
                            for x in m["fields"])
            L.append(f"  #{m['n']} {m['call']}  {chg}")
        L.append("")
    if det["removed"]:
        L.append("Removed QSOs:")
        for r in det["removed"]:
            L.append(f"  #{r['n']} {r['call']}  {r['desc']}")
        L.append("")
    if det["added"]:
        L.append("Added QSOs:")
        for r in det["added"]:
            L.append(f"  #{r['n']} {r['call']}  {r['desc']}")
        L.append("")
    if not (det["modified"] or det["removed"] or det["added"]):
        L.append("(no changes — the saved log is identical to the original)\n")

    L.append("== Unified diff (original -> saved) ==")
    diff = unified_diff(old_text.split("\n"), new_text.split("\n"),
                        f"{file_name}{ext} (original)", f"{file_name}{ext} (saved)")
    L.append(diff or "(no differences)")
    return "\n".join(L) + "\n"
