# log_check (web)

The same contest-log checker as the desktop app, but **100 % client-side** — it
runs in the browser with no server and no install, so it can be hosted for free
on **GitHub Pages**. Your log file is read locally and never leaves the browser.

It's a JavaScript port of the Python engine: `logcore.js` mirrors `logcore.py`
(same parsing, DXCC/rarity resolution, and exchange analysis), and
`test_logcore.mjs` mirrors `test_log_check.py` so the two stay in step.

## What it does

Identical to the desktop version — see the top-level `README.md`:

Five checks (highlighted in the table, named in the **Flags** column):

* **Rare DXCC (pink)** — entity on the most-wanted list.
* **Exchange (yellow)** — a station whose received exchange disagrees across its
  QSOs (and, in a ≥90 %-fixed contest, lone deviations). **Apply fix** corrects
  the clear early-wrong-then-consistent cases.
* **Zone** — a logged `CQZ`/`ITUZ` that doesn't match the entity's real zone.
* **Call** — a malformed or country-unresolvable callsign.
* **Near-dupe (UBN)** — a once-worked call one suffix-letter off a busier
  same-prefix station; one-click correction in the review window.
* Edit any cell inline, open a full-field editor (double-click the row number or
  use *Edit fields…*), delete selected QSOs (with confirmation), and **Save…**
  downloads the edited log as ADIF.

### Review window (work through issues one at a time)

The full table is fine for an overview, but to actually triage the problems
click **▸ Review issues (N)**. It opens a focused window showing **one issue at
a time**:

* **Rare DXCC** issues show the station, entity, and rank.
* **Exchange** issues show **every QSO with that station grouped together**, so
  the inconsistency (e.g. `100 / KW / KW`) is obvious, with the suggested fix
  and an **Apply fix** button right there.

Edit cells inline, open the full editor, or delete a QSO from within the window,
and step with **‹ Prev / Next ›** or the **← / →** keys (**Esc** closes). Fixing
an issue drops it from the list and moves you to the next.

## Run locally

ES modules and `fetch()` need a real HTTP origin (opening `index.html` from
`file://` is blocked by the browser), so serve the folder:

```sh
cd docs
python3 -m http.server 8000
# open http://localhost:8000/
```

There's a self-test page that drives the UI and reports PASS/FAIL:
`http://localhost:8000/_selftest.html`.

Run the core unit tests under Node (no browser, no dependencies):

```sh
node test_logcore.mjs        # 29 tests, mirrors the Python suite
```

## Deploy to GitHub Pages

1. Put this repo on GitHub. This `docs/` folder already holds everything Pages
   needs: `index.html`, `app.js`, `logcore.js`, `styles.css`, and
   `dxcc.json` / `itu.json` / `rare.json`.
2. Repo **Settings → Pages → Build and deployment → Deploy from a branch**.
3. Pick your branch (e.g. `main`) and the **/docs** folder, then **Save**.
4. Your app appears at `https://<user>.github.io/<repo>/` in a minute or two.
   That URL is all anyone needs — no install.

`_selftest.html`, `test_logcore.mjs`, and `package.json` are dev-only; harmless
to publish, or delete them from the published copy.

## Files

| file | purpose |
|------|---------|
| `index.html` / `styles.css` | page + styling |
| `app.js`            | DOM glue: file load, table render, review window, edit/delete/fix/save |
| `logcore.js`        | parsing + DXCC/rarity + exchange analysis (port of `logcore.py`) |
| `test_logcore.mjs`  | Node unit tests (mirror of `test_log_check.py`) |
| `_selftest.html`    | in-browser end-to-end smoke test (PASS/FAIL) |
| `dxcc/itu/rare.json`| lookup tables |
