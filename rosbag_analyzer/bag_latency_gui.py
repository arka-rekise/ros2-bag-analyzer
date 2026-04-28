#!/usr/bin/env python3
"""Entrypoint for the ROS 2 Bag Latency & Frequency Analyzer.

Run with:

    python3 bag_latency_gui.py
"""

import os
import sys

# Allow `python3 bag_latency_gui.py` from any cwd: ensure this directory is on
# sys.path so the sibling modules (ui_main, latency, ...) can be imported.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ui_main import main  # noqa: E402

if __name__ == "__main__":
    main()
