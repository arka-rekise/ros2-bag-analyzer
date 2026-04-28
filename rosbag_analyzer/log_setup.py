"""Centralised logging setup for the analyzer.

Call :func:`configure` once at startup. Every module then uses
``logging.getLogger(__name__)`` and the messages flow to stderr with a
timestamp + level + module prefix.

Example output:

    20:14:01.823 INFO  loader  Reading 3 topics in parallel (16 workers)
    20:14:01.824 INFO  reader  /topic_a: cache hit, 1,200,000 rows
    20:14:03.117 INFO  reader  /topic_b: 250,000 / 800,000 (31%)
"""

from __future__ import annotations

import logging
import os
import sys


_CONFIGURED = False


def configure(level: str | int | None = None) -> None:
    """Install a stderr StreamHandler with a compact format. Idempotent."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    if level is None:
        level = os.environ.get("BAG_ANALYZER_LOG", "INFO")
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d %(levelname)-5s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    ))

    root = logging.getLogger()
    # Don't double-add handlers if something already configured logging.
    root.handlers[:] = [handler]
    root.setLevel(level)
