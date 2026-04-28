"""Frequency-analysis tab: per-topic publish rate (Hz) over time."""

from __future__ import annotations

from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PyQt5 import QtCore, QtWidgets

from constants import PLOT_COLORS
from frequency import topic_rate_stats, topic_rates
from loader import ChainLoaderThread
from plotting import TimeAxisItem


class FrequencyTab(QtWidgets.QWidget):
    """Histogram each topic's bag timestamps into bins and plot Hz over time."""

    status_message = QtCore.pyqtSignal(str)

    def __init__(self, get_bag_meta_callable, parent=None):
        super().__init__(parent)
        self._get_bag = get_bag_meta_callable
        self.dfs: Dict[str, pd.DataFrame] = {}
        self.rates: Dict[str, np.ndarray] = {}
        self.centers_s: np.ndarray = np.empty(0, dtype=np.float64)
        self.loader: Optional[ChainLoaderThread] = None
        self._build()

    def _build(self):
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)

        split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        split.setHandleWidth(6); split.setChildrenCollapsible(False)
        v.addWidget(split, 1)

        # ---- top: topics + controls ----
        top_box = QtWidgets.QGroupBox("Topics for frequency analysis")
        tb = QtWidgets.QVBoxLayout(top_box)
        self.topic_list = QtWidgets.QListWidget()
        self.topic_list.setSelectionMode(
            QtWidgets.QAbstractItemView.ExtendedSelection)
        tb.addWidget(self.topic_list)

        btns = QtWidgets.QHBoxLayout()
        for label, cb in [("Remove", self._remove_selected),
                          ("Clear", self.topic_list.clear)]:
            b = QtWidgets.QPushButton(label); b.clicked.connect(cb); btns.addWidget(b)
        btns.addStretch()
        tb.addLayout(btns)

        ctrl = QtWidgets.QHBoxLayout()
        ctrl.addWidget(QtWidgets.QLabel("Bin width (s):"))
        self.bin_spin = QtWidgets.QDoubleSpinBox()
        self.bin_spin.setDecimals(3); self.bin_spin.setRange(0.001, 600.0)
        self.bin_spin.setValue(1.0)
        self.bin_spin.valueChanged.connect(self._recompute_from_dfs)
        ctrl.addWidget(self.bin_spin)
        ctrl.addSpacing(20)
        ctrl.addWidget(QtWidgets.QLabel("Smooth:"))
        self.smooth_spin = QtWidgets.QSpinBox()
        self.smooth_spin.setRange(1, 1000); self.smooth_spin.setValue(1)
        self.smooth_spin.setSuffix(" bins")
        self.smooth_spin.valueChanged.connect(self._refresh_plot)
        ctrl.addWidget(self.smooth_spin)
        ctrl.addSpacing(20)
        self.compute_btn = QtWidgets.QPushButton("Compute Frequencies  ▶")
        self.compute_btn.setStyleSheet("font-weight:bold;padding:6px;")
        self.compute_btn.clicked.connect(self.on_compute)
        ctrl.addWidget(self.compute_btn)
        ctrl.addStretch()
        tb.addLayout(ctrl)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setFormat("%p%  reading topics…")
        self.progress.setVisible(False)
        tb.addWidget(self.progress)
        split.addWidget(top_box)

        # ---- bottom: stats + plot ----
        bottom = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        bottom.setHandleWidth(6); bottom.setChildrenCollapsible(False)

        stats_box = QtWidgets.QGroupBox("Frequency stats (per topic)")
        stats_box.setMinimumHeight(80)
        sb = QtWidgets.QVBoxLayout(stats_box)
        self.stats_table = QtWidgets.QTableWidget(0, 8)
        self.stats_table.setHorizontalHeaderLabels(
            ["Topic", "msgs", "duration (s)", "mean Hz", "median Hz",
             "min Hz", "max Hz", "stddev Hz"])
        self.stats_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.stats_table.horizontalHeader().setStretchLastSection(True)
        sb.addWidget(self.stats_table)
        bottom.addWidget(stats_box)

        self.plot_widget = pg.PlotWidget(
            axisItems={"bottom": TimeAxisItem(orientation="bottom")})
        self.plot_widget.setBackground("w")
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self.plot_widget.setLabel("left", "Rate", units="Hz")
        self.plot_widget.setLabel("bottom", "Time")
        self.plot_widget.setDownsampling(auto=True, mode="peak")
        self.plot_widget.setClipToView(True)
        self.plot_widget.addLegend(offset=(10, 10))
        self.plot_widget.setMinimumHeight(140)
        bottom.addWidget(self.plot_widget)
        bottom.setStretchFactor(0, 0); bottom.setStretchFactor(1, 1)
        bottom.setSizes([220, 700])

        split.addWidget(bottom)
        split.setSizes([260, 700])

    # ----- public/topic ops -----
    def add_topic(self, topic: str, ttype: str):
        for i in range(self.topic_list.count()):
            if self.topic_list.item(i).text().split("    [")[0] == topic:
                return
        self.topic_list.addItem(f"{topic}    [{ttype}]")

    def _topics(self):
        return [self.topic_list.item(i).text().split("    [")[0]
                for i in range(self.topic_list.count())]

    def _remove_selected(self):
        for it in sorted(self.topic_list.selectedItems(),
                         key=lambda it: -self.topic_list.row(it)):
            self.topic_list.takeItem(self.topic_list.row(it))

    # ----- compute -----
    def on_compute(self):
        bag = self._get_bag()
        if not bag:
            QtWidgets.QMessageBox.information(self, "No bag", "Open a bag first.")
            return
        topics = self._topics()
        if not topics:
            QtWidgets.QMessageBox.information(
                self, "No topics", "Add at least one topic.")
            return
        for t in topics:
            if t not in bag.topics:
                QtWidgets.QMessageBox.warning(self, "Unknown topic", f"{t}")
                return
        self.compute_btn.setEnabled(False)
        self.progress.setVisible(True); self.progress.setRange(0, 0)
        self.status_message.emit("Reading topics for frequency analysis…")
        self.loader = ChainLoaderThread(bag, topics)
        self.loader.progress.connect(self._on_progress)
        self.loader.finished_ok.connect(self._on_done)
        self.loader.failed.connect(self._on_failed)
        self.loader.start()

    def _on_progress(self, pct: int, msg: str):
        if pct >= 0:
            self.progress.setRange(0, 100)
            self.progress.setValue(pct)
            short = msg.split(" | ", 1)[0]
            self.progress.setFormat(f"%p%  {short}")
        self.status_message.emit(msg)

    def _on_failed(self, msg: str):
        self.progress.setVisible(False); self.compute_btn.setEnabled(True)
        QtWidgets.QMessageBox.critical(self, "Load failed", msg)

    def _on_done(self, dfs: Dict[str, pd.DataFrame]):
        self.progress.setVisible(False); self.compute_btn.setEnabled(True)
        self.dfs = dfs
        self._recompute_from_dfs()

    def _recompute_from_dfs(self):
        if not self.dfs:
            return
        bin_s = self.bin_spin.value()
        rates, centers_s = topic_rates(self.dfs, bin_s)
        if centers_s.size == 0:
            QtWidgets.QMessageBox.warning(self, "No data", "Topics have no messages.")
            return
        self.rates = rates
        self.centers_s = centers_s

        self.stats_table.setRowCount(0)
        for t, df in self.dfs.items():
            stats = topic_rate_stats(df, rates[t])
            r = self.stats_table.rowCount()
            self.stats_table.insertRow(r)
            self.stats_table.setItem(r, 0, QtWidgets.QTableWidgetItem(t))
            self.stats_table.setItem(r, 1, QtWidgets.QTableWidgetItem(f"{stats['n']:,}"))
            for c, key in enumerate(
                    ["duration_s", "mean_hz", "median_hz",
                     "min_hz", "max_hz", "stddev_hz"], start=2):
                self.stats_table.setItem(
                    r, c, QtWidgets.QTableWidgetItem(f"{stats[key]:.3f}"))
        self.stats_table.resizeColumnsToContents()

        self._refresh_plot()
        self.status_message.emit(
            f"Frequency: {len(self.dfs)} topics, bin={bin_s}s, "
            f"{len(centers_s):,} bins.")

    def _refresh_plot(self):
        pw = self.plot_widget
        pw.clear()
        if self.centers_s.size == 0:
            return
        sm = max(1, self.smooth_spin.value())
        x = self.centers_s
        for ci, (t, y) in enumerate(self.rates.items()):
            if sm > 1 and y.size >= sm:
                y_plot = pd.Series(y).rolling(
                    sm, min_periods=1).mean().to_numpy()
            else:
                y_plot = y
            color = PLOT_COLORS[ci % len(PLOT_COLORS)]
            pw.plot(x, y_plot, pen=pg.mkPen(color=color, width=1), name=t)
        pw.enableAutoRange()
