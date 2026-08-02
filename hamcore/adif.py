"""ADIF: read a log, write a log.

Four projects each grew their own `parse_adif_records` — logan, log_check,
Contest_Plan and ADIF_import. Differential-tested against each other on ten
edge cases (headers, lower-case tags, typed fields, length mismatches, empty
fields, UTF-8, CRLF, a missing final <EOR>) they agree exactly, so this is
consolidation rather than a rescue: one implementation, one place to fix the
next edge case somebody's logging program invents.

The scan is manual rather than `finditer` for one specific reason: after
reading a counted value it resumes *after* that value, so tag-like text
inside a comment or a name field cannot corrupt the parse. `<COMMENT:14>see
<EOR> below` is a real thing that happens.

Pure standard library.
"""
# ---------------------------------------------------------------------------
# VENDORED from hamcore 1.0.0 -- do not edit here.
# Edit AI/hamcore/hamcore/adif.py and re-run:
#     python3 -m hamcore.vendor sync
# ---------------------------------------------------------------------------

import re
from datetime import datetime, timezone

# <NAME:len:TYPE> — the length and the type indicator are both optional.
TAG_RE = re.compile(r"<([A-Za-z0-9_]+)(?::(\d+))?(?::[A-Za-z])?>")


def parse_adif_records(text):
    """ADIF text -> a list of per-QSO dicts with upper-cased field names.

    Everything before <EOH> is the header and is skipped. A trailing record
    with no <EOR> is still returned — plenty of programs write logs that way,
    and dropping the last QSO of a log is a bad way to find out.
    """
    text = text or ""
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
    """One QSO dict -> one line of ADIF ending in <EOR>.

    Keys with a leading underscore are internal bookkeeping and are not
    written out.
    """
    parts = []
    for key, val in qso.items():
        if key.startswith("_"):
            continue
        val = "" if val is None else str(val)
        parts.append(f"<{key.upper()}:{len(val)}>{val}")
    parts.append("<EOR>")
    return " ".join(parts)


def serialize_adif(records, program="hamcore", header_comment=None):
    """Records -> a complete ADIF document with a minimal header."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d %H%M%S")
    comment = header_comment or f"{program} export"
    head = (f"{comment}\n"
            f"<ADIF_VER:5>3.1.0 <PROGRAMID:{len(program)}>{program} "
            f"<CREATED_TIMESTAMP:15>{stamp} <EOH>\n")
    return head + "\n".join(serialize_qso(r) for r in records) + "\n"


def qso_datetime(qso):
    """UTC datetime for a QSO, or None when the date/time is missing or bad.

    TIME_ON is commonly four digits (HHMM) and occasionally six; both are
    accepted, and a missing time is treated as midnight rather than
    discarding the QSO.
    """
    d = (qso.get("QSO_DATE", "") or "").strip()
    t = (qso.get("TIME_ON", "") or "").strip()
    if len(d) != 8:
        return None
    t = (t + "000000")[:6]
    try:
        return datetime.strptime(d + t, "%Y%m%d%H%M%S")
    except ValueError:
        return None


def field(qso, *names, default=""):
    """First present, non-empty value among `names` — ADIF has several
    spellings for the same idea (GRIDSQUARE / VUCC_GRIDS, STATION_CALLSIGN /
    OPERATOR) and callers should not each rediscover which."""
    for n in names:
        v = (qso.get(n.upper()) or "").strip()
        if v:
            return v
    return default
