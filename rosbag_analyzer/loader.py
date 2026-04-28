"""Background loader thread.

Reads a list of topics in parallel using a thread pool. Each worker:

* owns its own sqlite connection (sqlite connections are *not* thread-safe).
* reads its topic end-to-end into pre-allocated numpy buffers.
* writes the result to the per-topic cache.

Concurrency model
-----------------
* The orchestrator lives inside a ``QThread`` so the Qt event loop stays
  responsive.
* The workers are plain Python threads from ``ThreadPoolExecutor``. The
  Python GIL is released around sqlite I/O and around C-level numpy
  operations, so threads (not processes) give us linear speed-up while
  keeping the message classes — which contain non-picklable members —
  usable.
* The shared ``_progress`` dict (current ``n_read`` per topic) is guarded
  by a single ``threading.Lock``. This is the *only* lock in the system,
  so deadlocks are structurally impossible.
* Workers do not share a sqlite connection. SQLite uses file locks; with
  ``mode=ro`` URIs concurrent readers do not contend.

Progress reporting
------------------
Each worker reports ``(topic, n_read, status)`` via the callback. The
orchestrator computes a real percentage from
``sum(n_read) / sum(expected)`` and emits ``progress(pct, line)`` to the
GUI. ``line`` is also logged at INFO level so users running from a
terminal see what's happening.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

import pandas as pd
from PyQt5 import QtCore

from metadata import BagMetadata
from reader import read_topic
from ros_imports import import_ros

logger = logging.getLogger(__name__)


def _default_max_workers(n_topics: int) -> int:
    cores = os.cpu_count() or 4
    return max(1, min(n_topics, cores))


class ChainLoaderThread(QtCore.QThread):
    """Loads timestamps for ``chain_topics`` in parallel and emits the result."""

    # pct in [0, 100] for real progress, -1 if indeterminate
    progress = QtCore.pyqtSignal(int, str)
    finished_ok = QtCore.pyqtSignal(dict)         # {topic: DataFrame}
    failed = QtCore.pyqtSignal(str)

    def __init__(self,
                 bag_meta: BagMetadata,
                 chain_topics: List[str],
                 max_workers: Optional[int] = None) -> None:
        super().__init__()
        self.bag_meta = bag_meta
        self.chain_topics = list(chain_topics)
        self.max_workers = max_workers or _default_max_workers(len(chain_topics))
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        # Per-topic state: (n_read, status). status ∈ queued/reading/cached/done
        self._progress: Dict[str, Dict] = {
            t: {"n": 0, "status": "queued",
                "expected": bag_meta.counts.get(t, 0)}
            for t in chain_topics
        }
        self._last_emit = 0.0

    def cancel(self) -> None:
        self._cancel.set()

    def _on_worker_progress(self, topic: str, n_read: int, status: str) -> None:
        """Worker callback: aggregate and emit overall progress."""
        with self._lock:
            st = self._progress[topic]
            st["n"] = n_read
            st["status"] = status
            # If a topic was a cache hit, the "expected" was the metadata count
            # which can be slightly off (split topology mismatch); pin it to
            # the actual cached length so the bar reaches 100%.
            if status in ("cached", "done"):
                st["expected"] = max(st["expected"], n_read)

            total_n = sum(s["n"] for s in self._progress.values())
            total_expected = sum(
                max(s["expected"], s["n"]) for s in self._progress.values()
            ) or 1
            pct = int(min(100, round(100 * total_n / total_expected)))

            done_count = sum(
                1 for s in self._progress.values()
                if s["status"] in ("cached", "done")
            )
            line = (
                f"[{done_count}/{len(self._progress)} topics] "
                f"{total_n:,} / {total_expected:,} rows  ({pct}%)  | "
                + " · ".join(
                    f"{t.split('/')[-1]}: {self._progress[t]['n']:,}"
                    f"{'✓' if self._progress[t]['status'] in ('cached','done') else ''}"
                    for t in self.chain_topics
                )
            )
            now = time.time()
            should_emit = (now - self._last_emit) > 0.2 or status in (
                "cached", "done")
            if should_emit:
                self._last_emit = now

        if should_emit:
            self.progress.emit(pct, line)
            logger.info(line)

    def run(self) -> None:
        try:
            _, deserialize_message, get_message = import_ros()

            msg_classes = {}
            for t in self.chain_topics:
                ttype = self.bag_meta.topics.get(t)
                if not ttype:
                    raise RuntimeError(f"Unknown type for topic {t}")
                try:
                    msg_classes[t] = get_message(ttype)
                except Exception as e:
                    raise RuntimeError(
                        f"Cannot import message class {ttype} for topic {t}.\n"
                        f"Did you source the workspace that defines it?\n"
                        f"Underlying: {e}"
                    )

            start_msg = (f"Reading {len(self.chain_topics)} topics in parallel "
                         f"({self.max_workers} workers)…")
            logger.info(start_msg)
            self.progress.emit(0, start_msg)
            t0 = time.time()
            dfs: Dict[str, pd.DataFrame] = {}

            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                futures = {
                    pool.submit(
                        read_topic,
                        self.bag_meta,
                        topic,
                        msg_classes[topic],
                        deserialize_message,
                        self._on_worker_progress,
                    ): topic
                    for topic in self.chain_topics
                }
                for fut in as_completed(futures):
                    topic = futures[fut]
                    if self._cancel.is_set():
                        for f in futures:
                            f.cancel()
                        self.failed.emit("Cancelled by user")
                        logger.warning("Load cancelled by user")
                        return
                    dfs[topic] = fut.result()

            elapsed = time.time() - t0
            total = sum(len(v) for v in dfs.values())
            done_msg = (f"Loaded {total:,} msgs across {len(dfs)} topics "
                        f"in {elapsed:.1f}s")
            logger.info(done_msg)
            self.progress.emit(100, done_msg)
            self.finished_ok.emit(dfs)
        except Exception as e:
            tb = traceback.format_exc()
            logger.exception("Loader failed: %s", e)
            self.failed.emit(f"{e}\n\n{tb}")
