"""ROS 2 bag metadata loader.

Reads `metadata.yaml` and the per-split sqlite files to discover topics, types,
counts, and the list of `.db3` files. No message payload is touched.
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass, field
from typing import Dict, List

from ros_imports import import_ros


@dataclass
class BagMetadata:
    """Lightweight summary of a ROS 2 bag."""

    path: str
    topics: Dict[str, str] = field(default_factory=dict)   # name -> type
    counts: Dict[str, int] = field(default_factory=dict)   # name -> count
    duration_s: float = 0.0
    start_ns: int = 0
    end_ns: int = 0
    message_total: int = 0
    storage_id: str = "sqlite3"
    db_files: List[str] = field(default_factory=list)

    @staticmethod
    def from_path(path: str) -> "BagMetadata":
        bm = BagMetadata(path=path)
        meta_yaml = os.path.join(path, "metadata.yaml")

        if os.path.exists(meta_yaml):
            try:
                import yaml
                with open(meta_yaml) as f:
                    meta = yaml.safe_load(f)
                info = meta.get("rosbag2_bagfile_information", {})
                bm.storage_id = info.get("storage_identifier", "sqlite3")
                bm.message_total = info.get("message_count", 0)
                dur = info.get("duration", {}).get("nanoseconds", 0)
                bm.duration_s = dur / 1e9
                start = info.get("starting_time", {}).get(
                    "nanoseconds_since_epoch", 0)
                bm.start_ns = start
                bm.end_ns = start + dur
                for tinfo in info.get("topics_with_message_count", []):
                    md = tinfo.get("topic_metadata", {})
                    bm.topics[md["name"]] = md["type"]
                    bm.counts[md["name"]] = tinfo.get("message_count", 0)
                for rf in info.get("relative_file_paths", []) or []:
                    abs_p = rf if os.path.isabs(rf) else os.path.join(path, rf)
                    if os.path.exists(abs_p):
                        bm.db_files.append(abs_p)
            except Exception as e:
                print(f"[warn] could not parse metadata.yaml: {e}")

        if not bm.db_files:
            bm.db_files = sorted(glob.glob(os.path.join(path, "*.db3")))

        # Fallback: probe via rosbag2_py if metadata.yaml gave us nothing.
        if not bm.topics:
            try:
                rosbag2_py, _, _ = import_ros()
                reader = rosbag2_py.SequentialReader()
                reader.open(
                    rosbag2_py.StorageOptions(uri=path, storage_id=bm.storage_id),
                    rosbag2_py.ConverterOptions("", ""),
                )
                for t in reader.get_all_topics_and_types():
                    bm.topics.setdefault(t.name, t.type)
                    bm.counts.setdefault(t.name, 0)
                del reader
            except Exception as e:
                print(f"[warn] rosbag2_py probe failed: {e}")

        return bm
