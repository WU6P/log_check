// Node test suite for logcore.js — mirrors test_log_check.py so the JS port
// and the Python original stay in step. No dependencies:
//     node test_logcore.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import * as lc from "./logcore.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const j = (f) => JSON.parse(readFileSync(join(HERE, f), "utf-8"));
lc.init({ dxcc: j("dxcc.json"), itu: j("itu.json"), rare: j("rare.json") });

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log("ok   " + name); }
  catch (e) { failed++; console.log("FAIL " + name + "\n     " + e.message); }
}

const qso = (call, date = "20260101", time = "000000", extra = {}) =>
  ({ CALL: call, QSO_DATE: date, TIME_ON: time, ...extra });

// --- parsing ---------------------------------------------------------------
test("adif basic", () => {
  const r = lc.parseAdifRecords(
    "x <EOH> <CALL:4>W1AW <BAND:3>20M <MODE:2>CW <EOR>" +
    " <CALL:5>K3EST <BAND:3>15M <MODE:3>SSB <EOR>");
  assert.equal(r.length, 2);
  assert.equal(r[0].CALL, "W1AW");
  assert.equal(r[1].MODE, "SSB");
});

test("adif tag inside value", () => {
  const r = lc.parseAdifRecords("<EOH> <CALL:4>W1AW <COMMENT:10><EOR> hack <EOR>");
  assert.equal(r.length, 1);
  assert.equal(r[0].COMMENT, "<EOR> hack");
});

test("cabrillo basic", () => {
  const text = "START-OF-LOG: 3.0\n" +
    "QSO: 14025 CW 2026-01-01 0000 N6RO 599 25 JJ0VNR 599 KW\n" +
    "QSO: 21025 CW 2026-01-01 0001 N6RO 599 25 BD3TE 599 100\n" +
    "X-QSO: 21025 CW 2026-01-01 0002 N6RO 599 25 DUPE 599 100\n";
  const r = lc.parseCabrilloRecords(text);
  assert.equal(r.length, 2);
  assert.equal(r[0].CALL, "JJ0VNR");
  assert.equal(r[0].BAND, "20M");
  assert.equal(r[0].SRX_STRING, "KW");
  assert.equal(r[1].SRX_STRING, "100");
});

test("records_from_text dispatch", () => {
  assert.equal(lc.recordsFromText("<EOH> <CALL:4>W1AW <EOR>").length, 1);
  assert.equal(
    lc.recordsFromText("QSO: 14025 CW 2026-01-01 0000 N6RO 599 1 W1AW 599 2").length, 1);
});

test("serialize roundtrip", () => {
  const recs = lc.parseAdifRecords("<EOH> <CALL:4>W1AW <BAND:3>20M <EOR>");
  recs[0]._internal = "ignore me";
  const again = lc.parseAdifRecords(lc.serializeAdif(recs));
  assert.equal(again[0].CALL, "W1AW");
  assert.equal(again[0].BAND, "20M");
  assert.ok(!("_INTERNAL" in again[0]));
});

test("detectFormat", () => {
  assert.equal(lc.detectFormat("<EOH> <CALL:4>W1AW <EOR>"), "adif");
  assert.equal(
    lc.detectFormat("START-OF-LOG: 3.0\nQSO: 14025 CW 2026-01-01 0000 " +
                    "N6RO 599 25 W1AW 599 1"), "cabrillo");
});

test("serializeCabrillo preserves file, applies edits, drops deletes", () => {
  const text =
    "START-OF-LOG: 3.0\n" +
    "CONTEST: CQ-WW-CW\n" +
    "QSO: 14025 CW 2026-01-01 0000 N6RO 599 25 JJ0VNR 599 KW\n" +
    "QSO: 21025 CW 2026-01-01 0001 N6RO 599 25 BD3TE 599 100\n" +
    "X-QSO: 21025 CW 2026-01-01 0002 N6RO 599 25 DUPE 599 100\n" +
    "END-OF-LOG:\n";
  const recs = lc.recordsFromText(text);
  recs[0].CALL = "JA0VNR";       // fix a busted call
  recs.splice(1, 1);             // delete the second QSO
  const out = lc.serializeCabrillo(recs, text);
  assert.ok(out.includes("CONTEST: CQ-WW-CW"));   // header verbatim
  assert.ok(out.includes("END-OF-LOG:"));         // footer verbatim
  assert.ok(out.includes("X-QSO: 21025 CW 2026-01-01 0002")); // X-QSO kept
  assert.ok(out.includes("N6RO 599 25 JA0VNR"));  // sent side survives, call fixed
  assert.ok(!out.includes("JJ0VNR"));
  assert.ok(!out.includes("BD3TE"));              // deleted line dropped
});

test("serializeCabrillo no-edit save is byte-identical", () => {
  // padded columns + trailing spaces + CRLF must survive an untouched save
  const text =
    "START-OF-LOG: 3.0\r\n" +
    "CALLSIGN: K3EST\r\n" +
    "QSO:   14036 CW 2026-06-20 0000 K3EST    599 77   JH4UYB   599 61   \r\n" +
    "QSO:   21025 CW 2026-06-20 0001 K3EST    599 77   JA8RUZ   599 67   \r\n" +
    "END-OF-LOG:\r\n";
  const recs = lc.recordsFromText(text);
  assert.equal(lc.serializeCabrillo(recs, text), text);
});

test("serializeCabrillo edit keeps column alignment", () => {
  const line = "QSO:   14036 CW 2026-06-20 0000 K3EST         599 77     " +
               "JH4UYB        599 61       ";
  const text = "START-OF-LOG: 3.0\r\n" + line + "\r\n" +
               line.replace("JH4UYB", "JA8RUZ") + "\r\nEND-OF-LOG:\r\n";
  const recs = lc.recordsFromText(text);
  recs[0].SRX_STRING = "71";                 // same-length exchange fix
  const out = lc.serializeCabrillo(recs, text);
  const edited = out.split(/\r?\n/).find((l) => l.includes("JH4UYB"));
  assert.ok(edited.includes("599 71"));
  assert.equal(line.indexOf("JH4UYB"), edited.indexOf("JH4UYB")); // column kept
  assert.equal(edited.length, line.length);                       // width kept
});

test("serializeCabrillo shorter edit pads to keep width", () => {
  const line = "QSO:   14008 CW 2026-06-20 0416 K3EST         599 77     " +
               "JA4MLR        599 2672     ";
  const recs = lc.recordsFromText(line + "\r\n");
  recs[0].SRX_STRING = "72";                  // 2672 -> 72 (shorter)
  const out = lc.serializeCabrillo(recs, line + "\r\n").replace(/\r?\n$/, "");
  assert.ok(out.includes("599 72"));
  assert.equal(out.length, line.length);      // trailing pad absorbs it
});

test("serializeCabrillo applies exchange edit", () => {
  const text = "QSO: 14025 CW 2026-01-01 0000 N6RO 599 25 W1AW 599 5\n";
  const recs = lc.recordsFromText(text);
  recs[0].SRX_STRING = "3";
  const out = lc.serializeCabrillo(recs, text);
  assert.ok(out.includes("W1AW 599 3"));
  assert.ok(!out.includes("599 5"));
});

test("qsoDatetime", () => {
  assert.equal(lc.qsoDatetime({ QSO_DATE: "bad" }), null);
  const ts = lc.qsoDatetime({ QSO_DATE: "20260101", TIME_ON: "0102" });
  const d = new Date(ts);
  assert.equal(d.getUTCHours(), 1);
  assert.equal(d.getUTCMinutes(), 2);
});

// --- rare ------------------------------------------------------------------
test("entity resolution", () => {
  assert.equal(lc.entityOf("W1AW"), "United States of America");
  assert.equal(lc.entityOf("JJ0VNR"), "Japan");
});

test("rare flag", () => {
  assert.equal(lc.rareRank("P5DX"), 1);
  assert.equal(lc.rareRank("W1AW"), null);
});

test("rare slash call", () => {
  assert.equal(lc.rareRank("W1AW/M"), null);
});

test("multiletter prefix not swallowed", () => {
  assert.equal(lc.entityOf("TM6M"), "France");
  assert.equal(lc.rareRank("TM6M"), null);
  assert.notEqual(lc.rareRank("KP5/NP3VI"), null);
});

test("coarse prefix overrides", () => {
  assert.equal(lc.entityOf("KP4CC"), "Puerto Rico");
  assert.equal(lc.rareRank("KP4CC"), null);
  assert.equal(lc.entityOf("R1DX"), "European Russia");
  assert.equal(lc.rareRank("R1DX"), null);
  assert.equal(lc.rareRank("R1FJ"), 51);
  assert.equal(lc.rareRank("KP1AA"), 31);
});

test("analyze counts rare", () => {
  const res = lc.analyze([qso("W1AW"), qso("P5DX"), qso("K3EST")], "");
  assert.equal(res.rare_count, 1);
  assert.equal(res.per_record[1].rank, 1);
  assert.equal(res.per_record[0].rank, null);
});

// --- call-area split overrides ---------------------------------------------
test("call-area splits resolve correctly", () => {
  assert.equal(lc.entityOf("AH6AA"), "Hawaii");
  assert.equal(lc.entityOf("AH2R"), "Guam");
  assert.equal(lc.entityOf("KL7AA"), "Alaska");
  assert.equal(lc.entityOf("ED8X"), "Canary Is.");
  assert.equal(lc.entityOf("UA0D"), "Asiatic Russia");
  assert.equal(lc.entityOf("R5AJ"), "European Russia");
  assert.equal(lc.entityOf("R2FK"), "Kaliningrad");
  assert.equal(lc.entityOf("R2QA"), "European Russia");
  assert.equal(lc.rareRank("KH7K"), 7);          // full-call key, Kure restored
  assert.equal(lc.rareRank("R1FJ"), 51);         // Franz Josef still rare
});

// --- zone vs. entity -------------------------------------------------------
test("zone parse", () => {
  assert.deepEqual([...lc.expectedZones("JA1ABC", "cq")], [25]);
  assert.equal(lc.expectedZones("UA0DX", "cq"), null);   // Asiatic Russia marker
});
test("zone mismatch flagged, giants safe", () => {
  assert.equal(lc.zoneProblem({ CALL: "JA1ABC", CQZ: "5" }).logged, 5);
  assert.equal(lc.zoneProblem({ CALL: "JA1ABC", CQZ: "25" }), null);
  assert.equal(lc.zoneProblem({ CALL: "W1AW", CQZ: "5" }), null);    // US 3,4,5
  assert.equal(lc.zoneProblem({ CALL: "W1AW", CQZ: "8" }).logged, 8);
});
test("analyze zone count", () => {
  const res = lc.analyze([qso("JA1ABC", "20260101", "000000", { CQZ: "25" }),
                          qso("JA2DEF", "20260101", "000000", { CQZ: "5" })], "");
  assert.equal(res.zone_count, 1);
  assert.ok(res.per_record[1].zone_bust);
  assert.equal(res.per_record[1].zone_exp, "25");
});

// --- callsign plausibility -------------------------------------------------
test("call problem", () => {
  assert.equal(lc.callProblem("W1AW"), "");
  assert.equal(lc.callProblem("ABCDEF"), "malformed");
  assert.equal(lc.callProblem("12345"), "malformed");
  assert.equal(lc.callProblem("0Q1QQ"), "unresolved");
  const res = lc.analyze([qso("W1AW"), qso("0Q1QQ")], "");
  assert.equal(res.callbad_count, 1);
  assert.equal(res.per_record[1].call_bad, "unresolved");
});

// --- near-dupe (UBN) -------------------------------------------------------
test("suffix split", () => {
  assert.deepEqual(lc.nearDupes(Array(3).fill(qso("K3EST")).concat([qso("K3FST")])),
                   new Map([["K3FST", "K3EST"]]));
});
test("near-dupe ignores number/prefix and short suffixes", () => {
  assert.equal(lc.nearDupes(Array(3).fill(qso("IO3T")).concat([qso("IO8T")])).size, 0);
  assert.equal(lc.nearDupes(Array(3).fill(qso("S53A")).concat([qso("S53D")])).size, 0);
  assert.equal(lc.nearDupes(Array(2).fill(qso("K3EST")).concat([qso("K3FST")])).size, 0);
});
test("analyze dupe flag", () => {
  const res = lc.analyze(Array(3).fill(qso("K3EST")).concat([qso("K3FST")]), "");
  assert.equal(res.dupe_count, 1);
  assert.equal(res.per_record[3].dupe_of, "K3EST");
});

// --- exchange detection ----------------------------------------------------
test("candidates exclude universal and app", () => {
  const cands = lc.exchangeCandidates([qso("W1AW", "20260101", "000000",
    { BAND: "20M", CQZ: "5", APP_N1MM_X: "1" })]);
  assert.ok(cands.includes("CQZ"));
  assert.ok(!cands.includes("BAND"));
  assert.ok(!cands.includes("APP_N1MM_X"));
});

test("detect priority", () => {
  const recs = [];
  for (let i = 0; i < 5; i++) recs.push(qso(`W${i}AW`, "20260101", "000000",
    { STATE: "CA", RST_RCVD: "599" }));
  assert.equal(lc.detectExchangeField(recs), "STATE");
});

// --- exchange busts --------------------------------------------------------
test("fixed contest flags outlier", () => {
  const recs = [];
  for (let i = 0; i < 9; i++) recs.push(qso(`W${i}AW`, "20260101",
    `00${String(i).padStart(2, "0")}00`, { CQZ: "3" }));
  recs.push(qso("K9XYZ", "20260101", "001000", { CQZ: "8" }));
  const res = lc.analyze(recs, "CQZ");
  assert.ok(res.is_fixed);
  assert.equal(res.per_record[res.per_record.length - 1].exch_bust, true);
  assert.equal(res.bust_count, 1);
});

test("per station inconsistency", () => {
  const recs = [
    qso("DL1ABC", "20260101", "000000", { BAND: "20M", CQZ: "14" }),
    qso("DL1ABC", "20260101", "010000", { BAND: "15M", CQZ: "14" }),
    qso("DL1ABC", "20260101", "020000", { BAND: "10M", CQZ: "99" }),
  ];
  const res = lc.analyze(recs, "CQZ", { forceExchange: true });
  assert.deepEqual(res.per_record.map((p) => p.exch_bust), [false, false, true]);
});

test("serial field not applicable", () => {
  const recs = [];
  for (let i = 0; i < 10; i++) recs.push(qso(`W${i}AW`, "20260101", "000000",
    { SRX: String(i) }));
  const res = lc.analyze(recs, "SRX");
  assert.ok(res.is_serial);
  assert.ok(!res.exch_applicable);
  assert.equal(res.bust_count, 0);
});

// --- auto-fix --------------------------------------------------------------
test("early wrong then consistent", () => {
  const recs = [
    qso("VK9XYZ", "20260101", "000000", { BAND: "20M", RX_PWR: "100" }),
    qso("VK9XYZ", "20260101", "010000", { BAND: "15M", RX_PWR: "KW" }),
    qso("VK9XYZ", "20260101", "020000", { BAND: "10M", RX_PWR: "KW" }),
  ];
  const res = lc.analyze(recs, "RX_PWR", { forceExchange: true });
  assert.equal(res.fixes.length, 1);
  assert.deepEqual(res.fixes[0], [0, "100", "KW"]);
  lc.applyFixes(recs, "RX_PWR", res.fixes);
  assert.equal(recs[0].RX_PWR, "KW");
});

test("two-QSO disagreement fixes the older", () => {
  const recs = [
    qso("DL1ABC", "20260101", "000000", { CQZ: "14" }),   // older
    qso("DL1ABC", "20260101", "010000", { CQZ: "99" }),   // newer
  ];
  assert.deepEqual(lc.analyze(recs, "CQZ", { forceExchange: true }).fixes,
                   [[0, "14", "99"]]);                      // change the older
});

test("no fix when tied", () => {
  const recs = [
    qso("DL1ABC", "20260101", "000000", { CQZ: "14" }),
    qso("DL1ABC", "20260101", "010000", { CQZ: "14" }),
    qso("DL1ABC", "20260101", "020000", { CQZ: "99" }),
    qso("DL1ABC", "20260101", "030000", { CQZ: "99" }),
  ];
  assert.deepEqual(lc.analyze(recs, "CQZ", { forceExchange: true }).fixes, []);
});

test("no fix when scattered", () => {
  const recs = [
    qso("DL1ABC", "20260101", "000000", { CQZ: "14" }),
    qso("DL1ABC", "20260101", "010000", { CQZ: "99" }),
    qso("DL1ABC", "20260101", "020000", { CQZ: "14" }),
  ];
  assert.deepEqual(lc.analyze(recs, "CQZ", { forceExchange: true }).fixes, []);
});

test("fix multiple early", () => {
  const recs = [
    qso("ZL7AA", "20260101", "000000", { RX_PWR: "5" }),
    qso("ZL7AA", "20260101", "010000", { RX_PWR: "5" }),
    qso("ZL7AA", "20260101", "020000", { RX_PWR: "KW" }),
    qso("ZL7AA", "20260101", "030000", { RX_PWR: "KW" }),
    qso("ZL7AA", "20260101", "040000", { RX_PWR: "KW" }),
  ];
  const res = lc.analyze(recs, "RX_PWR", { forceExchange: true });
  assert.deepEqual(res.fixes.map((f) => f[0]).sort(), [0, 1]);
  lc.applyFixes(recs, "RX_PWR", res.fixes);
  assert.ok(recs.every((r) => r.RX_PWR === "KW"));
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
