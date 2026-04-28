"""Chain latency: transport (rosbag-based) + end-to-end (header-based).

We compute **two distinct kinds of latency** for the user-defined chain
``[T0, T1, ..., Tn]``:

1. **Transport latency** — uses ``t_bag``, the wall-clock time the bag
   wrote each row. Always available, robust against missing/restamped
   headers. (Sometimes called *pipeline* or *inter-stage* latency.)

       lat_xport_hop_i_to_(i+1)  =  t_bag(T_{i+1}) - t_bag(T_i)
       lat_xport_total           =  t_bag(T_n)    - t_bag(T_0)

   Why this matters:
       It tells you how long, in *bag time*, a message spent travelling
       between stages of your pipeline. It is what your processing stages
       contribute. It does NOT account for whatever delay happened before
       the source topic was published (sensor capture delay, USB transfer,
       kernel queueing, ...).

2. **End-to-End (E2E) latency** — uses ``header.stamp`` of the source
   topic ``T0`` as the t-zero. Only meaningful when ``T0`` carries a real
   header stamp, typically set by the *publisher* near the moment of
   acquisition (e.g. the camera driver writing the capture-clock value
   into the message).

       lat_src_T0     =  t_bag(T0)   - header.stamp(T0)         # source delay
       lat_e2e_at_Ti  =  t_bag(Ti)   - header.stamp(T0)         # cumulative
       lat_e2e_total  =  t_bag(Tn)   - header.stamp(T0)         # end-to-end

   Why this matters:
       Transport latency hides the "head start" the message already had
       before it hit your topic chain. End-to-end latency includes *that*
       — the source delay between when the publisher claims the data was
       sampled (header.stamp) and when the bag recorded it (t_bag). Add
       the transport latency on top and you get the total observed age
       of the data at any downstream topic.

Match strategy
--------------
* **Exact** — inner-merge every topic on ``header_stamp_ns``. Works when
  every node in the chain forwards the source's header.stamp unchanged
  (the ROS convention).
* **Approximate** — when the exact merge collapses (>= 99.9% loss at any
  hop) we fall back to ``pd.merge_asof(direction="forward")`` with the
  user's tolerance. If a topic has no real header stamps, that topic's
  join key falls back to its own ``t_bag_ns``. In this fallback mode the
  *match* uses bag-time but we still preserve the source's
  ``header_stamp_ns`` if it was real, so end-to-end latency may still be
  available.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from constants import hop_label as _lbl

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result metadata returned alongside the merged DataFrame.
# ---------------------------------------------------------------------------

@dataclass
class ChainResult:
    """Bundle returned by :func:`compute_chain_latency`.

    Use ``ChainResult.reasoning_lines()`` to render a human explanation of
    what was actually computed and why.
    """

    merged: pd.DataFrame
    method: str                       # "exact" | "approximate"
    counts: Dict[str, int]            # per-topic message counts
    has_e2e_latency: bool             # header.stamp of source is usable
    source_stamp_coverage: float      # fraction of merged rows with valid hdr
    chain: List[str]
    tolerance_ms: float

    # Backwards-compat alias for any code that still reads the old name.
    @property
    def has_true_latency(self) -> bool:        # pragma: no cover
        return self.has_e2e_latency

    def reasoning_lines(self) -> List[str]:
        """One paragraph explaining the analysis to a human."""
        n = len(self.merged)
        out = []
        if self.method == "exact":
            out.append(
                "Match: exact join on header.stamp — every topic in the "
                "chain forwards the source stamp unchanged, so we can match "
                "messages without any tolerance.")
        else:
            out.append(
                f"Match: approximate (merge_asof, ±{self.tolerance_ms:g} ms) "
                "— exact join failed because some node in the chain restamps "
                "messages or drops the header. We pair each upstream message "
                "with the next downstream message within the tolerance.")

        out.append(
            "Transport latency (rosbag-based, always available): "
            "t_bag(downstream) − t_bag(upstream). Robust; measures only the "
            "time the message spent travelling between stages of your "
            "pipeline. Computed for every hop and the chain total.")

        if self.has_e2e_latency:
            cov = 100.0 * self.source_stamp_coverage
            out.append(
                f"End-to-end latency (header-based, available for {cov:.1f}% "
                "of matched rows): t_bag(any topic) − header.stamp(source). "
                "Includes the source delay (publisher → bag) on top of the "
                "transport latency, giving the total observed age of the "
                "data at each downstream topic.")
        else:
            out.append(
                "End-to-end latency: NOT available — the source topic has no "
                "valid header.stamp on the matched rows, so we can only "
                "report transport (rosbag-based) latency.")

        out.append(
            f"Matched rows: {n:,}. The plot panes and stats below let you "
            "slice both kinds independently.")
        return out


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def _row_stats(s: pd.Series, threshold_ms: Optional[float]) -> Dict:
    s = s.dropna()
    if s.empty:
        return {"n": 0, "mean_ms": 0, "p50_ms": 0, "p95_ms": 0, "p99_ms": 0,
                "max_ms": 0, "min_ms": 0, "stddev_ms": 0, "jitter_ms": 0,
                "above_n": 0, "above_pct": 0.0}
    arr = s.to_numpy()
    jitter = float(np.sqrt(np.mean(np.diff(arr) ** 2))) if arr.size > 1 else 0.0
    above_n = int((arr > threshold_ms).sum()) if threshold_ms is not None else 0
    above_pct = (100.0 * above_n / arr.size) if threshold_ms is not None else 0.0
    return {
        "n": int(arr.size),
        "mean_ms": float(np.mean(arr)),
        "p50_ms": float(np.median(arr)),
        "p95_ms": float(np.quantile(arr, 0.95)),
        "p99_ms": float(np.quantile(arr, 0.99)),
        "max_ms": float(np.max(arr)),
        "min_ms": float(np.min(arr)),
        "stddev_ms": float(np.std(arr)),
        "jitter_ms": jitter,
        "above_n": above_n,
        "above_pct": above_pct,
    }


def stats_table(merged: pd.DataFrame, chain: List[str],
                threshold_ms: Optional[float] = None) -> List[Dict]:
    """Return one stats dict per latency series (transport + end-to-end).

    Each row carries ``"kind"`` ∈ {``"transport"``, ``"e2e"``} so the GUI
    can group/colour them.
    """
    rows: List[Dict] = []
    if merged.empty:
        return rows

    # ---- Transport (rosbag-based) ----
    for i in range(len(chain) - 1):
        a, b = _lbl(i), _lbl(i + 1)
        col = f"lat_{a}_{b}_ms"
        if col not in merged.columns:
            continue
        row = {"kind": "transport",
               "hop": f"{a} → {b}",
               "from": chain[i], "to": chain[i + 1],
               "what": "t_bag(downstream) − t_bag(upstream)"}
        row.update(_row_stats(merged[col], threshold_ms))
        rows.append(row)
    if "lat_total_ms" in merged.columns:
        row = {"kind": "transport",
               "hop": f"Transport total ({_lbl(0)} → {_lbl(len(chain)-1)})",
               "from": chain[0], "to": chain[-1],
               "what": "t_bag(last) − t_bag(first)"}
        row.update(_row_stats(merged["lat_total_ms"], threshold_ms))
        rows.append(row)

    # ---- End-to-end (header-based) ----
    if "lat_src_ms" in merged.columns:
        row = {"kind": "e2e",
               "hop": f"Source delay @ {_lbl(0)}",
               "from": f"{chain[0]}.header.stamp", "to": f"{chain[0]}.t_bag",
               "what": "t_bag(source) − header.stamp(source)"}
        row.update(_row_stats(merged["lat_src_ms"], threshold_ms))
        rows.append(row)
    for i in range(1, len(chain)):
        col = f"lat_true_{_lbl(i)}_ms"
        if col not in merged.columns:
            continue
        if i == len(chain) - 1:
            label = f"E2E end-to-end ({chain[0]}.header → {chain[-1]})"
        else:
            label = f"E2E @ {_lbl(i)} ({chain[0]}.header → {chain[i]})"
        row = {"kind": "e2e",
               "hop": label,
               "from": f"{chain[0]}.header.stamp", "to": chain[i],
               "what": f"t_bag({chain[i]}) − header.stamp(source)"}
        row.update(_row_stats(merged[col], threshold_ms))
        rows.append(row)

    return rows


# ---------------------------------------------------------------------------
# Chain join
# ---------------------------------------------------------------------------

def _approximate_chain_join(
    dfs: Dict[str, pd.DataFrame],
    chain: List[str],
    tolerance_ms: float,
) -> Optional[pd.DataFrame]:
    tol_ns = int(tolerance_ms * 1e6)

    def key_df(topic: str, label: str) -> pd.DataFrame:
        df = dfs[topic][["header_stamp_ns", "t_bag_ns", "msg_index"]].copy()
        has_stamps = (df["header_stamp_ns"] > 0).mean() > 0.5
        df["join_key"] = df["header_stamp_ns"] if has_stamps else df["t_bag_ns"]
        df = df.sort_values("join_key")
        df = df.rename(columns={
            "t_bag_ns": f"t_{label}_ns",
            "msg_index": f"idx_{label}",
        })
        return df[["join_key", "header_stamp_ns",
                   f"t_{label}_ns", f"idx_{label}"]]

    base = key_df(chain[0], _lbl(0))
    merged = base
    for i, topic in enumerate(chain[1:], start=1):
        d = key_df(topic, _lbl(i)).drop(columns=["header_stamp_ns"])
        merged = pd.merge_asof(
            merged, d, on="join_key",
            direction="forward",
            tolerance=tol_ns,
        )
    merged = merged.dropna()
    if len(merged) == 0:
        return merged
    for i in range(len(chain)):
        col = f"t_{_lbl(i)}_ns"
        merged[col] = merged[col].astype(np.int64)
        merged[f"idx_{_lbl(i)}"] = merged[f"idx_{_lbl(i)}"].astype(np.int64)
    return merged.reset_index(drop=True)


def compute_chain_latency(
    dfs: Dict[str, pd.DataFrame],
    chain: List[str],
    tolerance_ms: float = 50.0,
) -> Tuple[pd.DataFrame, str, Dict[str, int], "ChainResult"]:
    """Compute transport + end-to-end latency for ``chain``.

    Returns ``(merged, method, counts, result)`` — the first three for
    backward compatibility with earlier callers; ``result`` is a
    :class:`ChainResult` carrying flags and a human-readable reasoning.
    """
    counts = {t: len(dfs[t]) for t in chain}
    method = "exact"

    base = dfs[chain[0]][["header_stamp_ns", "t_bag_ns", "msg_index"]].copy()
    base = base[base["header_stamp_ns"] > 0]
    base = base.drop_duplicates(subset="header_stamp_ns", keep="first")
    base = base.rename(columns={
        "t_bag_ns": f"t_{_lbl(0)}_ns",
        "msg_index": f"idx_{_lbl(0)}",
    })

    merged = base
    exact_failed = False
    for i, topic in enumerate(chain[1:], start=1):
        d = dfs[topic][["header_stamp_ns", "t_bag_ns", "msg_index"]].copy()
        d = d[d["header_stamp_ns"] > 0]
        d = d.drop_duplicates(subset="header_stamp_ns", keep="first")
        d = d.rename(columns={
            "t_bag_ns": f"t_{_lbl(i)}_ns",
            "msg_index": f"idx_{_lbl(i)}",
        })
        new_merged = merged.merge(d, on="header_stamp_ns", how="inner")
        if len(new_merged) < max(10, 0.001 * min(len(merged), len(d))):
            exact_failed = True
            break
        merged = new_merged

    if exact_failed or len(merged) < 10:
        method = "approximate"
        logger.info("Exact join collapsed (%d rows). Falling back to "
                    "approximate join with ±%g ms tolerance.",
                    len(merged), tolerance_ms)
        merged = _approximate_chain_join(dfs, chain, tolerance_ms)

    if merged is None or len(merged) == 0:
        result = ChainResult(
            merged=pd.DataFrame(), method=method, counts=counts,
            has_e2e_latency=False, source_stamp_coverage=0.0,
            chain=chain, tolerance_ms=tolerance_ms)
        return pd.DataFrame(), method, counts, result

    # ---------------- Transport latency (rosbag-based) ----------------
    for i in range(len(chain) - 1):
        a, b = _lbl(i), _lbl(i + 1)
        merged[f"lat_{a}_{b}_ms"] = (
            merged[f"t_{b}_ns"] - merged[f"t_{a}_ns"]
        ) / 1e6
    if len(chain) > 1:
        merged["lat_total_ms"] = (
            merged[f"t_{_lbl(len(chain)-1)}_ns"] - merged[f"t_{_lbl(0)}_ns"]
        ) / 1e6

    # ---------------- End-to-end latency (header-based) --------------
    has_e2e = False
    coverage = 0.0
    if "header_stamp_ns" in merged.columns:
        hs = merged["header_stamp_ns"]
        valid = hs > 0
        coverage = float(valid.mean()) if len(merged) else 0.0
        if valid.any():
            has_e2e = True
            hs_arr = hs.to_numpy()
            valid_arr = valid.to_numpy()
            for i in range(len(chain)):
                lbl = _lbl(i)
                t_ns = merged[f"t_{lbl}_ns"].to_numpy()
                col = f"lat_true_{lbl}_ms"   # keep column name for compat
                merged[col] = np.where(
                    valid_arr, (t_ns - hs_arr) / 1e6, np.nan)
            # convenience aliases
            merged["lat_src_ms"] = merged[f"lat_true_{_lbl(0)}_ms"]
            if len(chain) > 1:
                merged["lat_true_total_ms"] = merged[
                    f"lat_true_{_lbl(len(chain)-1)}_ms"]

    merged["t_source_dt"] = pd.to_datetime(merged[f"t_{_lbl(0)}_ns"], unit="ns")
    merged["seq_index"] = np.arange(len(merged), dtype=np.int64)
    merged = merged.reset_index(drop=True)

    result = ChainResult(
        merged=merged, method=method, counts=counts,
        has_e2e_latency=has_e2e, source_stamp_coverage=coverage,
        chain=chain, tolerance_ms=tolerance_ms)
    logger.info("compute_chain_latency: method=%s rows=%d has_e2e=%s "
                "coverage=%.1f%%",
                method, len(merged), has_e2e, 100 * coverage)
    return merged, method, counts, result
