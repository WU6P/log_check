#!/usr/bin/env python3
"""Tests for log_check's core engine (logcore.py). Pure stdlib, no GUI.

    python3 test_log_check.py        # or: python3 -m unittest -v
"""

import unittest

import logcore as lc


def adif(*recs):
    """Build an ADIF document from (field=value) dicts for round-trip tests."""
    out = ["header <EOH>"]
    for r in recs:
        out.append(" ".join(f"<{k}:{len(str(v))}>{v}" for k, v in r.items()) + " <EOR>")
    return "\n".join(out)


def qso(call, date="20260101", time="000000", **extra):
    d = {"CALL": call, "QSO_DATE": date, "TIME_ON": time}
    d.update(extra)
    return d


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
class TestParsing(unittest.TestCase):
    def test_adif_basic(self):
        recs = lc.parse_adif_records(
            "x <EOH> <CALL:4>W1AW <BAND:3>20M <MODE:2>CW <EOR>"
            " <CALL:5>K3EST <BAND:3>15M <MODE:3>SSB <EOR>")
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["CALL"], "W1AW")
        self.assertEqual(recs[1]["MODE"], "SSB")

    def test_adif_tag_inside_value(self):
        # A value containing "<EOR>" must not split the record early.
        recs = lc.parse_adif_records("<EOH> <CALL:4>W1AW <COMMENT:10><EOR> hack <EOR>")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["COMMENT"], "<EOR> hack")

    def test_cabrillo_basic(self):
        text = ("START-OF-LOG: 3.0\n"
                "QSO: 14025 CW 2026-01-01 0000 N6RO 599 25 JJ0VNR 599 KW\n"
                "QSO: 21025 CW 2026-01-01 0001 N6RO 599 25 BD3TE 599 100\n"
                "X-QSO: 21025 CW 2026-01-01 0002 N6RO 599 25 DUPE 599 100\n")
        recs = lc.parse_cabrillo_records(text)
        self.assertEqual(len(recs), 2)            # X-QSO skipped
        self.assertEqual(recs[0]["CALL"], "JJ0VNR")
        self.assertEqual(recs[0]["BAND"], "20M")
        self.assertEqual(recs[0]["SRX_STRING"], "KW")
        self.assertEqual(recs[1]["SRX_STRING"], "100")

    def test_records_from_text_dispatch(self):
        self.assertEqual(len(lc.records_from_text("<EOH> <CALL:4>W1AW <EOR>")), 1)
        self.assertEqual(
            len(lc.records_from_text("QSO: 14025 CW 2026-01-01 0000 N6RO 599 1 W1AW 599 2")), 1)

    def test_serialize_roundtrip(self):
        recs = lc.parse_adif_records(adif(qso("W1AW", BAND="20M")))
        recs[0]["_internal"] = "ignore me"
        out = lc.serialize_adif(recs)
        again = lc.parse_adif_records(out)
        self.assertEqual(again[0]["CALL"], "W1AW")
        self.assertEqual(again[0]["BAND"], "20M")
        self.assertNotIn("_INTERNAL", again[0])     # underscore keys not written

    def test_qso_datetime(self):
        self.assertIsNone(lc.qso_datetime({"QSO_DATE": "bad"}))
        dt = lc.qso_datetime({"QSO_DATE": "20260101", "TIME_ON": "0102"})
        self.assertEqual((dt.hour, dt.minute), (1, 2))


# --------------------------------------------------------------------------
# DXCC / rarity
# --------------------------------------------------------------------------
class TestRare(unittest.TestCase):
    def test_entity_resolution(self):
        self.assertEqual(lc.entity_of("W1AW"), "United States of America")
        self.assertEqual(lc.entity_of("JJ0VNR"), "Japan")

    def test_rare_flag(self):
        # P5 (DPR of Korea) is rank 1 on the most-wanted list.
        self.assertEqual(lc.rare_rank("P5DX"), 1)
        # An ordinary US call is not rare.
        self.assertIsNone(lc.rare_rank("W1AW"))

    def test_rare_slash_call(self):
        # A /MM or portable indicator shouldn't break resolution.
        self.assertIsNone(lc.rare_rank("W1AW/M"))

    def test_multiletter_prefix_not_swallowed(self):
        # France 'TM' must not collapse to the catch-all 'T' (Kiribati) and get
        # a false rare flag — the original false-positive this guards against.
        self.assertEqual(lc.entity_of("TM6M"), "France")
        self.assertIsNone(lc.rare_rank("TM6M"))
        # A genuine rare DX op from Desecheo (KP5/...) is still flagged.
        self.assertIsNotNone(lc.rare_rank("KP5/NP3VI"))

    def test_coarse_prefix_overrides(self):
        # Common KP4/Puerto Rico and R1/European-Russia calls must not be
        # mislabelled as their rare neighbours (Navassa / Franz Josef Land).
        self.assertEqual(lc.entity_of("KP4CC"), "Puerto Rico")
        self.assertIsNone(lc.rare_rank("KP4CC"))
        self.assertEqual(lc.entity_of("R1DX"), "European Russia")
        self.assertIsNone(lc.rare_rank("R1DX"))
        # But the genuinely rare neighbours still resolve and flag.
        self.assertEqual(lc.rare_rank("R1FJ"), 51)      # Franz Josef Land
        self.assertEqual(lc.rare_rank("KP1AA"), 31)     # Navassa I.

    def test_analyze_counts_rare(self):
        recs = [qso("W1AW"), qso("P5DX"), qso("K3EST")]
        res = lc.analyze(recs, exchange_field="")
        self.assertEqual(res["rare_count"], 1)
        self.assertEqual(res["per_record"][1]["rank"], 1)
        self.assertIsNone(res["per_record"][0]["rank"])


# --------------------------------------------------------------------------
# Exchange detection
# --------------------------------------------------------------------------
class TestExchangeDetect(unittest.TestCase):
    def test_candidates_exclude_universal_and_app(self):
        recs = [qso("W1AW", BAND="20M", CQZ="5", APP_N1MM_X="1")]
        cands = lc.exchange_candidates(recs)
        self.assertIn("CQZ", cands)
        self.assertNotIn("BAND", cands)
        self.assertNotIn("APP_N1MM_X", cands)

    def test_detect_priority(self):
        recs = [qso(f"W{i}AW", STATE="CA", RST_RCVD="599") for i in range(5)]
        # STATE outranks RST_RCVD in the priority list.
        self.assertEqual(lc.detect_exchange_field(recs), "STATE")


# --------------------------------------------------------------------------
# Exchange consistency / busts
# --------------------------------------------------------------------------
class TestExchangeCheck(unittest.TestCase):
    def test_fixed_contest_flags_outlier(self):
        # 90%+ send zone "3"; one QSO with "8" is a bust.
        recs = [qso(f"W{i}AW", time=f"00{i:02d}00", CQZ="3") for i in range(9)]
        recs.append(qso("K9XYZ", time="001000", CQZ="8"))
        res = lc.analyze(recs, exchange_field="CQZ")
        self.assertTrue(res["is_fixed"])
        self.assertEqual(res["per_record"][-1]["exch_bust"], True)
        self.assertEqual(res["bust_count"], 1)

    def test_per_station_inconsistency(self):
        # Same station, two bands, two different zones -> the minority is flagged.
        recs = [
            qso("DL1ABC", time="000000", BAND="20M", CQZ="14"),
            qso("DL1ABC", time="010000", BAND="15M", CQZ="14"),
            qso("DL1ABC", time="020000", BAND="10M", CQZ="99"),
        ]
        res = lc.analyze(recs, exchange_field="CQZ", force_exchange=True)
        busts = [p["exch_bust"] for p in res["per_record"]]
        self.assertEqual(busts, [False, False, True])

    def test_serial_field_not_applicable(self):
        # Near-unique serials shouldn't be cross-checked (no false busts).
        recs = [qso(f"W{i}AW", SRX=str(i)) for i in range(10)]
        res = lc.analyze(recs, exchange_field="SRX")
        self.assertTrue(res["is_serial"])
        self.assertFalse(res["exch_applicable"])
        self.assertEqual(res["bust_count"], 0)


# --------------------------------------------------------------------------
# Auto-fix rule
# --------------------------------------------------------------------------
class TestAutoFix(unittest.TestCase):
    def test_early_wrong_then_consistent(self):
        # KW logged once as 100 early, then KW twice -> fix the 100 to KW.
        recs = [
            qso("VK9XYZ", time="000000", BAND="20M", RX_PWR="100"),
            qso("VK9XYZ", time="010000", BAND="15M", RX_PWR="KW"),
            qso("VK9XYZ", time="020000", BAND="10M", RX_PWR="KW"),
        ]
        res = lc.analyze(recs, exchange_field="RX_PWR", force_exchange=True)
        self.assertEqual(len(res["fixes"]), 1)
        idx, old, new = res["fixes"][0]
        self.assertEqual((idx, old, new), (0, "100", "KW"))
        lc.apply_fixes(recs, "RX_PWR", res["fixes"])
        self.assertEqual(recs[0]["RX_PWR"], "KW")

    def test_no_fix_when_tied(self):
        recs = [
            qso("DL1ABC", time="000000", CQZ="14"),
            qso("DL1ABC", time="010000", CQZ="14"),
            qso("DL1ABC", time="020000", CQZ="99"),
            qso("DL1ABC", time="030000", CQZ="99"),
        ]
        res = lc.analyze(recs, exchange_field="CQZ", force_exchange=True)
        self.assertEqual(res["fixes"], [])      # 2 vs 2 -> ambiguous

    def test_no_fix_when_scattered(self):
        # Right value is not a contiguous trailing run -> don't auto-fix.
        recs = [
            qso("DL1ABC", time="000000", CQZ="14"),
            qso("DL1ABC", time="010000", CQZ="99"),
            qso("DL1ABC", time="020000", CQZ="14"),
        ]
        res = lc.analyze(recs, exchange_field="CQZ", force_exchange=True)
        self.assertEqual(res["fixes"], [])

    def test_fix_multiple_early(self):
        recs = [
            qso("ZL7AA", time="000000", RX_PWR="5"),
            qso("ZL7AA", time="010000", RX_PWR="5"),
            qso("ZL7AA", time="020000", RX_PWR="KW"),
            qso("ZL7AA", time="030000", RX_PWR="KW"),
            qso("ZL7AA", time="040000", RX_PWR="KW"),
        ]
        res = lc.analyze(recs, exchange_field="RX_PWR", force_exchange=True)
        # 3 KW (tail) > 2 fives -> both fives corrected.
        self.assertEqual(sorted(f[0] for f in res["fixes"]), [0, 1])
        lc.apply_fixes(recs, "RX_PWR", res["fixes"])
        self.assertTrue(all(r["RX_PWR"] == "KW" for r in recs))


if __name__ == "__main__":
    unittest.main(verbosity=2)
