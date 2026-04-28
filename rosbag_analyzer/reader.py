"""Fast per-topic message-time reader.

Strategy
--------
For each topic we want two timestamps per message:

* ``t_bag_ns``        – when the bag wrote the row (sqlite ``timestamp`` col).
* ``header_stamp_ns`` – the original ``std_msgs/Header.stamp`` if the message
                         carries one, else ``-1``.

Two paths are used:

1. **CDR fast-path** — most ROS messages start with ``std_msgs/Header`` whose
   serialised layout, after the 4-byte CDR encapsulation header, is exactly
   ``int32 sec`` + ``uint32 nanosec``. We grab those 8 bytes with
   ``struct.unpack_from`` — **no Python deserialization at all**.

2. **Full deserialize fallback** — for messages that do not put ``header``
   first (or have no header), we call ``rclpy.serialization.deserialize_message``
   and read ``msg.header.stamp`` reflectively.

The first message of each topic is decoded **both** ways and compared. If they
agree, the rest of the topic uses the fast path; otherwise the slow path.

SQLite tuning
-------------
Each thread opens its own read-only connection (sqlite connections are not
thread-safe). We apply per-connection pragmas to maximise throughput:

* ``mmap_size``    — memory-map up to 8 GiB of the DB so reads come from page
                     cache instead of syscalls.
* ``cache_size``   — 256 MiB of page cache per connection.
* ``temp_store``   — keep temp btrees in RAM.
* ``query_only``   — defence in depth against any accidental writes.

Together these typically saturate disk read bandwidth on big bags.
"""

from __future__ import annotations

import os
import sqlite3
import struct
import time
import logging
from typing import Callable, List, Optional

import numpy as np
import pandas as pd

from cache import cache_path_for
from constants import CDR_HEADER_BYTES, HEADER_STAMP_FMT_BE, HEADER_STAMP_FMT_LE
from metadata import BagMetadata

logger = logging.getLogger(__name__)


# Progress callback signature: (topic, n_read, status) where status is
# one of "reading", "cached", "done".
ProgressCB = Callable[[str, int, str], None]


# Tunables
SQLITE_MMAP_BYTES = 8 * 1024 * 1024 * 1024     # 8 GiB
SQLITE_CACHE_PAGES = -262144                   # negative = KiB → 256 MiB
ROW_BATCH = 50_000                             # fetchmany batch size


def _tune_connection(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    try:
        cur.execute(f"PRAGMA mmap_size={SQLITE_MMAP_BYTES}")
        cur.execute(f"PRAGMA cache_size={SQLITE_CACHE_PAGES}")
        cur.execute("PRAGMA temp_store=MEMORY")
        cur.execute("PRAGMA query_only=1")
        cur.execute("PRAGMA synchronous=OFF")
    except sqlite3.DatabaseError:
        pass
    finally:
        cur.close()


def read_topic(
    bag_meta: BagMetadata,
    topic: str,
    msg_class,
    deserialize_message,
    progress_cb: Optional[ProgressCB] = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Read ``(t_bag_ns, header_stamp_ns)`` for ``topic`` across every split.

    Returns a DataFrame with columns ``t_bag_ns``, ``header_stamp_ns``,
    ``msg_index`` (0..N-1), sorted ascending by ``t_bag_ns``.

    ``progress_cb(topic, n_read, status)`` is called with status ∈
    {``"reading"``, ``"cached"``, ``"done"``}.
    """
    cache_p = cache_path_for(bag_meta.path, topic)
    if use_cache and os.path.exists(cache_p):
        try:
            df = pd.read_pickle(cache_p)
            if {"t_bag_ns", "header_stamp_ns"}.issubset(df.columns):
                df["msg_index"] = np.arange(len(df), dtype=np.int64)
                logger.info("%s: cache hit, %d rows", topic, len(df))
                if progress_cb:
                    progress_cb(topic, len(df), "cached")
                return df
        except Exception as e:
            logger.warning("cache read failed for %s: %s", topic, e)

    expected = bag_meta.counts.get(topic, 0)
    logger.info("%s: reading from sqlite (expected %s rows across %d splits)",
                topic, f"{expected:,}" if expected else "?",
                len(bag_meta.db_files))

    # Pre-allocate output arrays based on the metadata count where available.
    if expected and expected > 0:
        t_bag_arr = np.empty(expected, dtype=np.int64)
        stamp_arr = np.empty(expected, dtype=np.int64)
    else:
        t_bag_arr = np.empty(0, dtype=np.int64)
        stamp_arr = np.empty(0, dtype=np.int64)
    n_filled = 0

    fast_ok: Optional[bool] = None
    fmt = HEADER_STAMP_FMT_LE
    last_progress = time.time()

    def _grow(needed: int):
        nonlocal t_bag_arr, stamp_arr
        cur = t_bag_arr.size
        if needed <= cur:
            return
        new_cap = max(needed, cur * 2 if cur else 65536)
        t_bag_arr = np.resize(t_bag_arr, new_cap)
        stamp_arr = np.resize(stamp_arr, new_cap)

    for db_path in bag_meta.db_files:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
        _tune_connection(conn)
        try:
            row = conn.execute(
                "SELECT id FROM topics WHERE name=?", (topic,)
            ).fetchone()
            if not row:
                continue
            topic_id = row[0]
            cur = conn.execute(
                "SELECT timestamp, data FROM messages "
                "WHERE topic_id=? ORDER BY timestamp",
                (topic_id,),
            )
            while True:
                rows = cur.fetchmany(ROW_BATCH)
                if not rows:
                    break
                _grow(n_filled + len(rows))
                for t_bag, data in rows:
                    if fast_ok is None:
                        # Probe path: deserialise once, also try fast path.
                        deser_stamp = -1
                        try:
                            msg = deserialize_message(bytes(data), msg_class)
                            hdr = getattr(msg, "header", None)
                            if hdr is not None:
                                s = getattr(hdr, "stamp", None)
                                if s is not None:
                                    deser_stamp = (int(s.sec) * 1_000_000_000
                                                   + int(s.nanosec))
                        except Exception:
                            pass
                        if len(data) >= CDR_HEADER_BYTES + 8:
                            rep_id = bytes(data[0:2])
                            use_be = rep_id == b"\x00\x01"
                            fmt = HEADER_STAMP_FMT_BE if use_be else HEADER_STAMP_FMT_LE
                            try:
                                sec, nsec = struct.unpack_from(
                                    fmt, data, CDR_HEADER_BYTES)
                                fast_stamp = sec * 1_000_000_000 + nsec
                                fast_ok = (deser_stamp >= 0
                                           and fast_stamp == deser_stamp)
                            except struct.error:
                                fast_ok = False
                        else:
                            fast_ok = False
                        stamp = deser_stamp
                    elif fast_ok:
                        try:
                            sec, nsec = struct.unpack_from(
                                fmt, data, CDR_HEADER_BYTES)
                            stamp = sec * 1_000_000_000 + nsec
                        except struct.error:
                            stamp = -1
                    else:
                        try:
                            msg = deserialize_message(bytes(data), msg_class)
                            hdr = getattr(msg, "header", None)
                            if hdr is None:
                                stamp = -1
                            else:
                                s = getattr(hdr, "stamp", None)
                                stamp = (int(s.sec) * 1_000_000_000
                                         + int(s.nanosec)) if s else -1
                        except Exception:
                            stamp = -1
                    t_bag_arr[n_filled] = t_bag
                    stamp_arr[n_filled] = stamp
                    n_filled += 1

                if (time.time() - last_progress) > 0.4:
                    if progress_cb:
                        progress_cb(topic, n_filled, "reading")
                    if expected:
                        logger.debug("%s: %d / %d (%.1f%%)", topic,
                                     n_filled, expected,
                                     100.0 * n_filled / expected)
                    else:
                        logger.debug("%s: %d", topic, n_filled)
                    last_progress = time.time()
            cur.close()
        finally:
            conn.close()

    t_bag_arr = t_bag_arr[:n_filled]
    stamp_arr = stamp_arr[:n_filled]

    df = pd.DataFrame({"t_bag_ns": t_bag_arr, "header_stamp_ns": stamp_arr})
    df.sort_values("t_bag_ns", inplace=True, kind="mergesort")
    df.reset_index(drop=True, inplace=True)
    df["msg_index"] = np.arange(len(df), dtype=np.int64)

    try:
        df[["t_bag_ns", "header_stamp_ns"]].to_pickle(cache_p)
        logger.info("%s: wrote cache (%d rows, fast_path=%s)",
                    topic, len(df), bool(fast_ok))
    except Exception as e:
        logger.warning("cache write failed for %s: %s", topic, e)

    if progress_cb:
        progress_cb(topic, len(df), "done")
    return df
