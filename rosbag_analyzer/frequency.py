"""Per-topic publish-rate (Hz) computation.

For each selected topic we histogram the bag timestamps into fixed-width time
bins and divide by the bin width to obtain an instantaneous rate (in Hz). All
topics share the same time bin edges so the resulting curves are directly
comparable on a single plot.
"""

from __future__ import annotations

from typing import Dict, Tuple

import numpy as np
import pandas as pd


def common_bin_edges(dfs: Dict[str, pd.DataFrame], bin_s: float
                     ) -> Tuple[np.ndarray, np.ndarray]:
    """Return (edges_ns, centers_s) covering every topic's [t_min, t_max]."""
    bin_ns = max(1, int(bin_s * 1e9))
    starts, ends = [], []
    for df in dfs.values():
        if len(df) == 0:
            continue
        starts.append(int(df["t_bag_ns"].iloc[0]))
        ends.append(int(df["t_bag_ns"].iloc[-1]))
    if not starts:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float64)
    t0, t1 = min(starts), max(ends)
    edges = np.arange(t0, t1 + bin_ns, bin_ns, dtype=np.int64)
    if edges.size < 2:
        edges = np.array([t0, t0 + bin_ns], dtype=np.int64)
    centers = (edges[:-1].astype(np.float64) + bin_ns / 2.0) / 1e9
    return edges, centers


def topic_rates(dfs: Dict[str, pd.DataFrame], bin_s: float
                ) -> Tuple[Dict[str, np.ndarray], np.ndarray]:
    """Return ``({topic: rate_hz_per_bin}, bin_centers_seconds)``."""
    edges_ns, centers_s = common_bin_edges(dfs, bin_s)
    rates: Dict[str, np.ndarray] = {}
    if centers_s.size == 0:
        return rates, centers_s
    for topic, df in dfs.items():
        if len(df) == 0:
            rates[topic] = np.zeros_like(centers_s)
            continue
        ts = df["t_bag_ns"].to_numpy()
        counts, _ = np.histogram(ts, bins=edges_ns)
        rates[topic] = counts.astype(np.float64) / bin_s
    return rates, centers_s


def topic_rate_stats(df: pd.DataFrame, rate_hz: np.ndarray) -> Dict[str, float]:
    """Per-topic summary statistics for the rate array.

    `mean / median / min / stddev` are computed over **non-zero** bins so that
    long idle stretches (silent topics) do not dominate the average. `max` and
    `n` use the raw values, and `duration_s` is end-time minus start-time.
    """
    n = len(df)
    duration_s = ((int(df["t_bag_ns"].iloc[-1]) - int(df["t_bag_ns"].iloc[0]))
                  / 1e9) if n >= 2 else 0.0
    arr = rate_hz
    nz = arr[arr > 0] if arr.size else arr
    return {
        "n": n,
        "duration_s": duration_s,
        "mean_hz": float(nz.mean()) if nz.size else 0.0,
        "median_hz": float(np.median(nz)) if nz.size else 0.0,
        "min_hz": float(nz.min()) if nz.size else 0.0,
        "max_hz": float(arr.max()) if arr.size else 0.0,
        "stddev_hz": float(nz.std()) if nz.size else 0.0,
    }
