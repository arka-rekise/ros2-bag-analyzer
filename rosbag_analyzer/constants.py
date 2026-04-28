"""Constants and small pure helpers used across the package."""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Cache directory for per-topic parsed timestamps.
# ---------------------------------------------------------------------------
CACHE_DIR: str = os.path.expanduser("~/.cache/bag_latency_gui")


# ---------------------------------------------------------------------------
# CDR encapsulation header layout for ROS 2 messages.
#
# Every serialised ROS 2 message starts with a 4-byte encapsulation header:
#   [0:2]  representation_identifier  (0x0001 = CDR_LE, 0x0001 swapped = BE)
#   [2:4]  options                    (usually 0)
# After those 4 bytes the message body begins. When the very first field is
# `std_msgs/Header`, the layout is:
#   [4:8]   sec     (int32)
#   [8:12]  nanosec (uint32)
# We exploit this to read header.stamp without invoking `deserialize_message`.
# ---------------------------------------------------------------------------
CDR_HEADER_BYTES: int = 4
HEADER_STAMP_FMT_LE: str = "<iI"
HEADER_STAMP_FMT_BE: str = ">iI"


# ---------------------------------------------------------------------------
# Colours used for overlay plots / multi-topic frequency plots.
# ---------------------------------------------------------------------------
PLOT_COLORS = [
    (31, 119, 180),  (255, 127, 14), (44, 160, 44),  (214, 39, 40),
    (148, 103, 189), (140, 86, 75),  (227, 119, 194), (127, 127, 127),
    (188, 189, 34),  (23, 190, 207),
]


def hop_label(i: int) -> str:
    """Letter label for the i-th hop: 0->A, 1->B, ..., 25->Z, 26->AA, ..."""
    if i < 26:
        return chr(ord("A") + i)
    return chr(ord("A") + (i // 26) - 1) + chr(ord("A") + (i % 26))
