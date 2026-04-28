"""Lazy ROS 2 imports.

We import `rosbag2_py`, `rclpy.serialization`, and `rosidl_runtime_py.utilities`
only when first needed. This lets the GUI start (and report a clean error) even
if the user forgot to source ROS, instead of crashing on import.
"""

from __future__ import annotations

from typing import Any, Tuple


def import_ros() -> Tuple[Any, Any, Any]:
    """Return (rosbag2_py, deserialize_message, get_message)."""
    import rosbag2_py
    from rclpy.serialization import deserialize_message
    from rosidl_runtime_py.utilities import get_message
    return rosbag2_py, deserialize_message, get_message
