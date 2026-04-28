"""Small UI helpers shared by the tabs.

* :func:`info_icon` — an inline ``ⓘ`` label with a rich hover tooltip,
  used everywhere we want a one-line UI but a longer explanation on demand.
* :func:`set_header_tooltips` — attach per-column hover tooltips to a
  ``QTableWidget`` header without changing the visible label.
"""

from __future__ import annotations

from typing import Iterable

from PyQt5 import QtCore, QtWidgets


def info_icon(tooltip: str, parent: QtWidgets.QWidget | None = None
              ) -> QtWidgets.QLabel:
    """Return a clickable-looking ``ⓘ`` label whose hover shows *tooltip*."""
    lbl = QtWidgets.QLabel("ⓘ", parent)
    lbl.setToolTip(tooltip)
    lbl.setCursor(QtCore.Qt.WhatsThisCursor)
    lbl.setStyleSheet(
        "QLabel { color:#1976d2; font-weight:bold; padding:0 4px; }"
        "QLabel:hover { color:#0b4f97; }"
    )
    # Show the tooltip immediately and keep it open while the cursor stays.
    lbl.setAttribute(QtCore.Qt.WA_AlwaysShowToolTips, True)
    return lbl


def set_header_tooltips(table: QtWidgets.QTableWidget,
                        tooltips: Iterable[str | None]) -> None:
    """Attach a tooltip to each existing horizontal header item.

    Pass ``None`` for columns that should keep no tooltip. Extra entries
    beyond the column count are ignored; missing entries leave that
    column untouched.
    """
    for i, tip in enumerate(tooltips):
        if i >= table.columnCount():
            break
        if tip is None:
            continue
        item = table.horizontalHeaderItem(i)
        if item is None:
            item = QtWidgets.QTableWidgetItem(table.horizontalHeaderItem(i)
                                              .text() if False else "")
            table.setHorizontalHeaderItem(i, item)
        item.setToolTip(tip)
