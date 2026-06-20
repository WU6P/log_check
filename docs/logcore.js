// log_check core — browser/Node port of logcore.py.
//
// Pure logic: ADIF/Cabrillo parsing, ARRL-DXCC -> ITU country resolution with
// rarity, and the exchange-consistency analysis. No DOM here, so it runs the
// same in the browser (app.js) and under Node (test_logcore.mjs). Lookup tables
// are injected with init() because a static site can't read files at import
// time the way the Python module does.
//
// This is a deliberate re-implementation of logcore.py; the two must stay in
// step. test_logcore.mjs mirrors test_log_check.py to keep them honest.

// --- injected lookup state -------------------------------------------------
let DXCC = {};        // prefix -> {entity, cont, code, cq, itu, ...}
let ITU = {};         // 2-char series -> {country, cont, lat, lon}
let DXCC_CODE = {};   // entity name -> code
let ENTITY_REC = {};  // entity name -> full record (with cq/itu zones)
let RARE = {};        // code (string) -> rank

const _entityCache = new Map();
const _rareCache = new Map();
const _callCache = new Map();

export function init({ dxcc, itu, rare }) {
  DXCC = (dxcc && dxcc.lookup) || {};
  ITU = (itu && itu.lookup) || {};
  DXCC_CODE = {};
  ENTITY_REC = {};
  for (const e of (dxcc && dxcc.entities) || []) {
    DXCC_CODE[e.entity] = e.code;
    ENTITY_REC[e.entity] = e;
  }
  RARE = (rare && rare.rare) || {};
  _entityCache.clear();
  _rareCache.clear();
  _callCache.clear();
}

// ==========================================================================
// ADIF / Cabrillo parsing
// ==========================================================================

export function parseAdifRecords(text) {
  const eoh = /<EOH>/i.exec(text);
  let pos = eoh ? eoh.index + eoh[0].length : 0;
  const re = /<([A-Za-z0-9_]+)(?::(\d+))?(?::[A-Za-z])?>/g;
  const records = [];
  let current = {};
  while (true) {
    re.lastIndex = pos;
    const m = re.exec(text);
    if (!m) break;
    const name = m[1].toUpperCase();
    const length = m[2];
    pos = re.lastIndex;
    if (name === "EOR") {
      if (Object.keys(current).length) { records.push(current); current = {}; }
      continue;
    }
    if (name === "EOH") continue;
    if (length !== undefined) {
      const ln = parseInt(length, 10);
      current[name] = text.slice(pos, pos + ln);
      pos += ln;
    } else {
      current[name] = "";
    }
  }
  if (Object.keys(current).length) records.push(current);
  return records;
}

export function serializeQso(qso) {
  const parts = [];
  for (const [key, raw] of Object.entries(qso)) {
    if (key.startsWith("_")) continue;
    const val = raw == null ? "" : String(raw);
    parts.push(`<${key.toUpperCase()}:${val.length}>${val}`);
  }
  parts.push("<EOR>");
  return parts.join(" ");
}

export function serializeAdif(records, headerComment = "log_check export") {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  const stamp = `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())} ` +
                `${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
  const head = `${headerComment}\n<ADIF_VER:5>3.1.0 <PROGRAMID:9>log_check ` +
               `<CREATED_TIMESTAMP:15>${stamp} <EOH>\n`;
  return head + records.map(serializeQso).join("\n") + "\n";
}

// UTC timestamp (ms) for ordering, or null if date/time missing/invalid.
export function qsoDatetime(qso) {
  const d = ((qso.QSO_DATE || "") + "").trim();
  let t = ((qso.TIME_ON || "") + "").trim();
  if (d.length !== 8) return null;
  t = (t + "000000").slice(0, 6);
  const yr = +d.slice(0, 4), mo = +d.slice(4, 6), da = +d.slice(6, 8);
  const hh = +t.slice(0, 2), mi = +t.slice(2, 4), ss = +t.slice(4, 6);
  if ([yr, mo, da, hh, mi, ss].some(Number.isNaN)) return null;
  if (mo < 1 || mo > 12 || da < 1 || da > 31 || hh > 23 || mi > 59 || ss > 59) return null;
  const dt = Date.UTC(yr, mo - 1, da, hh, mi, ss);
  return Number.isNaN(dt) ? null : dt;
}

// --- Cabrillo --------------------------------------------------------------
const BAND_EDGES = [
  [1800, 2000, "160M"], [3500, 4000, "80M"], [5250, 5450, "60M"],
  [7000, 7300, "40M"], [10100, 10150, "30M"], [14000, 14350, "20M"],
  [18068, 18168, "17M"], [21000, 21450, "15M"], [24890, 24990, "12M"],
  [28000, 29700, "10M"], [50000, 54000, "6M"], [70000, 71000, "4M"],
  [144000, 148000, "2M"], [420000, 450000, "70CM"],
];
const CAB_MODE = { CW: "CW", PH: "SSB", SSB: "SSB", RY: "RTTY", RTTY: "RTTY",
                   FM: "FM", DG: "DATA", DI: "DATA" };

function khzToBand(khz) {
  const f = parseFloat(khz);
  if (Number.isNaN(f)) return "";
  for (const [lo, hi, name] of BAND_EDGES) if (f >= lo && f <= hi) return name;
  return "";
}

function isCallsign(tok) {
  const t = (tok || "").toUpperCase();
  return t.length >= 3 && t.length <= 12 && /[A-Z]/.test(t) && /[0-9]/.test(t) &&
         /^[A-Z0-9/]+$/.test(t);
}

function cabrilloSplit(body) {
  if (body.length < 7) return [null, []];
  let idx = 5 + Math.floor((body.length - 6) / 2);
  let cand = idx < body.length ? body[idx] : null;
  if (!(cand && isCallsign(cand))) {
    const shaped = body.slice(4).filter(isCallsign);
    cand = shaped.length >= 2 ? shaped[1] : cand;
    if (cand == null) return [cand, []];
    idx = body.indexOf(cand);
    if (idx < 0) return [cand, []];
  }
  return [cand, body.slice(idx + 1)];
}

export function parseCabrilloRecords(text) {
  const records = [];
  for (const line of text.split(/\r?\n/)) {
    const s = line.trim();
    if (!s || !s.toUpperCase().startsWith("QSO:")) continue;
    const body = s.split(/\s+/).slice(1);
    if (body.length < 6) continue;
    const freq = body[0], mode = body[1].toUpperCase(), d = body[2], t = body[3];
    const [call, rcvd] = cabrilloSplit(body);
    if (!call) continue;
    const ds = d.replace(/-/g, "");
    if (ds.length !== 8 || !(t.length === 4 || t.length === 6)) continue;
    const rst = rcvd.length && /^\d{2,3}$/.test(rcvd[0]) ? rcvd[0] : "";
    const exch = rst ? rcvd.slice(1) : rcvd;
    records.push({
      CALL: call.toUpperCase(), QSO_DATE: ds,
      TIME_ON: t.length === 4 ? t + "00" : t,
      BAND: khzToBand(freq), FREQ: freq, MODE: CAB_MODE[mode] || mode,
      RST_RCVD: rst, SRX_STRING: exch.join(" "),
    });
  }
  return records;
}

export function recordsFromText(text) {
  if (/<EOH>/i.test(text) || /<CALL/i.test(text)) return parseAdifRecords(text);
  if (/^\s*QSO:/im.test(text) || text.toUpperCase().includes("START-OF-LOG"))
    return parseCabrilloRecords(text);
  const recs = parseAdifRecords(text);
  return recs.length ? recs : parseCabrilloRecords(text);
}

// ==========================================================================
// DXCC / ITU resolution + rarity
// ==========================================================================

const SUFFIXES = new Set(["P", "M", "MM", "AM", "QRP", "A", "B"]);

function callCores(call) {
  call = (call || "").toUpperCase().trim();
  if (!call.includes("/")) return call ? [call] : [];
  const parts = call.split("/").filter((p) => p && !SUFFIXES.has(p) && /[A-Z]/.test(p));
  parts.sort((a, b) => a.length - b.length);
  return parts.length ? parts : [call.replace(/\//g, "")];
}

function leadingAlpha(head) {
  let i = 0;
  while (i < head.length && /[A-Z]/.test(head[i])) i++;
  return i;
}

function lookupHead(core) {
  if (core in DXCC) return [DXCC[core], core, core];   // full-call key (KH7K=Kure)
  const m = /^([A-Z0-9]+?\d)/.exec(core);
  const head = m ? m[1] : core;
  for (let n = head.length; n >= 1; n--) {
    const key = head.slice(0, n);
    if (key in DXCC) return [DXCC[key], key, head];
  }
  return [null, null, null];
}

// Curated fixes for coarse dxcc.json prefixes — call-area splits the table maps
// to the parent entity (and hence wrong zone). See logcore.py for the rationale.
const PREFIX_OVERRIDES = [
  [/^KP[34]/, "Puerto Rico"],
  [/^KP2/, "Virgin Is."],
  [/^[AKNW]H6/, "Hawaii"],
  [/^[AKNW]H7(?!K)/, "Hawaii"],            // KH7K = rare Kure, leave it
  [/^[AKNW]H2/, "Guam"],
  [/^[AKNW]H0/, "Mariana Is."],
  [/^[AKNW]L\d/, "Alaska"],
  [/^E[A-H]8/, "Canary Is."],
  [/^E[A-H]9/, "Ceuta & Melilla"],
  [/^(?:R[A-Z]?|U[A-I])[890]/, "Asiatic Russia"],
  [/^(?:R[A-Z]?|U[A-I])2F/, "Kaliningrad"],
  [/^R1(?!F)/, "European Russia"],
  [/^(?:R[A-Z]?|U[A-I])[2-7]/, "European Russia"],
];

function overrideEntity(call) {
  for (const core of callCores(call))
    for (const [rx, ent] of PREFIX_OVERRIDES) if (rx.test(core)) return ent;
  return "";
}

function dxccMatch(call) {
  const ent = overrideEntity(call);
  if (ent) return [ENTITY_REC[ent] || { entity: ent }, true];
  for (const core of callCores(call)) {
    const [rec, key, head] = lookupHead(core);
    if (rec) return [rec, key.length >= leadingAlpha(head)];
  }
  return [null, false];
}

function resolveItu(call) {
  for (const core of callCores(call)) {
    const rec = ITU[core.slice(0, 2)];
    if (rec && rec.cont) return rec;
  }
  return null;
}

export function entityOf(call) {
  if (_entityCache.has(call)) return _entityCache.get(call);
  let out;
  const [rec, confident] = dxccMatch(call);
  if (rec && confident) {
    out = rec.entity || "";
  } else {
    const irec = resolveItu(call);
    out = irec && irec.country ? irec.country : (rec ? rec.entity || "" : "");
  }
  _entityCache.set(call, out);
  return out;
}

export function rareRank(call) {
  if (_rareCache.has(call)) return _rareCache.get(call);
  let out = null;
  const [rec, confident] = dxccMatch(call);
  if (rec && confident) {
    const code = DXCC_CODE[rec.entity];
    if (code != null && String(code) in RARE) out = parseInt(RARE[String(code)], 10);
  }
  _rareCache.set(call, out);
  return out;
}

// ==========================================================================
// Zone vs. entity  /  callsign plausibility  /  near-dupe (UBN)
// ==========================================================================

function parseZones(spec) {
  if (!spec) return null;
  const out = new Set();
  for (let tok of String(spec).split(",")) {
    tok = tok.trim();
    const m = /^(\d+)-(\d+)$/.exec(tok);
    if (m) {
      const a = +m[1], b = +m[2];
      for (let z = Math.min(a, b); z <= Math.max(a, b); z++) out.add(z);
    } else if (/^\d+$/.test(tok)) {
      out.add(+tok);
    }
  }
  return out.size ? out : null;
}

export function expectedZones(call, kind) {
  const [rec, confident] = dxccMatch(call);
  if (!(rec && confident)) return null;
  return parseZones(rec[kind]);
}

export function zoneProblem(qso) {
  const call = qso.CALL || "";
  for (const [field, kind] of [["CQZ", "cq"], ["ITUZ", "itu"]]) {
    const v = ((qso[field] || "") + "").trim();
    if (!(v && /^\d+$/.test(v))) continue;
    const exp = expectedZones(call, kind);
    if (exp && !exp.has(+v)) return { field, logged: +v, exp };
    return null;
  }
  return null;
}

export function callProblem(call) {
  if (_callCache.has(call)) return _callCache.get(call);
  const c = (call || "").toUpperCase().trim();
  let out;
  if (!(c.length >= 3 && c.length <= 15 && /^[A-Z0-9/]+$/.test(c)
        && /[A-Z]/.test(c) && /[0-9]/.test(c))) out = "malformed";
  else out = entityOf(c) ? "" : "unresolved";
  _callCache.set(call, out);
  return out;
}

function suffixSplit(call) {
  let last = -1;
  for (let i = 0; i < call.length; i++) if (/[0-9]/.test(call[i])) last = i;
  if (last < 0 || last === call.length - 1) return null;
  return [call.slice(0, last + 1), call.slice(last + 1)];
}

export function nearDupes(records, freqMin = 3, suffixMin = 3) {
  const counts = new Map();
  for (const r of records) {
    const c = ((r.CALL || "") + "").toUpperCase().trim();
    if (c) counts.set(c, (counts.get(c) || 0) + 1);
  }
  const byPrefix = new Map();
  const anchors = [...counts.entries()].filter(([, n]) => n >= freqMin)
    .sort((a, b) => b[1] - a[1]).map((e) => e[0]);
  for (const c of anchors) {
    const sp = suffixSplit(c);
    if (sp && sp[1].length >= suffixMin) {
      if (!byPrefix.has(sp[0])) byPrefix.set(sp[0], []);
      byPrefix.get(sp[0]).push([c, sp[1]]);
    }
  }
  const out = new Map();
  for (const [call, n] of counts) {
    if (n !== 1) continue;
    const sp = suffixSplit(call);
    if (!sp || sp[1].length < suffixMin) continue;
    const [pre, suf] = sp;
    for (const [anchor, asuf] of byPrefix.get(pre) || []) {
      if (asuf.length === suf.length) {
        let d = 0;
        for (let i = 0; i < suf.length; i++) if (suf[i] !== asuf[i]) d++;
        if (d === 1) { out.set(call, anchor); break; }
      }
    }
  }
  return out;
}

// ==========================================================================
// Exchange field analysis
// ==========================================================================

const NON_EXCHANGE = new Set([
  "CALL", "QSO_DATE", "QSO_DATE_OFF", "TIME_ON", "TIME_OFF", "BAND", "BAND_RX",
  "FREQ", "FREQ_RX", "MODE", "SUBMODE", "RST_SENT", "STX", "STX_STRING",
  "STATION_CALLSIGN", "OPERATOR", "OWNER_CALLSIGN", "MY_GRIDSQUARE",
  "CONTEST_ID", "PFX", "QSL_RCVD", "QSL_SENT", "ID",
]);

const EXCHANGE_PRIORITY = [
  "SRX_STRING", "SRX", "RX_PWR", "STATE", "ARRL_SECT", "VE_PROV", "WPX_PREFIX",
  "CQZ", "ITUZ", "GRIDSQUARE", "CLASS", "PRECEDENCE", "CHECK", "NAME", "AGE",
  "RST_RCVD",
];

function val(qso, field) {
  return ((qso[field] || "") + "").trim();
}

export function exchangeCandidates(records) {
  const counts = new Map();
  for (const r of records)
    for (const k of Object.keys(r)) {
      if (k.startsWith("_") || k.startsWith("APP_") || NON_EXCHANGE.has(k)) continue;
      if (((r[k] || "") + "").trim()) counts.set(k, (counts.get(k) || 0) + 1);
    }
  return [...counts.entries()].sort((a, b) => b[1] - a[1]).map((e) => e[0]);
}

export function detectExchangeField(records) {
  if (!records.length) return "";
  const cands = exchangeCandidates(records);
  const present = new Set(cands);
  const n = records.length;
  for (const field of EXCHANGE_PRIORITY) {
    if (present.has(field)) {
      let filled = 0;
      for (const r of records) if (val(r, field)) filled++;
      if (filled >= 0.4 * n) return field;
    }
  }
  return cands.length ? cands[0] : "";
}

function distinctRatio(records, field) {
  const vals = [];
  for (const r of records) { const v = val(r, field); if (v) vals.push(v); }
  if (!vals.length) return 1.0;
  return new Set(vals).size / vals.length;
}

// Most common value in an array (ties -> first encountered, like Counter).
function topValue(arr) {
  const c = new Map();
  for (const v of arr) c.set(v, (c.get(v) || 0) + 1);
  let best = null, bn = -1;
  for (const [v, n] of c) if (n > bn) { bn = n; best = v; }
  return [best, bn];
}

export function analyze(records, exchangeField = null, opts = {}) {
  const { fixedThreshold = 0.9, serialThreshold = 0.5, forceExchange = false } = opts;
  const field = exchangeField != null ? exchangeField : detectExchangeField(records);

  const per = records.map((r) => ({
    entity: entityOf(r.CALL || ""), rank: rareRank(r.CALL || ""),
    exch: field ? val(r, field) : "", exch_bust: false,
    zone_bust: false, zone_exp: "", zone_logged: "",
    call_bad: callProblem(r.CALL || ""), dupe_of: "",
  }));
  const rareCount = per.filter((p) => p.rank != null).length;

  // zone vs. entity, and UBN near-dupe
  records.forEach((r, i) => {
    const zp = zoneProblem(r);
    if (zp) {
      per[i].zone_bust = true;
      per[i].zone_logged = `${zp.field}=${zp.logged}`;
      per[i].zone_exp = [...zp.exp].sort((a, b) => a - b).join(",");
    }
  });
  const dupes = nearDupes(records);
  records.forEach((r, i) => {
    const sug = dupes.get(((r.CALL || "") + "").toUpperCase().trim());
    if (sug) per[i].dupe_of = sug;
  });

  let majVal = "", majShare = 0, isFixed = false, isSerial = false, applicable = false;
  const fixes = [];
  if (field) {
    const nonEmpty = per.map((p) => p.exch).filter((v) => v);
    if (nonEmpty.length) {
      const [v, n] = topValue(nonEmpty);
      majVal = v; majShare = n / nonEmpty.length;
    }
    isFixed = majShare >= fixedThreshold;
    isSerial = distinctRatio(records, field) > serialThreshold;
    applicable = forceExchange || isFixed || !isSerial;

    if (applicable) {
      const groups = new Map();
      records.forEach((r, i) => {
        const k = ((r.CALL || "") + "").toUpperCase().trim();
        if (!groups.has(k)) groups.set(k, []);
        groups.get(k).push(i);
      });
      for (const idxs of groups.values()) {
        idxs.sort((a, b) => {
          const da = qsoDatetime(records[a]), db = qsoDatetime(records[b]);
          const va = da == null ? -Infinity : da, vb = db == null ? -Infinity : db;
          return va - vb || a - b;
        });
        const vals = idxs.map((i) => per[i].exch);
        const nonempty = vals.filter((v) => v);
        if (new Set(nonempty).size > 1 || (vals.includes("") && nonempty.length)) {
          const [gm] = topValue(nonempty);
          for (const i of idxs) if (per[i].exch !== gm) per[i].exch_bust = true;
        }
        if (isFixed)
          for (const i of idxs)
            if (per[i].exch && per[i].exch !== majVal) per[i].exch_bust = true;
        for (const f of groupFixes(idxs, vals)) fixes.push(f);
      }
    }
  }

  return {
    per_record: per, exchange_field: field, majority_value: majVal,
    majority_share: majShare, is_fixed: isFixed, is_serial: isSerial,
    exch_applicable: applicable, rare_count: rareCount,
    bust_count: per.filter((p) => p.exch_bust).length, fixes,
    zone_count: per.filter((p) => p.zone_bust).length,
    callbad_count: per.filter((p) => p.call_bad).length,
    dupe_count: per.filter((p) => p.dupe_of).length,
  };
}

// One callsign group's auto-fix proposals (see logcore._group_fixes).
function groupFixes(idxs, vals) {
  const n = vals.length;
  if (n < 2) return [];
  const x = vals[n - 1];
  if (!x) return [];
  let suffix = 0;
  for (let i = n - 1; i >= 0; i--) { if (vals[i] === x) suffix++; else break; }
  if (vals.filter((v) => v === x).length !== suffix) return [];
  const nOther = n - suffix;
  if (nOther === 0 || suffix <= nOther) return [];
  const out = [];
  for (let i = 0; i < nOther; i++) if (vals[i] !== x) out.push([idxs[i], vals[i], x]);
  return out;
}

export function applyFixes(records, field, fixes) {
  let applied = 0;
  for (const [i, , nw] of fixes)
    if (i >= 0 && i < records.length) { records[i][field] = nw; applied++; }
  return applied;
}
