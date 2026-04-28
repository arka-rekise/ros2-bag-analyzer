"""Per-topic disk cache.

Each `(bag_path, topic, mtime)` triple is hashed into a stable filename. The
cache stores a pickled DataFrame with the columns produced by `reader`.
Re-loading the same chain a second time costs only the parquet/pickle read.
"""

from __future__ import annotations

import glob
import hashlib
import os
from typing import List

from constants import CACHE_DIR


def _bag_mtime(bag_path: str) -> int:
    """Latest mtime across all .db3 files; used as part of the cache key so that
    re-recording the bag invalidates the cache automatically."""
    mtime = 0
    try:
        for f in glob.glob(os.path.join(bag_path, "*.db3")):
            mtime = max(mtime, int(os.path.getmtime(f)))
    except OSError:
        pass
    return mtime


def cache_path_for(bag_path: str, topic: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    bag_abs = os.path.abspath(bag_path)
    h = hashlib.sha1(
        f"{bag_abs}|{topic}|{_bag_mtime(bag_abs)}".encode()
    ).hexdigest()[:16]
    safe_topic = topic.strip("/").replace("/", "_") or "root"
    return os.path.join(CACHE_DIR, f"{safe_topic}__{h}.pkl")


def clear_cache() -> int:
    """Remove all cached topic files. Returns the number of files removed."""
    n = 0
    for pat in ("*.pkl", "*.parquet"):
        for f in glob.glob(os.path.join(CACHE_DIR, pat)):
            try:
                os.remove(f)
                n += 1
            except OSError:
                pass
    return n


def list_cached() -> List[str]:
    return sorted(glob.glob(os.path.join(CACHE_DIR, "*.pkl")))
