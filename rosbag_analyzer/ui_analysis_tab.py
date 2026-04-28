"""Latency-analysis tab: chain selection, compute, stats, plot panes."""

from __future__ import annotations

import json
import traceback
from typing import Dict, List, Optional

import pandas as pd
from PyQt5 import QtCore, QtGui, QtWidgets

from constants import hop_label as _lbl
from latency import compute_chain_latency, stats_table
from loader import ChainLoaderThread
from metadata import BagMetadata
from plotting import PlotPane, PopoutWindow


class AnalysisTab(QtWidgets.QWidget):
    """One self-contained latency analysis."""

    status_message = QtCore.pyqtSignal(str)

    def __init__(self, get_bag_meta_callable, parent=None):
        super().__init__(parent)
        self._get_bag = get_bag_meta_callable
        self.dfs: Dict[str, pd.DataFrame] = {}
        self.merged: Optional[pd.DataFrame] = None
        self.loader: Optional[ChainLoaderThread] = None
        self._maximized_pane: Optional[PlotPane] = None
        self._pre_max_sizes: Optional[List[int]] = None
        self._popouts: Dict[PlotPane, PopoutWindow] = {}
        self._build()

    # ------------------------------------------------------------------ UI ---
    def _build(self):
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)

        split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        split.setHandleWidth(6); split.setChildrenCollapsible(False)
        v.addWidget(split, stretch=1)

        # Top: chain controls
        chain_box = QtWidgets.QGroupBox("Chain (in order: source → destination)")
        cb = QtWidgets.QVBoxLayout(chain_box)
        self.chain_list = QtWidgets.QListWidget()
        self.chain_list.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        cb.addWidget(self.chain_list)

        chain_btns = QtWidgets.QHBoxLayout()
        for label, cb_ in [("↑", lambda: self._move_chain_item(-1)),
                           ("↓", lambda: self._move_chain_item(+1)),
                           ("Remove", self._remove_chain_item),
                           ("Clear", self.chain_list.clear),
                           ("Save…", self.on_save_chain),
                           ("Load…", self.on_load_chain)]:
            b = QtWidgets.QPushButton(label)
            b.clicked.connect(cb_)
            chain_btns.addWidget(b)
        cb.addLayout(chain_btns)

        tol_row = QtWidgets.QHBoxLayout()
        tol_row.addWidget(QtWidgets.QLabel("Approx. tolerance (ms):"))
        self.tolerance_spin = QtWidgets.QDoubleSpinBox()
        self.tolerance_spin.setDecimals(1)
        self.tolerance_spin.setRange(0.1, 5000.0)
        self.tolerance_spin.setValue(50.0)
        tol_row.addWidget(self.tolerance_spin)

        tol_row.addSpacing(20)
        tol_row.addWidget(QtWidgets.QLabel("SLA threshold (ms):"))
        self.threshold_spin = QtWidgets.QDoubleSpinBox()
        self.threshold_spin.setDecimals(2)
        self.threshold_spin.setRange(0.0, 1_000_000.0)
        self.threshold_spin.setValue(0.0)
        self.threshold_spin.setSpecialValueText("(off)")
        self.threshold_spin.valueChanged.connect(self._on_threshold_changed)
        tol_row.addWidget(self.threshold_spin)
        tol_row.addStretch()
        cb.addLayout(tol_row)

        self.compute_btn = QtWidgets.QPushButton("Compute Latency  ▶")
        self.compute_btn.setStyleSheet("font-weight:bold;padding:6px;")
        self.compute_btn.clicked.connect(self.on_compute)
        cb.addWidget(self.compute_btn)

        self.progress = QtWidgets.QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setFormat("%p%  reading topics…")
        self.progress.setVisible(False)
        cb.addWidget(self.progress)
        split.addWidget(chain_box)

        # Bottom: stats + plot panes (resizable)
        bottom = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        bottom.setHandleWidth(6); bottom.setChildrenCollapsible(False)

        stats_box = QtWidgets.QGroupBox(
            "Latency stats — Pipeline (rosbag-based) and True (header-based)")
        stats_box.setMinimumHeight(80)
        sb = QtWidgets.QVBoxLayout(stats_box)
        self.method_label = QtWidgets.QLabel("")
        sb.addWidget(self.method_label)

        # Reasoning panel: explains what is being analysed and why.
        self.reasoning_label = QtWidgets.QLabel("")
        self.reasoning_label.setWordWrap(True)
        self.reasoning_label.setStyleSheet(
            "background:#f6f6f0; border:1px solid #ddd; "
            "padding:6px; color:#333;")
        self.reasoning_label.setTextInteractionFlags(
            QtCore.Qt.TextSelectableByMouse)
        sb.addWidget(self.reasoning_label)

        self.stats_table = QtWidgets.QTableWidget(0, 13)
        self.stats_table.setHorizontalHeaderLabels(
            ["Kind", "Hop / what", "definition", "n",
             "min", "mean", "p50", "p95", "p99",
             "max", "stddev", "jitter", "above SLA"])
        self.stats_table.horizontalHeader().setStretchLastSection(True)
        self.stats_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        sb.addWidget(self.stats_table)

        self.loss_label = QtWidgets.QLabel("")
        self.loss_label.setStyleSheet("font-family: monospace;")
        sb.addWidget(self.loss_label)

        export_row = QtWidgets.QHBoxLayout()
        self.export_btn = QtWidgets.QPushButton("Export CSV…")
        self.export_btn.clicked.connect(self.on_export_csv)
        self.export_btn.setEnabled(False)
        self.add_pane_btn = QtWidgets.QPushButton("➕ Add Plot Pane")
        self.add_pane_btn.clicked.connect(self.on_add_pane)
        self.add_pane_btn.setEnabled(False)
        self.layout_combo = QtWidgets.QComboBox()
        self.layout_combo.addItems(["Plots: Vertical", "Plots: Horizontal"])
        self.layout_combo.currentIndexChanged.connect(self._on_layout_changed)
        export_row.addWidget(self.add_pane_btn)
        export_row.addWidget(self.layout_combo)
        export_row.addWidget(self.export_btn)
        export_row.addStretch()
        sb.addLayout(export_row)
        bottom.addWidget(stats_box)

        self.panes_split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.panes_split.setHandleWidth(6)
        self.panes_split.setChildrenCollapsible(False)
        bottom.addWidget(self.panes_split)
        bottom.setStretchFactor(0, 0); bottom.setStretchFactor(1, 1)
        bottom.setSizes([220, 700])
        split.addWidget(bottom)
        split.setSizes([200, 700])

    # ----------------------------------------------------------- chain ops ---
    def add_topic(self, topic: str, ttype: str) -> None:
        self.chain_list.addItem(f"{topic}    [{ttype}]")

    def chain_topics(self) -> List[str]:
        return [self.chain_list.item(i).text().split("    [")[0]
                for i in range(self.chain_list.count())]

    def _move_chain_item(self, delta: int):
        r = self.chain_list.currentRow()
        if r < 0:
            return
        new_r = r + delta
        if 0 <= new_r < self.chain_list.count():
            it = self.chain_list.takeItem(r)
            self.chain_list.insertItem(new_r, it)
            self.chain_list.setCurrentRow(new_r)

    def _remove_chain_item(self):
        r = self.chain_list.currentRow()
        if r >= 0:
            self.chain_list.takeItem(r)

    # ---------------------------------------------------------- save/load ---
    def on_save_chain(self):
        chain = self.chain_topics()
        if not chain:
            QtWidgets.QMessageBox.information(
                self, "Empty chain", "Add topics first.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save chain preset", "chain.json", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "w") as f:
                json.dump({"chain": chain,
                           "tolerance_ms": self.tolerance_spin.value(),
                           "threshold_ms": self.threshold_spin.value()},
                          f, indent=2)
            self.status_message.emit(f"Saved chain to {path}")
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Save failed", str(e))

    def on_load_chain(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Load chain preset", "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path) as f:
                payload = json.load(f)
            self.chain_list.clear()
            bag: Optional[BagMetadata] = self._get_bag()
            for t in payload.get("chain", []):
                ttype = bag.topics.get(t, "?") if bag else "?"
                self.chain_list.addItem(f"{t}    [{ttype}]")
            if "tolerance_ms" in payload:
                self.tolerance_spin.setValue(float(payload["tolerance_ms"]))
            if "threshold_ms" in payload:
                self.threshold_spin.setValue(float(payload["threshold_ms"]))
            self.status_message.emit(f"Loaded chain from {path}")
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Load failed", str(e))

    # ------------------------------------------------------------- compute ---
    def on_compute(self):
        bag = self._get_bag()
        if not bag:
            QtWidgets.QMessageBox.information(
                self, "No bag", "Open a bag first.")
            return
        chain = self.chain_topics()
        if len(chain) < 2:
            QtWidgets.QMessageBox.information(
                self, "Need ≥ 2 topics", "Add at least two topics.")
            return
        for t in chain:
            if t not in bag.topics:
                QtWidgets.QMessageBox.warning(
                    self, "Unknown topic", f"{t} not in this bag.")
                return

        self.compute_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.status_message.emit("Reading chain topics…")
        self.loader = ChainLoaderThread(bag, chain)
        self.loader.progress.connect(self._on_progress)
        self.loader.finished_ok.connect(self._on_done)
        self.loader.failed.connect(self._on_failed)
        self.loader.start()

    def _on_progress(self, pct: int, msg: str):
        if pct >= 0:
            self.progress.setRange(0, 100)
            self.progress.setValue(pct)
            # Trim msg for the bar's overlay; full text goes to the status bar.
            short = msg.split(" | ", 1)[0]
            self.progress.setFormat(f"%p%  {short}")
        self.status_message.emit(msg)

    def _on_failed(self, msg: str):
        self.progress.setVisible(False)
        self.compute_btn.setEnabled(True)
        QtWidgets.QMessageBox.critical(self, "Load failed", msg)

    def _on_done(self, dfs: Dict[str, pd.DataFrame]):
        self.progress.setVisible(False)
        self.compute_btn.setEnabled(True)
        self.dfs = dfs
        chain = self.chain_topics()
        try:
            merged, method, counts, result = compute_chain_latency(
                dfs, chain, tolerance_ms=self.tolerance_spin.value())
        except Exception as e:
            QtWidgets.QMessageBox.critical(
                self, "Compute failed", f"{e}\n\n{traceback.format_exc()}")
            return

        self.merged = merged
        self.result = result
        if merged.empty:
            QtWidgets.QMessageBox.warning(
                self, "No matched messages",
                "No messages could be matched across the chain.\n"
                "Try a larger tolerance or a different chain.")
            return

        true_tag = ("<b style='color:#2ca02c;'>True latency available</b>"
                    if result.has_true_latency
                    else "<b style='color:#888;'>True latency: unavailable</b>")
        self.method_label.setText(
            f"Matched {len(merged):,} messages across {len(chain)} topics  |  "
            f"Match: <b>{method}</b>"
            + (f"  (tolerance ±{self.tolerance_spin.value()} ms)"
               if method == "approximate" else "")
            + f"  |  {true_tag}")

        # Render the reasoning panel ("what is being analysed and why").
        self.reasoning_label.setText(
            "<br>".join("• " + line for line in result.reasoning_lines()))

        self._refresh_stats_table()

        loss_lines = ["Per-topic counts and inter-hop loss:"]
        prev_n = None
        for i, t in enumerate(chain):
            n = counts[t]
            if prev_n is None:
                loss_lines.append(f"  {_lbl(i)}  {t:<60}  {n:>10,}")
            else:
                lost = prev_n - n
                pct = (lost / prev_n * 100) if prev_n > 0 else 0
                loss_lines.append(
                    f"  {_lbl(i)}  {t:<60}  {n:>10,}   "
                    f"Δ={lost:+,}  ({pct:+.2f}%)")
            prev_n = n
        loss_lines.append(
            f"  Matched in chain: {len(merged):,} "
            f"({100*len(merged)/max(1,counts[chain[0]]):.2f}% of source)")
        self.loss_label.setText("\n".join(loss_lines))

        all_panes = self._all_panes()
        if not all_panes:
            self.on_add_pane()
        else:
            for pane in all_panes:
                pane.set_data(merged, chain)

        self.export_btn.setEnabled(True)
        self.add_pane_btn.setEnabled(True)
        self.status_message.emit(
            f"Done. {len(merged):,} matches via {method} matching.")

    # --------------------------------------------------------- stats ----
    def _threshold(self) -> Optional[float]:
        v = self.threshold_spin.value()
        return v if v > 0 else None

    def _refresh_stats_table(self):
        if self.merged is None or self.merged.empty:
            self.stats_table.setRowCount(0)
            return
        thr = self._threshold()
        rows = stats_table(self.merged, self.chain_topics(), threshold_ms=thr)
        self.stats_table.setRowCount(0)
        keys = ["min_ms", "mean_ms", "p50_ms", "p95_ms", "p99_ms",
                "max_ms", "stddev_ms", "jitter_ms"]
        # Light tint per kind for fast visual grouping.
        bg_pipe = QtGui.QBrush(QtGui.QColor("#eaf3ff"))
        bg_true = QtGui.QBrush(QtGui.QColor("#eafbe7"))
        for row in rows:
            r = self.stats_table.rowCount()
            self.stats_table.insertRow(r)
            kind = row.get("kind", "pipeline")
            kind_tag = "Pipeline" if kind == "pipeline" else "True"
            kind_item = QtWidgets.QTableWidgetItem(kind_tag)
            self.stats_table.setItem(r, 0, kind_item)
            self.stats_table.setItem(r, 1, QtWidgets.QTableWidgetItem(row["hop"]))
            self.stats_table.setItem(
                r, 2, QtWidgets.QTableWidgetItem(row.get("what", "")))
            self.stats_table.setItem(r, 3, QtWidgets.QTableWidgetItem(f"{row['n']:,}"))
            for c, key in enumerate(keys, start=4):
                self.stats_table.setItem(
                    r, c, QtWidgets.QTableWidgetItem(f"{row[key]:.3f}"))
            if thr is not None:
                cell = QtWidgets.QTableWidgetItem(
                    f"{row['above_n']:,}  ({row['above_pct']:.2f}%)")
                if row["above_n"] > 0:
                    cell.setForeground(QtGui.QBrush(QtGui.QColor("#b00020")))
                self.stats_table.setItem(r, 12, cell)
            else:
                self.stats_table.setItem(r, 12, QtWidgets.QTableWidgetItem("—"))
            bg = bg_pipe if kind == "pipeline" else bg_true
            for c in range(self.stats_table.columnCount()):
                it = self.stats_table.item(r, c)
                if it is not None:
                    it.setBackground(bg)
        self.stats_table.resizeColumnsToContents()

    def _on_threshold_changed(self, _):
        thr = self._threshold()
        for pane in self._all_panes():
            pane.set_threshold(thr)
        self._refresh_stats_table()

    # ------------------------------------------------------- pane mgmt ---
    def _on_layout_changed(self, idx: int):
        ori = QtCore.Qt.Horizontal if idx == 1 else QtCore.Qt.Vertical
        self.panes_split.setOrientation(ori)
        n = self.panes_split.count()
        if n > 0:
            ext = (self.panes_split.width() if ori == QtCore.Qt.Horizontal
                   else self.panes_split.height())
            self.panes_split.setSizes([max(ext, 200) // n] * n)

    def _all_panes(self) -> List[PlotPane]:
        out = []
        for i in range(self.panes_split.count()):
            w = self.panes_split.widget(i)
            if isinstance(w, PlotPane):
                out.append(w)
        out.extend(self._popouts.keys())
        return out

    def on_add_pane(self):
        pane = PlotPane()
        pane.remove_requested.connect(self._remove_pane)
        pane.maximize_requested.connect(self._toggle_maximize_pane)
        pane.popout_requested.connect(self._popout_pane)
        pane.set_threshold(self._threshold())
        if self.merged is not None and not self.merged.empty:
            pane.set_data(self.merged, self.chain_topics())
        self.panes_split.addWidget(pane)
        n = self.panes_split.count()
        ori = self.panes_split.orientation()
        ext = (self.panes_split.width() if ori == QtCore.Qt.Horizontal
               else self.panes_split.height())
        self.panes_split.setSizes([max(ext, 200) // n] * n)

    def _remove_pane(self, pane: PlotPane):
        if pane in self._popouts:
            self._popouts.pop(pane).close()
            return
        if self.panes_split.count() <= 1 and not self._popouts:
            QtWidgets.QMessageBox.information(
                self, "Keep at least one", "At least one pane must remain.")
            return
        if pane is self._maximized_pane:
            self._maximized_pane = None; self._pre_max_sizes = None
        pane.setParent(None); pane.deleteLater()

    def _toggle_maximize_pane(self, pane: PlotPane):
        if pane in self._popouts:
            win = self._popouts[pane]
            (win.showNormal if win.isMaximized() else win.showMaximized)()
            return
        n = self.panes_split.count()
        if self._maximized_pane is pane:
            if self._pre_max_sizes:
                self.panes_split.setSizes(self._pre_max_sizes)
            self._maximized_pane = None; self._pre_max_sizes = None
            return
        self._pre_max_sizes = self.panes_split.sizes()
        sizes = [10] * n
        for i in range(n):
            if self.panes_split.widget(i) is pane:
                sizes[i] = max(self.panes_split.height(), 600); break
        self.panes_split.setSizes(sizes)
        self._maximized_pane = pane

    def _popout_pane(self, pane: PlotPane):
        if pane in self._popouts:
            self._popouts[pane].raise_(); self._popouts[pane].activateWindow()
            return
        if pane is self._maximized_pane:
            self._maximized_pane = None
            if self._pre_max_sizes:
                self.panes_split.setSizes(self._pre_max_sizes)
            self._pre_max_sizes = None
        title = pane.hop_combo.currentText() or "Plot"
        win = PopoutWindow(pane, title=f"Plot — {title}")
        win.closed.connect(self._on_popout_closed)
        self._popouts[pane] = win
        win.show()
        if self.panes_split.count() == 0:
            self.on_add_pane()

    def _on_popout_closed(self, pane: PlotPane):
        if pane not in self._popouts:
            return
        del self._popouts[pane]
        pane.setParent(None)
        self.panes_split.addWidget(pane)

    # ----------------------------------------------------------- export ---
    def on_export_csv(self):
        if self.merged is None or self.merged.empty:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export latency CSV", "chain_latency.csv", "CSV (*.csv)")
        if not path:
            return
        out = self.merged.copy()
        out["t_source_iso"] = out["t_source_dt"].dt.strftime(
            "%Y-%m-%d %H:%M:%S.%f")
        out = out.drop(columns=["t_source_dt"])
        out.to_csv(path, index=False)
        self.status_message.emit(f"Wrote {len(out):,} rows to {path}")
