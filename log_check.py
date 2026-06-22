#!/usr/bin/env python3
"""log_check — a desktop log-checker for ham-radio contest logs.

Loads an ADIF (.adi/.adif) or Cabrillo (.log) contest log and runs two
integrity checks, highlighting the suspect QSOs right in an editable table:

  1. Rare DXCC   — QSOs whose worked entity is on the "most wanted" list
                   (rare.json). In a domestic contest a stray P5/Bouvet is
                   almost always a busted callsign. Highlighted PINK.

  2. Exchange    — pick the received-exchange field (auto-detected; override in
                   the drop-down). A station sends the same exchange all
                   contest, so when the value disagrees across that station's
                   QSOs the odd ones are flagged. If 90%+ of the whole log
                   carries one value the contest is treated as a *fixed
                   exchange* and lone deviations are flagged too. Highlighted
                   YELLOW.  "Auto-fix" corrects the clear cases (early value
                   wrong, then the same right value repeated).

You can edit any cell inline, open a full-field editor (double-click the row
number or the Edit button), delete the selected QSO (with confirmation), and
save back out in the same format the log was loaded in (ADIF in → ADIF out,
Cabrillo .log in → Cabrillo out).

Run:  python3 log_check.py   (needs PyQt5:  pip install PyQt5)
"""

import sys
from pathlib import Path

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QComboBox, QCheckBox, QTableWidget, QTableWidgetItem, QFileDialog,
    QMessageBox, QAbstractItemView, QHeaderView, QDialog, QFormLayout,
    QLineEdit, QDialogButtonBox, QScrollArea,
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QBrush

import logcore as lc

# (header, adif-field-key or special tag). Editable columns carry a real field
# key; "EXCH" is rebound to the chosen exchange field; the rest are computed.
COLUMNS = [
    ("#", "_NUM"), ("Date", "QSO_DATE"), ("Time", "TIME_ON"), ("Call", "CALL"),
    ("Band", "BAND"), ("Mode", "MODE"), ("RST", "RST_RCVD"),
    ("Exchange", "EXCH"), ("Entity", "_ENTITY"), ("Rare#", "_RARE"),
    ("Flags", "_FLAGS"),
]
EDITABLE = {"QSO_DATE", "TIME_ON", "CALL", "BAND", "MODE", "RST_RCVD", "EXCH"}

COL_RARE = QColor(255, 205, 210)     # pink
COL_EXCH = QColor(255, 245, 157)     # yellow
COL_BOTH = QColor(255, 183, 120)     # orange (both checks fire)
COL_PLAIN = QColor(Qt.white)


class FieldEditor(QDialog):
    """Edit every ADIF field of a single QSO (add new fields too)."""

    def __init__(self, qso, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Edit QSO — {qso.get('CALL', '?')}")
        self.qso = qso
        self.edits = {}
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        form = QFormLayout(inner)
        for key in sorted(k for k in qso if not k.startswith("_")):
            le = QLineEdit(str(qso.get(key, "")))
            self.edits[key] = le
            form.addRow(key, le)
        # one blank row to add a brand-new field
        self.new_key = QLineEdit()
        self.new_key.setPlaceholderText("NEW_FIELD_NAME")
        self.new_val = QLineEdit()
        row = QWidget(); rl = QHBoxLayout(row); rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.new_key); rl.addWidget(self.new_val)
        form.addRow("add field", row)
        scroll.setWidget(inner)
        outer.addWidget(scroll)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)
        self.resize(420, 520)

    def apply(self):
        """Write edited values back into the QSO dict in place."""
        for key, le in self.edits.items():
            self.qso[key] = le.text()
        nk = self.new_key.text().strip().upper()
        if nk:
            self.qso[nk] = self.new_val.text()


class LogCheck(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("log_check — contest log checker")
        self.records = []
        self.path = None
        self.src_text = ""
        self.src_format = "adif"
        self.result = None
        self.exch_field = ""
        self._loading = False           # guard so programmatic fills don't edit
        self._build_ui()
        self.resize(1080, 680)

    # ---- UI construction ------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # Top bar: file + exchange controls
        bar = QHBoxLayout()
        self.btn_open = QPushButton("Open log…")
        self.btn_open.clicked.connect(self.open_log)
        bar.addWidget(self.btn_open)

        bar.addWidget(QLabel("   Exchange field:"))
        self.cmb_field = QComboBox()
        self.cmb_field.setMinimumWidth(140)
        self.cmb_field.currentIndexChanged.connect(self._field_changed)
        bar.addWidget(self.cmb_field)

        self.chk_force = QCheckBox("force check (serial-like field)")
        self.chk_force.setToolTip(
            "Check the exchange even when its values look like serial numbers "
            "(mostly unique). Off by default to avoid false alarms.")
        self.chk_force.stateChanged.connect(lambda _: self.analyze())
        bar.addWidget(self.chk_force)
        bar.addStretch(1)

        self.btn_save = QPushButton("Save…")
        self.btn_save.clicked.connect(self.save_log)
        self.btn_save.setEnabled(False)
        bar.addWidget(self.btn_save)
        root.addLayout(bar)

        # Summary line
        self.lbl_summary = QLabel("No log loaded. Open an .adi / .adif / .log file.")
        self.lbl_summary.setWordWrap(True)
        root.addWidget(self.lbl_summary)

        # The table
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels([h for h, _ in COLUMNS])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.itemChanged.connect(self._item_changed)
        self.table.cellDoubleClicked.connect(self._maybe_edit_row)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.Interactive)
        hdr.setStretchLastSection(True)
        root.addWidget(self.table, 1)

        # Bottom action bar
        act = QHBoxLayout()
        self.btn_fix = QPushButton("Auto-fix exchanges")
        self.btn_fix.clicked.connect(self.auto_fix)
        self.btn_fix.setEnabled(False)
        act.addWidget(self.btn_fix)

        self.btn_edit = QPushButton("Edit fields…")
        self.btn_edit.clicked.connect(self.edit_selected)
        self.btn_edit.setEnabled(False)
        act.addWidget(self.btn_edit)

        self.btn_del = QPushButton("Delete QSO")
        self.btn_del.clicked.connect(self.delete_selected)
        self.btn_del.setEnabled(False)
        act.addWidget(self.btn_del)
        act.addStretch(1)

        legend = QLabel()
        legend.setText(
            '<span style="background:#ffcdd2">&nbsp;rare DXCC&nbsp;</span> &nbsp; '
            '<span style="background:#fff59d">&nbsp;exchange / zone / call / dupe&nbsp;</span> &nbsp; '
            '<span style="background:#ffb778">&nbsp;rare + another&nbsp;</span>')
        act.addWidget(legend)
        root.addLayout(act)

        self.table.itemSelectionChanged.connect(self._selection_changed)

    # ---- Loading --------------------------------------------------------
    def open_log(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open contest log", "",
            "Logs (*.adi *.adif *.log);;All files (*)")
        if not path:
            return
        try:
            # read bytes + decode (not read_text) so CRLF line endings survive
            # universal-newline translation and a Cabrillo save round-trips exactly
            text = Path(path).read_bytes().decode("utf-8", errors="replace")
            recs = lc.records_from_text(text)
        except Exception as e:
            QMessageBox.critical(self, "Open failed", f"Could not read log:\n{e}")
            return
        if not recs:
            QMessageBox.warning(self, "No QSOs",
                                "No QSO records were found in that file.")
            return
        self.records = recs
        self.path = path
        self.src_text = text                       # for verbatim Cabrillo save
        self.src_format = lc.detect_format(text)   # 'adif' | 'cabrillo'
        self._populate_field_combo()
        self.analyze()
        self.btn_save.setEnabled(True)
        self.setWindowTitle(f"log_check — {Path(path).name}")

    def _populate_field_combo(self):
        self._loading = True
        self.cmb_field.clear()
        cands = lc.exchange_candidates(self.records)
        default = lc.detect_exchange_field(self.records)
        items = ["(none)"] + cands
        self.cmb_field.addItems(items)
        if default and default in cands:
            self.cmb_field.setCurrentIndex(items.index(default))
        self._loading = False
        self.exch_field = default or ""

    def _field_changed(self, _idx):
        if self._loading:
            return
        sel = self.cmb_field.currentText()
        self.exch_field = "" if sel == "(none)" else sel
        self.analyze()

    # ---- Analysis + table render ---------------------------------------
    def analyze(self):
        if not self.records:
            return
        self.result = lc.analyze(
            self.records, exchange_field=self.exch_field,
            force_exchange=self.chk_force.isChecked())
        self._render()
        self._update_summary()

    def _render(self):
        self._loading = True
        per = self.result["per_record"]
        field = self.result["exchange_field"]
        # keep the Exchange header showing which field it is
        labels = [h if k != "EXCH" else f"Exch: {field or '—'}" for h, k in COLUMNS]
        self.table.setHorizontalHeaderLabels(labels)
        self.table.setRowCount(len(self.records))
        for i, qso in enumerate(self.records):
            info = per[i]
            rare = info["rank"] is not None
            other = (info["exch_bust"] or info["zone_bust"]
                     or info["call_bad"] or info["dupe_of"])
            bg = (COL_BOTH if rare and other else COL_RARE if rare
                  else COL_EXCH if other else COL_PLAIN)
            for c, (_h, key) in enumerate(COLUMNS):
                text = self._cell_text(i, qso, info, key)
                item = QTableWidgetItem(text)
                editable = key in EDITABLE and not (key == "EXCH" and not field)
                flags = item.flags()
                if editable:
                    flags |= Qt.ItemIsEditable
                else:
                    flags &= ~Qt.ItemIsEditable
                item.setFlags(flags)
                item.setBackground(QBrush(bg))
                if c == 0:
                    item.setData(Qt.UserRole, i)        # stable row id
                self.table.setItem(i, c, item)
        self.table.resizeColumnsToContents()
        self._loading = False

    def _cell_text(self, i, qso, info, key):
        if key == "_NUM":
            return str(i + 1)
        if key == "_ENTITY":
            return info["entity"]
        if key == "_RARE":
            return "" if info["rank"] is None else f"#{info['rank']}"
        if key == "_FLAGS":
            f = []
            if info["rank"] is not None:
                f.append("RARE")
            if info["exch_bust"]:
                f.append("EXCH")
            if info["zone_bust"]:
                f.append("ZONE")
            if info["call_bad"]:
                f.append("CALL!" if info["call_bad"] == "malformed" else "CALL?")
            if info["dupe_of"]:
                f.append("DUPE?")
            return " ".join(f)
        if key == "EXCH":
            return info["exch"]
        return str(qso.get(key, ""))

    def _update_summary(self):
        r = self.result
        field = r["exchange_field"] or "—"
        parts = [f"<b>{len(self.records)}</b> QSOs",
                 f"<b>{r['rare_count']}</b> rare DXCC"]
        if r["exchange_field"]:
            if r["exch_applicable"]:
                fixed = (f"FIXED at '{r['majority_value']}' "
                         f"({r['majority_share']*100:.0f}%)" if r["is_fixed"]
                         else f"top '{r['majority_value']}' "
                              f"{r['majority_share']*100:.0f}%")
                parts.append(f"exchange '{field}' [{fixed}] — "
                             f"<b>{r['bust_count']}</b> busts, "
                             f"<b>{len(r['fixes'])}</b> auto-fixable")
            else:
                parts.append(f"exchange '{field}' looks like serial numbers — "
                             f"check skipped (tick ‘force check’ to run it)")
        else:
            parts.append("no exchange field selected")
        parts.append(f"<b>{r['zone_count']}</b> zone, "
                     f"<b>{r['callbad_count']}</b> bad-call, "
                     f"<b>{r['dupe_count']}</b> near-dupe")
        self.lbl_summary.setText("&nbsp;|&nbsp; ".join(parts))
        self.btn_fix.setEnabled(bool(r["fixes"]))

    # ---- Editing --------------------------------------------------------
    def _item_changed(self, item):
        if self._loading:
            return
        i = item.row()
        col = item.column()
        key = COLUMNS[col][1]
        if key not in EDITABLE:
            return
        target = self.exch_field if key == "EXCH" else key
        if not target:
            return
        self.records[i][target] = item.text()
        self.analyze()                  # re-check + repaint

    def _maybe_edit_row(self, row, col):
        # Double-clicking the number / computed columns opens the full editor;
        # double-clicking an editable cell just edits inline (Qt default).
        if COLUMNS[col][1] not in EDITABLE:
            self._edit_row(row)

    def edit_selected(self):
        rows = self._selected_rows()
        if len(rows) == 1:
            self._edit_row(rows[0])

    def _edit_row(self, row):
        dlg = FieldEditor(self.records[row], self)
        if dlg.exec_() == QDialog.Accepted:
            dlg.apply()
            self.analyze()

    def delete_selected(self):
        rows = self._selected_rows()
        if not rows:
            return
        calls = ", ".join(self.records[i].get("CALL", "?") for i in rows[:6])
        more = "…" if len(rows) > 6 else ""
        if QMessageBox.question(
                self, "Delete QSO(s)",
                f"Delete {len(rows)} QSO(s)?\n\n{calls}{more}\n\n"
                "This cannot be undone (until you reload the file).",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        for i in sorted(rows, reverse=True):
            del self.records[i]
        self.analyze()

    def auto_fix(self):
        fixes = self.result["fixes"]
        if not fixes:
            return
        field = self.result["exchange_field"]
        preview = "\n".join(
            f"  #{i+1} {self.records[i].get('CALL','?'):<10} "
            f"{old or '∅'}  →  {new}" for i, old, new in fixes[:15])
        more = f"\n  …and {len(fixes)-15} more" if len(fixes) > 15 else ""
        if QMessageBox.question(
                self, "Auto-fix exchanges",
                f"Apply {len(fixes)} exchange fix(es) to field '{field}'?\n\n"
                f"{preview}{more}",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes) != QMessageBox.Yes:
            return
        n = lc.apply_fixes(self.records, field, fixes)
        self.analyze()
        QMessageBox.information(self, "Auto-fix", f"Corrected {n} QSO(s).")

    # ---- Saving ---------------------------------------------------------
    def save_log(self):
        if not self.records:
            return
        cabrillo = self.src_format == "cabrillo"
        ext = ".log" if cabrillo else ".adi"
        caption = "Save Cabrillo" if cabrillo else "Save ADIF"
        filt = ("Cabrillo (*.log);;All files (*)" if cabrillo
                else "ADIF (*.adi *.adif);;All files (*)")
        suggested = ""
        if self.path:
            p = Path(self.path)
            suggested = str(p.with_name(p.stem + "_checked" + ext))
        path, _ = QFileDialog.getSaveFileName(self, caption, suggested, filt)
        if not path:
            return
        try:
            text = (lc.serialize_cabrillo(self.records, self.src_text) if cabrillo
                    else lc.serialize_adif(self.records))
            # write bytes so the serializer's exact line endings aren't re-translated
            Path(path).write_bytes(text.encode("utf-8"))
        except Exception as e:
            QMessageBox.critical(self, "Save failed", str(e))
            return
        QMessageBox.information(self, "Saved",
                                f"Wrote {len(self.records)} QSOs to\n{path}")

    # ---- helpers --------------------------------------------------------
    def _selected_rows(self):
        return sorted({idx.row() for idx in self.table.selectedIndexes()})

    def _selection_changed(self):
        rows = self._selected_rows()
        self.btn_del.setEnabled(bool(rows))
        self.btn_edit.setEnabled(len(rows) == 1)


def main():
    app = QApplication(sys.argv)
    win = LogCheck()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
