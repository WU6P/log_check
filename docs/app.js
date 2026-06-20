// log_check web UI — DOM glue around logcore.js. All logic lives in logcore;
// this file only loads the file, renders the table, and wires the buttons.
import * as lc from "./logcore.js";

// Column layout: key is the ADIF field for editable cells, "EXCH" is rebound
// to the chosen exchange field, the rest (leading "_") are computed.
const COLUMNS = [
  ["", "_SEL"], ["#", "_NUM"], ["Date", "QSO_DATE"], ["Time", "TIME_ON"],
  ["Call", "CALL"], ["Band", "BAND"], ["Mode", "MODE"], ["RST", "RST_RCVD"],
  ["Exchange", "EXCH"], ["Entity", "_ENTITY"], ["Rare#", "_RARE"], ["Flags", "_FLAGS"],
];
const EDITABLE = new Set(["QSO_DATE", "TIME_ON", "CALL", "BAND", "MODE", "RST_RCVD", "EXCH"]);

const $ = (id) => document.getElementById(id);
const el = {
  file: $("file"), field: $("field"), force: $("force"), save: $("save"),
  summary: $("summary"), head: $("head"), body: $("body"), empty: $("empty"),
  table: $("table"), edit: $("edit"), del: $("del"),
  modal: $("modal"), mTitle: $("modal-title"), mFields: $("modal-fields"),
  newKey: $("new-key"), newVal: $("new-val"), mOk: $("modal-ok"), mCancel: $("modal-cancel"),
  review: $("review"), rv: $("review-win"), rvBody: $("rv-body"), rvPos: $("rv-pos"),
  rvPrev: $("rv-prev"), rvNext: $("rv-next"), rvClose: $("rv-close"),
  rvPrev2: $("rv-prev2"), rvNext2: $("rv-next2"),
};

let records = [];
let result = null;
let field = "";
let fileName = "log";
const selected = new Set();      // selected record indices

// Review-window state
let issues = [];                 // [{call, idxs, rank, entity, hasBust, fixes}]
let issueIdx = 0;
let reviewOpen = false;
let pendingCall = null;          // keep the review on this station across a re-analyze

// --- startup: load lookups -------------------------------------------------
(async function boot() {
  try {
    const [dxcc, itu, rare] = await Promise.all(
      ["dxcc.json", "itu.json", "rare.json"].map((f) => fetch(f).then((r) => r.json())));
    lc.init({ dxcc, itu, rare });
    el.summary.innerHTML =
      "Ready. Open an <code>.adi</code> / <code>.adif</code> / <code>.log</code> file.";
  } catch (e) {
    el.summary.textContent =
      "Could not load lookup data (dxcc/itu/rare.json). Serve this folder over HTTP. " + e;
  }
})();

// --- file open -------------------------------------------------------------
el.file.addEventListener("change", async (ev) => {
  const f = ev.target.files[0];
  if (!f) return;
  fileName = f.name.replace(/\.[^.]+$/, "");
  let text;
  try { text = await f.text(); }
  catch (e) { alert("Could not read file: " + e); return; }
  loadFromText(text, fileName);
  el.file.value = "";                 // allow re-opening the same file
});

function loadFromText(text, name) {
  const recs = lc.recordsFromText(text);
  if (!recs.length) { alert("No QSO records found in that file."); return false; }
  fileName = name;
  records = recs;
  selected.clear();
  populateFieldSelect();
  analyze();
  el.save.disabled = false;
  return true;
}
// Test hook (harmless in production): drive a load without a file picker.
window.__lcLoad = loadFromText;

function populateFieldSelect() {
  const cands = lc.exchangeCandidates(records);
  const def = lc.detectExchangeField(records);
  el.field.innerHTML = "";
  for (const name of ["(none)", ...cands]) {
    const o = document.createElement("option");
    o.value = name === "(none)" ? "" : name;
    o.textContent = name;
    el.field.appendChild(o);
  }
  field = def && cands.includes(def) ? def : "";
  el.field.value = field;
  el.field.disabled = false;
}

el.field.addEventListener("change", () => { field = el.field.value; analyze(); });
el.force.addEventListener("change", analyze);

// --- analysis + render -----------------------------------------------------
function analyze() {
  if (!records.length) return;
  result = lc.analyze(records, field, { forceExchange: el.force.checked });
  render();
  updateSummary();
  updateReview();
}

function render() {
  // header (rebind the Exchange column label to the chosen field)
  el.head.innerHTML = "";
  for (const [label, key] of COLUMNS) {
    const th = document.createElement("th");
    th.textContent = key === "EXCH" ? `Exch: ${result.exchange_field || "—"}` : label;
    el.head.appendChild(th);
  }

  const scroll = el.table.parentElement.scrollTop;
  const per = result.per_record;
  const frag = document.createDocumentFragment();
  for (let i = 0; i < records.length; i++) {
    const info = per[i];
    const tr = document.createElement("tr");
    tr.dataset.i = i;
    const rare = info.rank != null, bust = info.exch_bust;
    tr.className = (rare && bust) ? "both" : rare ? "rare" : bust ? "exch" : "";
    if (selected.has(i)) tr.classList.add("sel");
    for (const [, key] of COLUMNS) tr.appendChild(cell(i, records[i], info, key));
    frag.appendChild(tr);
  }
  el.body.replaceChildren(frag);
  el.empty.style.display = records.length ? "none" : "";
  el.table.parentElement.scrollTop = scroll;
  updateButtons();
}

function cell(i, qso, info, key) {
  const td = document.createElement("td");
  if (key === "_SEL") {
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = selected.has(i); cb.dataset.sel = i;
    td.appendChild(cb);
    return td;
  }
  if (key === "_NUM") { td.className = "num"; td.dataset.num = i; td.textContent = i + 1; return td; }
  if (key === "_ENTITY") { td.textContent = info.entity; return td; }
  if (key === "_RARE") { td.textContent = info.rank == null ? "" : "#" + info.rank; return td; }
  if (key === "_FLAGS") {
    td.className = "flags";
    td.textContent = [info.rank != null ? "RARE" : "", info.exch_bust ? "EXCH" : ""]
      .filter(Boolean).join(" ");
    return td;
  }
  if (key === "EXCH") {
    td.textContent = info.exch;
    if (result.exchange_field) makeEditable(td, i, "EXCH");
    return td;
  }
  td.textContent = qso[key] || "";
  if (EDITABLE.has(key)) makeEditable(td, i, key);
  return td;
}

function makeEditable(td, i, key) {
  td.className = (td.className + " editable").trim();
  td.contentEditable = "true";
  td.spellcheck = false;
  td.dataset.edit = key;
  td.dataset.i = i;
}

// commit an inline edit when a cell loses focus
el.body.addEventListener("focusout", (ev) => {
  const td = ev.target.closest("td.editable");
  if (!td) return;
  const i = +td.dataset.i;
  const key = td.dataset.edit;
  const target = key === "EXCH" ? field : key;
  if (!target) return;
  const val = td.textContent.trim();
  if ((records[i][target] || "") === val) return;     // no change
  records[i][target] = val;
  analyze();
});

// checkbox + row-number clicks (event delegation)
el.body.addEventListener("change", (ev) => {
  const cb = ev.target.closest("input[data-sel]");
  if (!cb) return;
  const i = +cb.dataset.sel;
  cb.checked ? selected.add(i) : selected.delete(i);
  el.body.querySelector(`tr[data-i="${i}"]`)?.classList.toggle("sel", cb.checked);
  updateButtons();
});
el.body.addEventListener("dblclick", (ev) => {
  const num = ev.target.closest("td.num");
  if (num) openEditor(+num.dataset.num);
});

function updateButtons() {
  el.del.disabled = selected.size === 0;
  el.edit.disabled = selected.size !== 1;
}

function updateSummary() {
  const r = result;
  const parts = [`<b>${records.length}</b> QSOs`, `<b>${r.rare_count}</b> rare DXCC`];
  if (r.exchange_field) {
    if (r.exch_applicable) {
      const pct = (r.majority_share * 100).toFixed(0);
      const fixed = r.is_fixed ? `FIXED at '${r.majority_value}' (${pct}%)`
                               : `top '${r.majority_value}' ${pct}%`;
      parts.push(`exchange '${r.exchange_field}' [${fixed}] — ` +
                 `<b>${r.bust_count}</b> busts, <b>${r.fixes.length}</b> auto-fixable`);
    } else {
      parts.push(`exchange '${r.exchange_field}' looks like serial numbers — ` +
                 `check skipped (tick ‘force check’ to run it)`);
    }
  } else {
    parts.push("no exchange field selected");
  }
  el.summary.innerHTML = parts.join(" &nbsp;|&nbsp; ");
}

// --- delete ----------------------------------------------------------------
el.del.addEventListener("click", () => {
  const rows = [...selected].sort((a, b) => a - b);
  if (!rows.length) return;
  const calls = rows.slice(0, 6).map((i) => records[i].CALL || "?").join(", ");
  const more = rows.length > 6 ? "…" : "";
  if (!confirm(`Delete ${rows.length} QSO(s)?\n\n${calls}${more}\n\n` +
               "This cannot be undone (until you reload the file).")) return;
  for (const i of rows.sort((a, b) => b - a)) records.splice(i, 1);
  selected.clear();
  analyze();
});

// --- full-field editor modal ----------------------------------------------
let editIndex = -1;
el.edit.addEventListener("click", () => {
  if (selected.size === 1) openEditor([...selected][0]);
});
function openEditor(i) {
  editIndex = i;
  const qso = records[i];
  el.mTitle.textContent = `Edit QSO — ${qso.CALL || "?"}`;
  el.mFields.innerHTML = "";
  for (const key of Object.keys(qso).filter((k) => !k.startsWith("_")).sort()) {
    const lab = document.createElement("label"); lab.textContent = key;
    const inp = document.createElement("input"); inp.value = qso[key] ?? ""; inp.dataset.k = key;
    el.mFields.append(lab, inp);
  }
  el.newKey.value = ""; el.newVal.value = "";
  el.modal.classList.remove("hidden");
}
el.mCancel.addEventListener("click", () => el.modal.classList.add("hidden"));
el.mOk.addEventListener("click", () => {
  const qso = records[editIndex];
  for (const inp of el.mFields.querySelectorAll("input")) qso[inp.dataset.k] = inp.value;
  const nk = el.newKey.value.trim().toUpperCase();
  if (nk) qso[nk] = el.newVal.value;
  el.modal.classList.add("hidden");
  if (reviewOpen) pendingCall = issues[issueIdx]?.call;
  analyze();
});
el.modal.addEventListener("click", (ev) => { if (ev.target === el.modal) el.modal.classList.add("hidden"); });

// --- save ------------------------------------------------------------------
el.save.addEventListener("click", () => {
  if (!records.length) return;
  const blob = new Blob([lc.serializeAdif(records)], { type: "text/plain" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = fileName + "_checked.adi";
  a.click();
  URL.revokeObjectURL(a.href);
});

// ==========================================================================
// Review window — step through issues one at a time. Exchange issues show the
// whole station group (every QSO with that callsign) so the inconsistency is
// visible at a glance, with the suggested fix and per-QSO edit/delete.
// ==========================================================================

// One issue per problem station: rare entity and/or an inconsistent exchange.
function buildIssues() {
  const per = result.per_record;
  const groups = new Map();                      // CALL -> [record index]
  records.forEach((r, i) => {
    const k = ((r.CALL || "") + "").toUpperCase().trim();
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(i);
  });
  const fixByIdx = new Map();
  for (const f of result.fixes) fixByIdx.set(f[0], f);

  const out = [];
  for (const [call, idxs0] of groups) {
    const idxs = [...idxs0].sort((a, b) => {
      const da = lc.qsoDatetime(records[a]), db = lc.qsoDatetime(records[b]);
      return (da == null ? -Infinity : da) - (db == null ? -Infinity : db) || a - b;
    });
    let rank = null, entity = "", hasBust = false;
    const fixes = [];
    for (const i of idxs) {
      const p = per[i];
      if (p.rank != null) { rank = p.rank; entity = p.entity; }
      if (p.exch_bust) hasBust = true;
      if (fixByIdx.has(i)) fixes.push(fixByIdx.get(i));
    }
    if (rank == null && !hasBust) continue;
    out.push({ call, idxs, rank, entity: entity || per[idxs[0]].entity, hasBust, fixes });
  }
  // rarest first, then exchange-only stations alphabetically
  out.sort((a, b) =>
    (a.rank == null) - (b.rank == null) || (a.rank || 0) - (b.rank || 0) ||
    a.call.localeCompare(b.call));
  return out;
}

function updateReview() {
  issues = result ? buildIssues() : [];
  el.review.textContent = `▸ Review issues (${issues.length})`;
  el.review.disabled = issues.length === 0;
  if (!reviewOpen) return;
  if (!issues.length) { closeReview(); return; }
  if (pendingCall) {
    const j = issues.findIndex((x) => x.call === pendingCall);
    if (j >= 0) issueIdx = j;
    pendingCall = null;
  }
  issueIdx = Math.max(0, Math.min(issueIdx, issues.length - 1));
  renderIssue();
}

function openReview() {
  if (!issues.length) return;
  reviewOpen = true;
  issueIdx = 0;
  el.rv.classList.remove("hidden");
  renderIssue();
}
function closeReview() {
  reviewOpen = false;
  el.rv.classList.add("hidden");
}
function go(delta) {
  issueIdx = Math.max(0, Math.min(issueIdx + delta, issues.length - 1));
  renderIssue();
}

function renderIssue() {
  const it = issues[issueIdx];
  if (!it) return;
  el.rvPos.textContent = `Issue ${issueIdx + 1} of ${issues.length}`;
  const atStart = issueIdx === 0, atEnd = issueIdx === issues.length - 1;
  el.rvPrev.disabled = el.rvPrev2.disabled = atStart;
  el.rvNext.disabled = el.rvNext2.disabled = atEnd;

  const body = el.rvBody;
  body.innerHTML = "";

  const title = document.createElement("div");
  title.className = "rv-title";
  const badges = [];
  if (it.rank != null) badges.push(`<span class="badge rare">RARE #${it.rank}</span>`);
  if (it.hasBust) badges.push(`<span class="badge exch">EXCHANGE</span>`);
  title.innerHTML = `${badges.join(" ")} <span class="rv-call">${it.call}</span>` +
                    `<span class="rv-ent">${it.entity || ""}</span>`;
  body.appendChild(title);

  const ex = document.createElement("p");
  ex.className = "rv-explain";
  const msgs = [];
  if (it.rank != null)
    msgs.push(`Resolves to <b>${it.entity}</b>, #${it.rank} on the most-wanted list — ` +
              `in a normal log a rare entity is usually a busted callsign, so check it.`);
  if (it.hasBust)
    msgs.push(`This station's received exchange isn't consistent across its ` +
              `${it.idxs.length} QSO(s); a station sends the same exchange all contest, ` +
              `so the odd one out is the likely copying error.`);
  ex.innerHTML = msgs.join(" ");
  body.appendChild(ex);

  if (it.fixes.length) {
    const fb = document.createElement("div");
    fb.className = "rv-fix";
    const lines = it.fixes.map((f) => `#${f[0] + 1}: ${f[1] || "∅"} → <b>${f[2]}</b>`).join(", ");
    const span = document.createElement("span");
    span.innerHTML = `Suggested fix — ${lines}`;
    const btn = document.createElement("button");
    btn.className = "btn primary";
    btn.textContent = "Apply fix";
    btn.addEventListener("click", () => {
      pendingCall = it.call;
      lc.applyFixes(records, result.exchange_field, it.fixes);
      analyze();
    });
    fb.append(span, btn);
    body.appendChild(fb);
  }

  body.appendChild(groupTable(it));
}

function groupTable(it) {
  const per = result.per_record;
  const f = result.exchange_field;
  const tbl = document.createElement("table");
  tbl.className = "rv-table";
  const thead = document.createElement("thead");
  const htr = document.createElement("tr");
  for (const c of ["#", "Date", "Time", "Call", "Band", "Mode", "RST",
                   `Exch: ${f || "—"}`, ""]) {
    const th = document.createElement("th"); th.textContent = c; htr.appendChild(th);
  }
  thead.appendChild(htr); tbl.appendChild(thead);

  const tb = document.createElement("tbody");
  for (const i of it.idxs) {
    const r = records[i], p = per[i];
    const tr = document.createElement("tr");
    if (p.exch_bust) tr.className = "exch";
    const cells = [
      [String(i + 1), null], [r.QSO_DATE || "", "QSO_DATE"], [r.TIME_ON || "", "TIME_ON"],
      [r.CALL || "", "CALL"], [r.BAND || "", "BAND"], [r.MODE || "", "MODE"],
      [r.RST_RCVD || "", "RST_RCVD"], [p.exch, "EXCH"],
    ];
    for (const [val, key] of cells) {
      const td = document.createElement("td");
      td.textContent = val;
      if (key && (key !== "EXCH" || f)) rvEditable(td, i, key);
      tr.appendChild(td);
    }
    const act = document.createElement("td");
    act.className = "rv-actions";
    const eb = document.createElement("button");
    eb.className = "mini"; eb.textContent = "Edit";
    eb.addEventListener("click", () => { pendingCall = it.call; openEditor(i); });
    const db = document.createElement("button");
    db.className = "mini danger"; db.textContent = "Del";
    db.addEventListener("click", () => rvDelete(i, it.call));
    act.append(eb, db);
    tr.appendChild(act);
    tb.appendChild(tr);
  }
  tbl.appendChild(tb);
  return tbl;
}

function rvEditable(td, i, key) {
  td.className = (td.className + " editable").trim();
  td.contentEditable = "true";
  td.spellcheck = false;
  td.addEventListener("focusout", () => {
    const target = key === "EXCH" ? field : key;
    if (!target) return;
    const v = td.textContent.trim();
    if ((records[i][target] || "") === v) return;
    pendingCall = issues[issueIdx]?.call;
    records[i][target] = v;
    analyze();
  });
}

function rvDelete(i, call) {
  const r = records[i];
  if (!confirm(`Delete this QSO?\n\n#${i + 1}  ${r.CALL || "?"}  ${r.BAND || ""}  ` +
               `${r.QSO_DATE || ""} ${r.TIME_ON || ""}\n\nThis cannot be undone ` +
               "(until you reload the file).")) return;
  pendingCall = call;
  records.splice(i, 1);
  selected.clear();
  analyze();
}

el.review.addEventListener("click", openReview);
el.rvClose.addEventListener("click", closeReview);
el.rvPrev.addEventListener("click", () => go(-1));
el.rvNext.addEventListener("click", () => go(1));
el.rvPrev2.addEventListener("click", () => go(-1));
el.rvNext2.addEventListener("click", () => go(1));
el.rv.addEventListener("click", (ev) => { if (ev.target === el.rv) closeReview(); });
document.addEventListener("keydown", (ev) => {
  if (!reviewOpen) return;
  if (!el.modal.classList.contains("hidden")) return;     // full-field editor is open
  const a = document.activeElement;                        // don't steal keys while editing
  if (a && (a.isContentEditable || a.tagName === "INPUT" || a.tagName === "SELECT")) return;
  if (ev.key === "Escape") closeReview();
  else if (ev.key === "ArrowRight") go(1);
  else if (ev.key === "ArrowLeft") go(-1);
});
