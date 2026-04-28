"""Top-level GUI: bag loading, shared topic table, tabbed analyses."""

from __future__ import annotations

import logging
import os
import sys
import traceback
from datetime import datetime
from typing import List, Optional

import pyqtgraph as pg
from PyQt5 import QtCore, QtWidgets

import log_setup
from cache import CACHE_DIR, clear_cache
from metadata import BagMetadata
from ui_analysis_tab import AnalysisTab
from ui_frequency_tab import FrequencyTab

logger = logging.getLogger(__name__)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ROS 2 Bag Latency Analyzer")
        self.resize(1700, 1000)

        self.bag_meta: Optional[BagMetadata] = None
        self._tab_counter = 0
        self._settings = QtCore.QSettings("rosbag-analyzer", "bag_latency_gui")

        self._build_ui()
        self._add_analysis_tab()

        geom = self._settings.value("window/geometry")
        if geom is not None:
            try:
                self.restoreGeometry(geom)
            except Exception:
                pass

    # ----------------------------------------------------------------- UI ---
    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(6, 6, 6, 6)

        # -- top toolbar --
        top = QtWidgets.QHBoxLayout()
        self.open_btn = QtWidgets.QToolButton()
        self.open_btn.setText("Open Bag Folder…")
        self.open_btn.setPopupMode(QtWidgets.QToolButton.MenuButtonPopup)
        self.open_btn.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
        self.open_btn.clicked.connect(self.on_open_bag)
        self.recent_menu = QtWidgets.QMenu(self.open_btn)
        self.open_btn.setMenu(self.recent_menu)
        self._rebuild_recent_menu()
        top.addWidget(self.open_btn)

        self.bag_label = QtWidgets.QLabel("No bag loaded")
        self.bag_label.setStyleSheet("font-weight:bold;")
        top.addWidget(self.bag_label, stretch=1)

        self.clear_cache_btn = QtWidgets.QPushButton("Clear cache")
        self.clear_cache_btn.setToolTip(f"Delete all cache files in {CACHE_DIR}")
        self.clear_cache_btn.clicked.connect(self.on_clear_cache)
        top.addWidget(self.clear_cache_btn)
        root.addLayout(top)

        self.summary_label = QtWidgets.QLabel("")
        self.summary_label.setStyleSheet("color:#555;")
        root.addWidget(self.summary_label)

        # -- horizontal splitter: topics | tabs --
        h_split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        h_split.setHandleWidth(6); h_split.setChildrenCollapsible(False)
        root.addWidget(h_split, stretch=1)

        topic_box = QtWidgets.QGroupBox("Topics in bag")
        tb = QtWidgets.QVBoxLayout(topic_box)
        self.filter_edit = QtWidgets.QLineEdit()
        self.filter_edit.setPlaceholderText("Filter topics (substring)…")
        self.filter_edit.textChanged.connect(self._filter_topics)
        tb.addWidget(self.filter_edit)

        self.topic_table = QtWidgets.QTableWidget(0, 3)
        self.topic_table.setHorizontalHeaderLabels(["Topic", "Type", "Count"])
        h = self.topic_table.horizontalHeader()
        h.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        h.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        self.topic_table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectRows)
        self.topic_table.setEditTriggers(
            QtWidgets.QAbstractItemView.NoEditTriggers)
        self.topic_table.setSortingEnabled(True)
        self.topic_table.doubleClicked.connect(self._add_selected_topic_to_tab)
        tb.addWidget(self.topic_table)

        add_btn = QtWidgets.QPushButton("Add selected ➜ active tab")
        add_btn.setToolTip(
            "Append selected topic(s) to the active analysis tab.")
        add_btn.clicked.connect(self._add_selected_topic_to_tab)
        tb.addWidget(add_btn)
        h_split.addWidget(topic_box)

        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)

        tabs_row = QtWidgets.QHBoxLayout()
        self.new_tab_btn = QtWidgets.QPushButton("➕ Latency Analysis")
        self.new_tab_btn.clicked.connect(lambda: self._add_analysis_tab())
        self.new_freq_btn = QtWidgets.QPushButton("➕ Frequency Analysis")
        self.new_freq_btn.clicked.connect(lambda: self._add_frequency_tab())
        self.dup_tab_btn = QtWidgets.QPushButton("⎘ Duplicate")
        self.dup_tab_btn.clicked.connect(self._duplicate_active_tab)
        self.rename_tab_btn = QtWidgets.QPushButton("✎ Rename")
        self.rename_tab_btn.clicked.connect(self._rename_active_tab)
        for b in (self.new_tab_btn, self.new_freq_btn,
                  self.dup_tab_btn, self.rename_tab_btn):
            tabs_row.addWidget(b)
        tabs_row.addStretch()
        rv.addLayout(tabs_row)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        rv.addWidget(self.tabs, stretch=1)
        h_split.addWidget(right)
        h_split.setSizes([420, 1280])

        self.statusBar().showMessage("Ready")

    # ---------------------------------------------------------- bag ops ---
    def _get_bag_meta(self) -> Optional[BagMetadata]:
        return self.bag_meta

    def on_open_bag(self):
        recents = self._recent_bags()
        start_dir = os.path.dirname(recents[0]) if recents else os.path.expanduser("~")
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select ROS 2 bag folder (containing metadata.yaml)", start_dir)
        if not path:
            return
        self._open_bag_path(path)

    def _open_bag_path(self, path: str):
        if not os.path.exists(os.path.join(path, "metadata.yaml")):
            QtWidgets.QMessageBox.warning(
                self, "Not a bag", f"{path}\nNo metadata.yaml in this folder.")
            return
        logger.info("Opening bag: %s", path)
        self.statusBar().showMessage(f"Opening bag {path}…")
        QtWidgets.QApplication.processEvents()
        try:
            self.bag_meta = BagMetadata.from_path(path)
            logger.info("Bag loaded: %d topics, %d splits, %s msgs total",
                        len(self.bag_meta.topics),
                        len(self.bag_meta.db_files),
                        f"{self.bag_meta.message_total:,}")
        except Exception as e:
            QtWidgets.QMessageBox.critical(
                self, "Failed to open bag",
                f"{e}\n\n{traceback.format_exc()}")
            self.statusBar().showMessage("Failed to open bag")
            return

        self._push_recent_bag(path)
        self.bag_label.setText(path)
        if self.bag_meta.duration_s > 0:
            dur_h = self.bag_meta.duration_s / 3600.0
            t0 = datetime.fromtimestamp(self.bag_meta.start_ns / 1e9
                                        ).strftime("%b %d %Y %H:%M:%S")
            t1 = datetime.fromtimestamp(self.bag_meta.end_ns / 1e9
                                        ).strftime("%b %d %Y %H:%M:%S")
            self.summary_label.setText(
                f"Storage: {self.bag_meta.storage_id}    |    "
                f"Splits: {len(self.bag_meta.db_files)}    |    "
                f"Topics: {len(self.bag_meta.topics)}    |    "
                f"Messages: {self.bag_meta.message_total:,}    |    "
                f"Duration: {dur_h:.2f} h    |    "
                f"{t0}  →  {t1}")
        else:
            self.summary_label.setText(
                f"Storage: {self.bag_meta.storage_id}    |    "
                f"Topics: {len(self.bag_meta.topics)}")
        self._populate_topic_table()
        self.statusBar().showMessage(
            "Bag loaded. Select chain topics, then Compute.")

    def on_clear_cache(self):
        n = clear_cache()
        self.statusBar().showMessage(f"Cleared {n} cache files from {CACHE_DIR}")

    def _populate_topic_table(self):
        self.topic_table.setSortingEnabled(False)
        self.topic_table.setRowCount(0)
        for topic in sorted(self.bag_meta.topics.keys()):
            ttype = self.bag_meta.topics[topic]
            count = self.bag_meta.counts.get(topic, 0)
            r = self.topic_table.rowCount()
            self.topic_table.insertRow(r)
            self.topic_table.setItem(r, 0, QtWidgets.QTableWidgetItem(topic))
            self.topic_table.setItem(r, 1, QtWidgets.QTableWidgetItem(ttype))
            it = QtWidgets.QTableWidgetItem()
            it.setData(QtCore.Qt.DisplayRole, int(count))
            self.topic_table.setItem(r, 2, it)
        self.topic_table.setSortingEnabled(True)
        self.topic_table.sortByColumn(0, QtCore.Qt.AscendingOrder)

    def _filter_topics(self, text: str):
        text = text.lower().strip()
        for r in range(self.topic_table.rowCount()):
            topic = self.topic_table.item(r, 0).text().lower()
            self.topic_table.setRowHidden(r, text not in topic)

    def _add_selected_topic_to_tab(self):
        tab = self.tabs.currentWidget()
        if not isinstance(tab, (AnalysisTab, FrequencyTab)):
            return
        rows = sorted({i.row() for i in self.topic_table.selectedIndexes()})
        for r in rows:
            topic = self.topic_table.item(r, 0).text()
            ttype = self.topic_table.item(r, 1).text()
            tab.add_topic(topic, ttype)

    # --------------------------------------------------------- tab mgmt ---
    def _add_analysis_tab(self, name: Optional[str] = None) -> AnalysisTab:
        self._tab_counter += 1
        tab = AnalysisTab(self._get_bag_meta)
        tab.status_message.connect(self.statusBar().showMessage)
        title = name or f"Latency {self._tab_counter}"
        self.tabs.addTab(tab, title)
        self.tabs.setCurrentWidget(tab)
        return tab

    def _add_frequency_tab(self, name: Optional[str] = None) -> FrequencyTab:
        self._tab_counter += 1
        tab = FrequencyTab(self._get_bag_meta)
        tab.status_message.connect(self.statusBar().showMessage)
        title = name or f"Frequency {self._tab_counter}"
        self.tabs.addTab(tab, title)
        self.tabs.setCurrentWidget(tab)
        return tab

    def _close_tab(self, index: int):
        if self.tabs.count() <= 1:
            QtWidgets.QMessageBox.information(
                self, "Keep at least one", "At least one tab must remain.")
            return
        w = self.tabs.widget(index)
        self.tabs.removeTab(index)
        if w is not None:
            w.deleteLater()

    def _duplicate_active_tab(self):
        cur = self.tabs.currentWidget()
        if cur is None:
            return
        title = self.tabs.tabText(self.tabs.indexOf(cur)) + " (copy)"
        if isinstance(cur, AnalysisTab):
            new = self._add_analysis_tab(title)
            new.tolerance_spin.setValue(cur.tolerance_spin.value())
            new.threshold_spin.setValue(cur.threshold_spin.value())
            for i in range(cur.chain_list.count()):
                new.chain_list.addItem(cur.chain_list.item(i).text())
        elif isinstance(cur, FrequencyTab):
            new = self._add_frequency_tab(title)
            new.bin_spin.setValue(cur.bin_spin.value())
            new.smooth_spin.setValue(cur.smooth_spin.value())
            for i in range(cur.topic_list.count()):
                new.topic_list.addItem(cur.topic_list.item(i).text())

    def _rename_active_tab(self):
        idx = self.tabs.currentIndex()
        if idx < 0:
            return
        cur_name = self.tabs.tabText(idx)
        new_name, ok = QtWidgets.QInputDialog.getText(
            self, "Rename analysis", "Name:", text=cur_name)
        if ok and new_name.strip():
            self.tabs.setTabText(idx, new_name.strip())

    # ------------------------------------------------------ recent bags ---
    def _recent_bags(self) -> List[str]:
        v = self._settings.value("recent_bags", [])
        if isinstance(v, str):
            v = [v] if v else []
        return [p for p in (v or []) if p and os.path.isdir(p)]

    def _push_recent_bag(self, path: str, max_n: int = 10):
        path = os.path.abspath(path)
        existing = [p for p in self._recent_bags() if p != path]
        existing.insert(0, path)
        self._settings.setValue("recent_bags", existing[:max_n])
        self._rebuild_recent_menu()

    def _rebuild_recent_menu(self):
        self.recent_menu.clear()
        recents = self._recent_bags()
        if not recents:
            act = self.recent_menu.addAction("(no recent bags)")
            act.setEnabled(False)
            return
        for p in recents:
            act = self.recent_menu.addAction(p)
            act.triggered.connect(lambda _=False, pp=p: self._open_bag_path(pp))
        self.recent_menu.addSeparator()
        clear = self.recent_menu.addAction("Clear recent")
        clear.triggered.connect(
            lambda: (self._settings.setValue("recent_bags", []),
                     self._rebuild_recent_menu()))

    def closeEvent(self, event):
        self._settings.setValue("window/geometry", self.saveGeometry())
        super().closeEvent(event)


def main():
    log_setup.configure()
    logger.info("Starting ROS 2 Bag Latency Analyzer "
                "(set BAG_ANALYZER_LOG=DEBUG for verbose output)")
    app = QtWidgets.QApplication(sys.argv)
    pg.setConfigOptions(antialias=False, useOpenGL=False)
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())
