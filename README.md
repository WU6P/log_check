# log_check

A small desktop tool for **sanity-checking a ham-radio contest log** before you
submit it. Load an ADIF (`.adi`/`.adif`) or Cabrillo (`.log`) log and it runs
five checks, highlighting the suspect QSOs in an editable table so you can fix or
delete them.

It reuses the ADIF/Cabrillo parsing and the ARRL-DXCC → ITU callsign-series
country-resolution chain from the **Contest_Plan** project.

## What it checks (at a glance)

Each QSO is run through five independent checks; the **Flags** column names
every hit and rows are colored **pink** (rare), **yellow** (any other issue),
or **orange** (both).

| Flag | Check | What it catches |
|------|-------|-----------------|
| `RARE` | **Rare DXCC** | A worked callsign that resolves to a "most-wanted" entity (`rare.json`, 82 entities) — in a normal log, almost always a busted call. Conservative: only a *confident* prefix match flags. |
| `EXCH` | **Exchange bust** | A station whose received exchange isn't consistent across its QSOs (a station sends the same exchange all contest). Field auto-detected (`CQZ`, `RX_PWR`, `STATE`, …); offers one-click fixes. |
| `ZONE` | **Zone vs. entity** | A logged CQ/ITU zone that doesn't match the zone implied by the resolved DXCC entity — usually a busted call or a mis-typed zone. |
| `CALL!` / `CALL?` | **Callsign plausibility** | A callsign that's **malformed** (`CALL!`, wrong shape) or **unresolved** (`CALL?`, maps to no DXCC/ITU country — an exotic prefix that's usually a typo). |
| `DUPE?` | **Near-dupe / UBN** | A call worked **once** that's a single letter off a busier call worked 3+ times with the same prefix — a likely mis-copy. The Review window lists the busier station's QSOs for reference and offers a one-click correction. |

Flagged QSOs appear in an editable table and in a step-through **Review**
window; fixes you apply are written back when you **Save…**. Each check is
described in full below.

## Repository & status

- **GitHub:** <https://github.com/WU6P/log_check> — public, MIT licensed.
- **Live web app (GitHub Pages):** <https://wu6p.github.io/log_check/> — served
  from the `docs/` folder; no install, runs entirely in the browser.
- **Tests:** 34 Python (`python3 test_log_check.py`) + 29 JavaScript
  (`cd docs && node test_logcore.mjs`), all passing.

## The checks

Rows are highlighted **pink** for a rare-DXCC hit and **yellow** for any of the
other issues (orange when both fire); the **Flags** column names each
(`RARE`, `EXCH`, `ZONE`, `CALL?`/`CALL!`, `DUPE?`).

**1. Rare DXCC (pink).**
Every worked callsign is resolved to its DXCC entity. If that entity is on the
"most wanted" list (`rare.json`, 82 entities) the QSO is flagged — in a domestic
contest a stray *P5 / Bouvet / Navassa* is almost always a busted callsign worth
an eyeball. The resolver is deliberately conservative: a callsign only earns a
rare flag on a *confident* DXCC match (the matched prefix keeps every leading
letter), and a few coarse-prefix gotchas in the shared DXCC table are corrected
(`KP3/KP4`→Puerto Rico, `KP2`→Virgin Is., `R1`→European Russia) so common calls
aren't mislabelled as their rare island neighbours.

**2. Exchange (highlighted yellow).**
A station sends the *same* exchange all contest, so the received exchange should
agree across every QSO with that station. Pick the exchange field (auto-detected
— `CQZ` for CQ WW, `RX_PWR` for ARRL DX, `SRX_STRING`, `STATE`, … — and
override-able in the drop-down). Then:

* QSOs whose exchange disagrees with what the **same station** gave on its other
  QSOs are flagged.
* If **≥ 90 %** of the whole log carries one value, the contest is treated as a
  *fixed exchange* and lone values that differ from the log-wide majority are
  flagged too.
* A field whose values are nearly all unique (serial numbers) can't be
  cross-checked, so the check is skipped unless you tick **force check**.

**Auto-fix** corrects the clear-cut cases only: when a station's *early* QSO(s)
carry the wrong value but every *later* QSO carries the same right value (a
contiguous run) and that value is a strict majority, the early ones are
corrected to it. Ambiguous cases (ties, scattered values) are highlighted but
left for you to decide.

**3. Zone vs. entity (`ZONE`).**
When the log has a `CQZ`/`ITUZ` zone, it's checked against the worked entity's
real CQ/ITU zone (from `dxcc.json`). A JA logged as zone 5 (Japan is 25) stands
out. Multi-zone countries (USA `3,4,5`, Canada `1-5`) and entities with an
indeterminate zone are never flagged, so it's quiet on clean logs. Call-area
splits the coarse DXCC table mis-files (Hawaii/Alaska/Guam, Asiatic Russia, the
Canaries, bare `R<digit>` calls) are corrected so they resolve — and zone-check
— against the right entity.

**4. Callsign plausibility (`CALL!` / `CALL?`).**
Flags a callsign that's **malformed** (wrong shape) or **unresolved** (maps to
no DXCC *or* ITU country — an exotic prefix that's usually a typo).

**5. Near-dupe / UBN (`DUPE?`).**
A call worked **once** whose suffix is a single letter off a call worked 3+
times **with the same prefix** (e.g. `JR2HCZ` vs a busy `JR2SCZ`) is flagged as
a probable mis-copy of that busier station — the classic copy error. The shared
prefix and a suffix of ≥ 3 letters keep it from flagging genuinely different
short contest calls (`S53A` vs `S53D`). In the review window the busier
station's own QSOs are listed in a read-only reference table (band / time /
mode) so you can confirm the mis-copy at a glance, then apply the suggested
correction with one click.

## Editing

* **Inline:** double-click any of the Date / Time / Call / Band / Mode / RST /
  Exchange cells to edit; the checks re-run and the highlights update.
* **Full editor:** double-click the row number (or select a row and press
  *Edit fields…*) to edit every ADIF field of that QSO, or add a new field.
* **Delete:** select one or more rows and press *Delete QSO* (asks to confirm).
* **Save…** writes the (edited) log back out **in the format it was loaded in**
  — an ADIF in gives an ADIF (`.adi`) out, a Cabrillo `.log` in gives a Cabrillo
  `.log` out. Cabrillo saves are rebuilt in place over the original file, so the
  header, footer and every untouched token (the whole sent side) are preserved
  verbatim; only edited calls / exchanges / times change and deleted QSOs drop.

## Running

Needs PyQt5. A self-contained virtualenv is the easiest path:

```sh
python3 -m venv .venv
.venv/bin/pip install PyQt5
./run.sh                     # or: .venv/bin/python log_check.py
```

## Tests

The whole engine (`logcore.py`) is GUI-free and unit-tested:

```sh
python3 test_log_check.py    # 34 tests, pure stdlib — no PyQt5 needed
```

## Files

| file | purpose |
|------|---------|
| `log_check.py`     | PyQt5 GUI |
| `logcore.py`       | parsing, DXCC/rarity resolution, exchange analysis (stdlib) |
| `test_log_check.py`| unit tests for `logcore.py` |
| `dxcc.json` / `itu.json` / `rare.json` | DXCC/ITU lookups + most-wanted ranking (from Contest_Plan) |

## Web version (runs in a browser / GitHub Pages)

A fully client-side JavaScript port lives in [`docs/`](docs/) — same five checks,
same editing, but no install and no server, so it can be hosted free on GitHub
Pages (which serves straight from a `/docs` folder). `docs/logcore.js` mirrors
`logcore.py` and `docs/test_logcore.mjs` mirrors this project's tests (29).
See [`docs/README.md`](docs/README.md) for running it locally and deploying.

## Known limitation

DXCC resolution is only as granular as the shared `dxcc.json` (built from the
ARRL DXCC PDF), which keeps coarse prefix keys for a handful of entities. The
confidence rule plus the curated call-area overrides (Hawaii/Alaska/Guam,
Asiatic Russia, Kaliningrad's `2F` series, the Canaries, bare `R<digit>`) remove
the common false flags; if you spot another umbrella/split prefix over-flagging,
add a row to `_PREFIX_OVERRIDES` — in **both** `logcore.py` and
`docs/logcore.js` (they are parallel implementations kept in sync by the mirror
test suites).
