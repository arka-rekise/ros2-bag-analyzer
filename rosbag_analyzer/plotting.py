"""PyQtGraph plot widgets used by the GUI."""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PyQt5 import QtCore, QtGui, QtWidgets

from constants import PLOT_COLORS, hop_label as _lbl


def _fmt_lat(v_s: float) -> str:
    """Format a latency value (in seconds) using the most natural SI prefix.

    Used by the cursor readout so its label stays readable regardless of
    zoom level — ``2 ns``, ``500 µs``, ``12.3 ms``, ``1.7 s``, etc.
    """
    a = abs(v_s)
    if a >= 1.0:
        return f"{v_s:.3f} s"
    if a >= 1e-3:
        return f"{v_s * 1e3:.3f} ms"
    if a >= 1e-6:
        return f"{v_s * 1e6:.3f} µs"
    return f"{v_s * 1e9:.0f} ns"


class TimeAxisItem(pg.AxisItem):
    """Axis that formats Unix-seconds floats as 'Apr 15 11:41:23'."""

    def tickStrings(self, values, scale, spacing):
        out = []
        for v in values:
            try:
                dt = datetime.fromtimestamp(v)
                if spacing >= 86400:
                    out.append(dt.strftime("%b %d"))
                elif spacing >= 60:
                    out.append(dt.strftime("%b %d %H:%M"))
                else:
                    out.append(dt.strftime("%b %d %H:%M:%S"))
            except (ValueError, OSError, OverflowError):
                out.append("")
        return out


class PopoutWindow(QtWidgets.QMainWindow):
    """Free-floating wrapper around a single PlotPane."""

    closed = QtCore.pyqtSignal(object)  # emits the wrapped pane

    def __init__(self, pane: "PlotPane", title: str):
        super().__init__()
        self.setWindowTitle(title)
        self.resize(1200, 700)
        self.pane = pane
        self.setCentralWidget(pane)

    def closeEvent(self, event):
        self.closed.emit(self.pane)
        super().closeEvent(event)


class PlotPane(QtWidgets.QFrame):
    """One latency plot with type/X/hop selectors, threshold line, popout."""

    PLOT_TYPES = ["Line", "Line + markers", "Scatter",
                  "Histogram", "CDF", "Rolling mean"]

    remove_requested = QtCore.pyqtSignal(object)
    maximize_requested = QtCore.pyqtSignal(object)
    popout_requested = QtCore.pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self._merged: Optional[pd.DataFrame] = None
        self._chain: List[str] = []
        self._threshold_ms: Optional[float] = None

        # ---- top control bar ----
        top = QtWidgets.QHBoxLayout()
        top.setContentsMargins(4, 4, 4, 0)

        top.addWidget(QtWidgets.QLabel("Plot:"))
        self.type_combo = QtWidgets.QComboBox()
        self.type_combo.addItems(self.PLOT_TYPES)
        self.type_combo.currentIndexChanged.connect(self._refresh_plot)
        top.addWidget(self.type_combo)

        top.addSpacing(10)
        top.addWidget(QtWidgets.QLabel("X:"))
        self.x_combo = QtWidgets.QComboBox()
        self.x_combo.addItems(["Time (bag)", "Message index"])
        self.x_combo.currentIndexChanged.connect(self._refresh_plot)
        top.addWidget(self.x_combo)

        top.addSpacing(10)
        top.addWidget(QtWidgets.QLabel("Hop:"))
        self.hop_combo = QtWidgets.QComboBox()
        self.hop_combo.currentIndexChanged.connect(self._refresh_plot)
        top.addWidget(self.hop_combo, stretch=1)

        top.addSpacing(10)
        self.log_check = QtWidgets.QCheckBox("Y log")
        self.log_check.toggled.connect(self._refresh_plot)
        top.addWidget(self.log_check)

        top.addWidget(QtWidgets.QLabel("Window:"))
        self.window_spin = QtWidgets.QSpinBox()
        self.window_spin.setRange(2, 100000)
        self.window_spin.setValue(100)
        self.window_spin.setSuffix(" pts")
        self.window_spin.setToolTip("Rolling-mean window / histogram bins")
        self.window_spin.valueChanged.connect(self._refresh_plot)
        top.addWidget(self.window_spin)

        for tip, sym, sig in (
            ("Maximize / restore", "⛶", "maximize_requested"),
            ("Pop out to a free-floating window", "⇱", "popout_requested"),
            ("Close this pane", "✕", "remove_requested"),
        ):
            b = QtWidgets.QPushButton(sym)
            b.setFixedWidth(28)
            b.setToolTip(tip)
            b.clicked.connect(
                lambda _=False, s=sig: getattr(self, s).emit(self))
            top.addWidget(b)

        # ---- plot widget ----
        self.plot_widget = pg.PlotWidget(
            axisItems={"bottom": TimeAxisItem(orientation="bottom")})
        self.plot_widget.setBackground("w")
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        # Use seconds as the canonical axis unit so PyQtGraph's SI auto-prefix
        # picks the right prefix dynamically as you zoom (ns / µs / ms / s).
        # Internally we store latency in ms; we divide by 1000 at plot time.
        self.plot_widget.setLabel("left", "Latency", units="s")
        self.plot_widget.setLabel("bottom", "Time")
        self.plot_widget.setDownsampling(auto=True, mode="peak")
        self.plot_widget.setClipToView(True)
        self.plot_widget.addLegend(offset=(10, 10))

        # crosshair
        self._vline = pg.InfiniteLine(angle=90, pen=pg.mkPen("#888", width=1))
        self._hline = pg.InfiniteLine(angle=0, pen=pg.mkPen("#888", width=1))
        self._vline.setZValue(1000); self._hline.setZValue(1000)
        self._cursor_label = pg.TextItem(color="#222", anchor=(0, 1))
        self._cursor_label.setZValue(1001)
        self.plot_widget.addItem(self._vline, ignoreBounds=True)
        self.plot_widget.addItem(self._hline, ignoreBounds=True)
        self.plot_widget.addItem(self._cursor_label, ignoreBounds=True)
        self.plot_widget.scene().sigMouseMoved.connect(self._on_mouse_moved)

        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(2, 2, 2, 2)
        v.addLayout(top)
        v.addWidget(self.plot_widget)
        self.setMinimumHeight(140)
        self.setMinimumWidth(280)

    # ---- public ----
    def set_threshold(self, ms: Optional[float]) -> None:
        self._threshold_ms = ms
        self._refresh_plot()

    def set_data(self, merged: pd.DataFrame, chain: List[str]) -> None:
        self._merged = merged
        self._chain = chain
        # Build the list of plot selections. Each entry is
        # (display_name, [(col, legend_label), ...]).
        items: List[Tuple[str, List[Tuple[str, str]]]] = []
        n_hops = len(chain) - 1

        # ---- Pipeline (rosbag-based) ----
        for i in range(n_hops):
            a, b = _lbl(i), _lbl(i + 1)
            items.append((f"[Pipe] {a}→{b}: {chain[i]}  →  {chain[i+1]}",
                          [(f"lat_{a}_{b}_ms", f"{a}→{b}")]))
        if n_hops >= 1:
            items.append(
                (f"[Pipe] Total: {chain[0]}  →  {chain[-1]}",
                 [("lat_total_ms", "pipe total")]))
            items.append(
                ("[Pipe] All hops (overlay)",
                 [(f"lat_{_lbl(i)}_{_lbl(i+1)}_ms",
                   f"{_lbl(i)}→{_lbl(i+1)}") for i in range(n_hops)]
                 + [("lat_total_ms", "pipe total")]))

        # ---- True (header-based), only if available ----
        has_true = "lat_src_ms" in merged.columns
        if has_true:
            items.append(
                (f"[True] Source delay @ {_lbl(0)} "
                 f"(header.stamp → t_bag of {chain[0]})",
                 [("lat_src_ms", "src delay")]))
            for i in range(1, len(chain)):
                col = f"lat_true_{_lbl(i)}_ms"
                if col not in merged.columns:
                    continue
                if i == n_hops:
                    label = (f"[True] End-to-end: "
                             f"{chain[0]}.header → {chain[-1]}")
                    leg = "true e2e"
                else:
                    label = (f"[True] @ {_lbl(i)}: "
                             f"{chain[0]}.header → {chain[i]}")
                    leg = f"true @ {_lbl(i)}"
                items.append((label, [(col, leg)]))
            true_overlay = [("lat_src_ms", "src delay")] + [
                (f"lat_true_{_lbl(i)}_ms", f"true @ {_lbl(i)}")
                for i in range(1, len(chain))
                if f"lat_true_{_lbl(i)}_ms" in merged.columns]
            items.append(("[True] All true (overlay)", true_overlay))
            if n_hops >= 1:
                items.append(
                    ("[Pipe+True] Compare end-to-end",
                     [("lat_total_ms", "pipe total"),
                      ("lat_true_total_ms", "true e2e")]))

        self._items = items
        self.hop_combo.blockSignals(True)
        self.hop_combo.clear()
        for name, _ in items:
            self.hop_combo.addItem(name)
        self.hop_combo.blockSignals(False)
        self.hop_combo.setCurrentIndex(0)
        self._refresh_plot()

    # ---- internals ----
    def _y_columns(self) -> List[Tuple[str, str]]:
        if not getattr(self, "_items", None):
            return []
        idx = self.hop_combo.currentIndex()
        if idx < 0 or idx >= len(self._items):
            return []
        return self._items[idx][1]

    def _on_mouse_moved(self, pos):
        vb = self.plot_widget.getPlotItem().vb
        if not self.plot_widget.sceneBoundingRect().contains(pos):
            return
        mp = vb.mapSceneToView(pos)
        x, y = mp.x(), mp.y()
        self._vline.setPos(x); self._hline.setPos(y)
        ptype = self.type_combo.currentText()
        x_mode = self.x_combo.currentIndex()
        # y is in seconds on time/index plots and on CDFs/histograms.
        if ptype in ("Histogram", "CDF"):
            txt = f"x={_fmt_lat(x)}\ny={y:.3f}"
        elif x_mode == 0:
            try:
                ts = datetime.fromtimestamp(x).strftime("%b %d %H:%M:%S.%f")[:-3]
            except (ValueError, OSError, OverflowError):
                ts = f"{x:.3f}"
            txt = f"{ts}\n{_fmt_lat(y)}"
        else:
            txt = f"idx={int(x)}\n{_fmt_lat(y)}"
        self._cursor_label.setText(txt)
        self._cursor_label.setPos(x, y)

    def _refresh_plot(self):
        pw = self.plot_widget
        pw.clear()
        # PlotWidget.clear() removes everything — re-add crosshair items.
        pw.addItem(self._vline, ignoreBounds=True)
        pw.addItem(self._hline, ignoreBounds=True)
        pw.addItem(self._cursor_label, ignoreBounds=True)
        if self._merged is None or self._merged.empty or not self._chain:
            return

        cols = self._y_columns()
        if not cols:
            return

        ptype = self.type_combo.currentText()
        x_mode = self.x_combo.currentIndex()

        # All latency axes use SI base unit "s"; PyQtGraph then auto-prefixes
        # to ns / µs / ms / s as the user zooms in or out.
        if ptype == "Histogram":
            pw.setAxisItems({"bottom": pg.AxisItem(orientation="bottom")})
            pw.setLabel("bottom", "Latency", units="s")
            pw.setLabel("left", "Count")
        elif ptype == "CDF":
            pw.setAxisItems({"bottom": pg.AxisItem(orientation="bottom")})
            pw.setLabel("bottom", "Latency", units="s")
            pw.setLabel("left", "CDF")
        else:
            if x_mode == 0:
                pw.setAxisItems({"bottom": TimeAxisItem(orientation="bottom")})
                pw.setLabel("bottom", "Time")
            else:
                pw.setAxisItems({"bottom": pg.AxisItem(orientation="bottom")})
                pw.setLabel("bottom", "Message index (matched chain)")
            pw.setLabel("left", "Latency", units="s")

        pw.setLogMode(
            x=False,
            y=self.log_check.isChecked() and ptype not in ("Histogram", "CDF"),
        )

        for ci, (ycol, label) in enumerate(cols):
            if ycol not in self._merged.columns:
                continue
            # Stored values are ms; convert to seconds for plotting so that
            # PyQtGraph's auto-SI prefix on the axis works correctly.
            y_s = self._merged[ycol].to_numpy() / 1000.0
            color = PLOT_COLORS[ci % len(PLOT_COLORS)]
            pen = pg.mkPen(color=color, width=1)

            if ptype == "Histogram":
                bins = max(10, self.window_spin.value())
                yfin = y_s[np.isfinite(y_s)]
                if yfin.size == 0:
                    continue
                hist, edges = np.histogram(yfin, bins=bins)
                centers = (edges[:-1] + edges[1:]) / 2
                width = edges[1] - edges[0] if len(edges) > 1 else 1.0
                pw.addItem(pg.BarGraphItem(
                    x=centers, height=hist, width=width * 0.9,
                    brush=pg.mkBrush(*color, 160), pen=pen, name=label))
            elif ptype == "CDF":
                yfin = np.sort(y_s[np.isfinite(y_s)])
                if yfin.size == 0:
                    continue
                cdf = np.linspace(0, 1, yfin.size, endpoint=True)
                pw.plot(yfin, cdf, pen=pen, name=label)
            else:
                if x_mode == 0:
                    x = self._merged[f"t_{_lbl(0)}_ns"].to_numpy() / 1e9
                else:
                    x = self._merged["seq_index"].to_numpy()

                if ptype == "Rolling mean":
                    w = max(2, self.window_spin.value())
                    if y_s.size >= w:
                        y_plot = pd.Series(y_s).rolling(
                            w, min_periods=1).mean().to_numpy()
                    else:
                        y_plot = y_s
                    pw.plot(x, y_plot, pen=pen,
                            name=f"{label} (w={self.window_spin.value()})")
                elif ptype == "Scatter":
                    pw.plot(x, y_s, pen=None, symbol="o", symbolSize=3,
                            symbolBrush=color, symbolPen=None, name=label)
                elif ptype == "Line + markers":
                    pw.plot(x, y_s, pen=pen, symbol="o", symbolSize=3,
                            symbolBrush=color, symbolPen=None, name=label)
                else:  # Line
                    pw.plot(x, y_s, pen=pen, name=label)

        if (self._threshold_ms is not None and self._threshold_ms > 0
                and ptype not in ("Histogram", "CDF")):
            line = pg.InfiniteLine(
                pos=self._threshold_ms / 1000.0, angle=0,
                pen=pg.mkPen("#d62728", width=2,
                             style=QtCore.Qt.DashLine),
                label=f"SLA {_fmt_lat(self._threshold_ms / 1000.0)}",
                labelOpts={"position": 0.95, "color": "#d62728",
                           "fill": (255, 255, 255, 200)},
            )
            line.setZValue(900)
            pw.addItem(line, ignoreBounds=True)

        pw.setTitle(cols[0][1] if len(cols) == 1 else "All hops")
        pw.enableAutoRange()
