# How the analyzer computes latency, frequency, and `header.stamp`

This document explains the maths and the byte-level tricks used by the
package. It is meant to be readable end-to-end.

The three things we compute are:

1. **`header.stamp` extraction** — pulling the originating timestamp out of
   each message *without* fully deserializing it.
2. **Per-hop and end-to-end latency** for a chain `A → B → C → ...`.
3. **Per-topic publish frequency** in Hz over time.

---

## 0. Vocabulary

| Symbol | Meaning |
|---|---|
| `t_bag` | The wall-clock time, in nanoseconds, that the bag wrote a row. This is the value of the `messages.timestamp` column in the rosbag2 sqlite schema. |
| `header.stamp` | The `builtin_interfaces/Time` field that the *publisher* placed inside the message. It is `(int32 sec, uint32 nanosec)`. We flatten it into a single `int64`: `header_stamp_ns = sec * 1e9 + nanosec`. |
| Hop | A single edge of the chain — i.e. a pair of consecutive topics `(X, Y)`. |
| Chain | An ordered list of topics `[T0, T1, ..., Tn]`. There are `n` hops. |
| Source / sink | The first and last topic of the chain. |

Time is always stored as `int64` nanoseconds internally to avoid the precision
loss `float64` would introduce around 2025 epoch values.

---

## 1. Extracting `header.stamp` from the raw bytes

This is the single optimization that makes the loader fast on multi-GiB bags.

### 1.1 The CDR encapsulation header

Every ROS 2 message stored in a bag is encoded with **CDR** (Common Data
Representation, OMG). The very first 4 bytes form the *encapsulation header*:

```
+----------+----------+----------+----------+
| repr_id  | repr_id  | options  | options  |
|  byte 0  |  byte 1  |  byte 2  |  byte 3  |
+----------+----------+----------+----------+
```

* `representation_identifier` — 2 bytes that tell us the byte order of the
  body that follows. The two values we care about are
  `0x00 0x01` → little-endian CDR (the default on x86), and
  `0x00 0x00` (with the second pair `0x01 0x00`) → big-endian CDR.
* `options` — almost always zero.

### 1.2 The body when the first field is `std_msgs/Header`

A great many ROS 2 message types start with a `std_msgs/Header header` field.
`Header` is itself a struct:

```idl
builtin_interfaces/Time stamp   # int32 sec + uint32 nanosec
string                  frame_id
```

The `Time` struct contains only fixed-size primitives, and CDR places it at
4-byte alignment. Because the encapsulation header is already 4 bytes long,
no padding is inserted, and the bytes immediately after the encapsulation
header are exactly:

```
offset  size   field
4       4      sec      (int32, signed, native byte order per repr_id)
8       4      nanosec  (uint32, unsigned, native byte order per repr_id)
```

So the `header.stamp` of any header-prefixed message lives at **bytes
`[4:12]`** of the serialised payload.

### 1.3 The fast path

The loader extracts that with a single `struct.unpack_from`:

```python
# little-endian (x86 default)
sec, nsec = struct.unpack_from("<iI", data, 4)
header_stamp_ns = sec * 1_000_000_000 + nsec
```

`<iI` means "little-endian, signed int32, unsigned uint32". The big-endian
case uses `>iI`. We pick the right format by reading the first two bytes
once.

### 1.4 Validating the fast path

Not every message type starts with `Header`. For example, a custom interface
might define `Header header2` later in the struct, or use a completely
different first field. We can't tell from metadata alone, so on the **first
message** of every topic we:

1. Run the slow path: `deserialize_message(data, msg_class)`, then read
   `msg.header.stamp` reflectively.
2. Run the fast path: `struct.unpack_from("<iI", data, 4)`.
3. Compare. If they agree, set `fast_ok = True` and use the fast path for
   every subsequent message of this topic. If they disagree, set
   `fast_ok = False` and fall back to `deserialize_message` for the whole
   topic.

This is implemented in `reader.py`. The validator is what makes
the trick safe: a topic whose layout is unusual silently falls back to the
slow path; we never produce wrong stamps.

### 1.5 Why this matters

`deserialize_message` allocates a Python object, walks the entire message
graph, and copies every primitive field. For a 1 MiB image it parses a
megabyte of pixel data only to throw it away. The fast path reads 8 bytes
and ignores the rest. On a multi-million-message bag this typically gives a
10× speed-up of the loading step end-to-end.

---

## 2. Latency computation

We compute **two distinct kinds of latency** for the same matched-message
table. Each kind answers a different question:

| Kind | Formula | Question it answers | Always available? |
|---|---|---|---|
| **Pipeline** (rosbag-based) | `t_bag(downstream) − t_bag(upstream)` | "How long did the message spend travelling between two topics inside my pipeline?" | Yes |
| **True / source-to-system** (header-based) | `t_bag(downstream) − header.stamp(source)` | "How old is the data at this downstream topic, counting from the moment the publisher claims it was acquired?" | Only when the source's `header.stamp` is real (sensor drivers, rosbag2 publishers, anything that fills it in) |

### 2.0 Why two kinds?

`t_bag` is the moment **the bag wrote** the row — strictly monotonic, present
on every row, totally robust. It is the right reference for measuring what
**your nodes** are doing, because it does not include any delay that
happened *before* the source topic existed in the chain.

`header.stamp` of the source is the moment the **publisher** claims the
data was acquired (e.g. the camera's hardware capture timestamp, written by
the driver). Using `header.stamp(source)` as `t = 0` gives you the
**total observed age of the data** at any downstream topic — capture
delay + USB delay + kernel + driver + your pipeline. That is what an
end-user actually feels.

Showing both in the same UI lets you decompose the end-to-end delay:

```
true_end_to_end   =   source_delay   +   pipeline_total
   t_bag(C)             t_bag(A)             t_bag(C)
   - hdr(A)             - hdr(A)             - t_bag(A)
```

### 2.1 Pipeline latency formulas

For a chain `[T0, T1, ..., Tn]`:

```
lat_pipe_hop_i_to_(i+1) = t_bag(T_{i+1}) − t_bag(T_i)        (ns)
lat_pipe_total          = t_bag(T_n)     − t_bag(T_0)
```

These are reported in milliseconds. They appear as columns
`lat_<A>_<B>_ms` and `lat_total_ms` in the merged DataFrame.

### 2.2 True / header-based latency formulas

When the source's `header.stamp` is valid on a row (`header_stamp_ns > 0`):

```
lat_src_T0     = t_bag(T0) − header.stamp(T0)         (source delay)
lat_true_at_Ti = t_bag(Ti) − header.stamp(T0)         (cumulative)
lat_true_total = t_bag(Tn) − header.stamp(T0)         (end-to-end)
```

These appear as columns `lat_src_ms`, `lat_true_<X>_ms`, and
`lat_true_total_ms`. Where the source stamp is missing on a row, the
column is `NaN`; the stats functions (`np.quantile`, `mean`, ...) ignore
NaNs via a `dropna()` before reduction.

### 2.3 The match step (shared by both kinds)

Both kinds use the **same matched-row table** — the only difference is
which time field they subtract. Matching is what tells us "this row in
T0 corresponds to this row in T1 corresponds to this row in T2." The
match strategies are unchanged and described next.

### 2.1 Exact join on `header.stamp`

In a well-behaved ROS pipeline, every node propagates `header.stamp` from its
input message to its output message. That means the *same* `header_stamp_ns`
value appears in every topic of the chain — once per logical message.

So the exact join is:

```python
merged = T0
for Ti in [T1, ..., Tn]:
    merged = merged.merge(Ti, on="header_stamp_ns", how="inner")
```

* We discard rows with `header_stamp_ns <= 0` first (those are messages with
  no stamp; they cannot participate in any chain).
* We also `drop_duplicates(subset="header_stamp_ns", keep="first")` per topic,
  so a republished stamp counts once.

After this loop, every row of `merged` carries `t_T0_ns, t_T1_ns, ...,
t_Tn_ns` — the bag times we need.

#### When does exact fail?

Some pipelines *restamp* messages: a node receives `(stamp=A)`, does work,
then publishes `(stamp=B != A)`. After such a node the inner join collapses
to ~0 rows.

The detector is purely empirical:

```python
if len(new_merged) < max(10, 0.001 * min(len(merged), len(d))):
    # less than 0.1% of either side matched; abandon exact join
    exact_failed = True
    break
```

That is, *if joining an extra topic loses more than 99.9% of rows, exact is
hopeless and we switch strategies*.

### 2.2 Approximate join with a tolerance

When exact fails we use `pandas.merge_asof`, which performs a sorted-merge
that, for each row of the left side, picks the *nearest* row on the right
within a tolerance:

```python
pd.merge_asof(
    upstream, downstream, on="join_key",
    direction="forward",                 # downstream comes AFTER upstream
    tolerance=int(tolerance_ms * 1e6),   # in nanoseconds
)
```

* `direction="forward"` enforces causality — for an upstream row at time
  `t_u`, the matched downstream row must satisfy
  `t_u <= t_d <= t_u + tolerance`.
* `tolerance` is the user's "Approx. tolerance (ms)" in the GUI. 50 ms is a
  reasonable default for many ROS pipelines.

#### The join key when stamps are missing

If a topic in the chain has no real `header.stamp` at all (most of its rows
have `header_stamp_ns <= 0`), we substitute `t_bag_ns` as that topic's
`join_key`. Concretely, in `_approximate_chain_join`:

```python
has_stamps = (df["header_stamp_ns"] > 0).mean() > 0.5
df["join_key"] = df["header_stamp_ns"] if has_stamps else df["t_bag_ns"]
```

The 50% threshold is deliberately tolerant: a few sentinel zero stamps in an
otherwise-stamped topic still let us use stamps for matching.

### 2.4 Computing pipeline latency

Once `merged` is a single dataframe with `t_T0_ns, ..., t_Tn_ns`:

```python
for i in range(n):                     # n = len(chain) - 1
    A, B = labels[i], labels[i+1]      # 'A','B','C',...
    merged[f"lat_{A}_{B}_ms"] = (merged[f"t_{B}_ns"] - merged[f"t_{A}_ns"]) / 1e6

merged["lat_total_ms"] = (merged[f"t_{labels[-1]}_ns"]
                          - merged[f"t_{labels[0]}_ns"]) / 1e6
```

### 2.5 Computing true latency

When the source row carries a real `header.stamp`, we add three families of
columns (rows where the source stamp is invalid get `NaN`, ignored by stats):

```python
hs    = merged["header_stamp_ns"]                # source stamp
valid = (hs > 0)

merged["lat_src_ms"]        = np.where(valid,
                                       (merged["t_A_ns"] - hs) / 1e6, np.nan)
merged["lat_true_B_ms"]     = np.where(valid,
                                       (merged["t_B_ns"] - hs) / 1e6, np.nan)
merged["lat_true_total_ms"] = np.where(valid,
                                       (merged[f"t_{last}_ns"] - hs) / 1e6,
                                       np.nan)
```

Note that `lat_src_ms` is exactly `lat_true_A_ms` — both equal
`t_bag(A) − header.stamp(A)`. We expose it under both names so the GUI can
label it intuitively ("source delay" vs. "true latency at A").

The `ChainResult` returned by `compute_chain_latency` carries:

* `has_true_latency` — `True` when at least one matched row has a valid
  source stamp.
* `source_stamp_coverage` — the fraction of matched rows where the source
  stamp is valid (the GUI shows this in the reasoning panel).

### 2.6 Convenience columns

We also attach:

* `t_source_dt` — `pd.to_datetime(t_T0_ns, unit="ns")` for human-readable
  CSV export.
* `seq_index` — `0..len(merged)-1` for the "Message index" plot X-axis.

### 2.7 What the GUI shows you and why

After **Compute Latency**, the analyzer reports:

* **Match line** — `exact` or `approximate` and the tolerance used. This
  tells you *how* messages were paired up.
* **Reasoning panel** — three or four bullets explaining, in plain English,
  which kinds of latency are available, what they measure, and what to use
  them for. This is generated by `ChainResult.reasoning_lines()`.
* **Stats table** — every series gets one row, tagged either **Pipeline**
  (light blue background) or **True** (light green). Columns: `kind, hop,
  definition, n, min, mean, p50, p95, p99, max, stddev, jitter, above SLA`.
* **Loss accounting** — per-topic counts and `Δ` per hop. Tells you whether
  a node is dropping messages.
* **Plot panes** — every pane lets you choose any single series or any
  overlay. Selections are tagged `[Pipe]`, `[True]`, or `[Pipe+True]` so you
  always know which kind you're looking at. The `[Pipe+True] Compare
  end-to-end` selection overlays `lat_total_ms` and `lat_true_total_ms`
  on the same axes — the gap between them at any moment is the source
  delay at that moment.

### 2.8 Loss accounting

For each topic we know:

* `counts[T_i]` — total messages of that topic in the bag (per-topic length
  before any join).
* `len(merged)` — messages that survived the chain join.

The GUI displays both `Δ` between consecutive topics (how many messages were
dropped per hop) and a final "matched in chain" percentage relative to the
source topic.

### 2.9 Per-series summary statistics

For each latency series — pipeline hop, pipeline total, source delay,
true-at-Ti, and true end-to-end — we report:

| stat | formula |
|---|---|
| `n` | number of finite samples |
| `min`, `mean`, `max`, `stddev` | the obvious numpy reductions |
| `p50`, `p95`, `p99` | `np.quantile(arr, q)` |
| `jitter` | RMS of the consecutive-sample first differences: `sqrt(mean((Δarr)²))`. Large `jitter` with small `stddev` means smooth bursts; small `jitter` with large `stddev` means slow drifts. |
| `above SLA n / %` | count and % of samples strictly greater than the user's SLA threshold (only when threshold > 0). |

Every row is tagged `kind = pipeline | true` so the GUI can colour-group
them and so you can `groupby('kind')` in CSV exports.

All of this lives in `latency.py:_row_stats`.

---

## 3. Frequency computation

Frequency is far simpler than latency: we only need the bag timestamps of one
or more topics.

### 3.1 Common bin edges

Given a set of topics with bag-time arrays `t_i`, we cover the *union* of
their time ranges with fixed-width bins:

```python
t_start = min over topics of t_i[0]
t_end   = max over topics of t_i[-1]
bin_ns  = round(bin_seconds * 1e9)

edges_ns   = arange(t_start, t_end + bin_ns, bin_ns)        # int64
centers_s  = (edges_ns[:-1] + bin_ns/2) / 1e9               # float64, plotted
```

All topics share the same edges so curves are directly comparable on one
plot. `bin_seconds` defaults to 1.0 and is exposed as the "Bin width (s)"
spinbox in the GUI.

### 3.2 Per-topic histogram

For each topic:

```python
counts, _ = np.histogram(t_i, bins=edges_ns)   # ints, one per bin
rate_hz   = counts.astype(float) / bin_seconds # messages per second
```

That's it — `np.histogram` is a single C-level pass over `t_i`, so this is
fast even for tens of millions of timestamps.

### 3.3 Smoothing

The "Smooth" spinbox applies a centred moving average over `w` consecutive
bins:

```python
y_smooth = pd.Series(rate_hz).rolling(w, min_periods=1).mean().to_numpy()
```

`w = 1` (the default) is no smoothing. Useful when the bin width is small
enough to expose bursty publishers.

### 3.4 Per-topic stats

The GUI reports:

| stat | computed over | rationale |
|---|---|---|
| `msgs` | all rows of the topic | exact total |
| `duration` | `t[-1] - t[0]` | the topic's active span in the bag |
| `mean / median / min / stddev Hz` | **non-zero bins only** | a topic that publishes at 10 Hz for 30 s and is silent for 5 minutes should report ≈10 Hz, not 1 Hz. We exclude zero-count bins from those four statistics. |
| `max Hz` | all bins | the worst-case burst is interesting |

The non-zero-mean choice is documented in
`frequency.py:topic_rate_stats`.

### 3.5 Choosing a bin width

* Smaller bins → more time resolution but noisier rate (Poisson noise on
  small counts).
* Larger bins → smoother but coarser.

A sensible heuristic is `bin_s ≈ 5 / nominal_rate_hz`, e.g. `0.1 s` for a
50 Hz topic, `1 s` for a 5 Hz topic. The GUI lets you change the bin width
and recompute live without re-reading the bag, so just try a few.

---

## 4. End-to-end picture

```
                            +----------------------+
              t_bag, raw    | sqlite messages tbl  |
              ----------->  |  (per .db3 split)    |
                            +----------+-----------+
                                       |
                  fetchmany(50000)     |  per-topic, per-thread
                                       v
              +-------------------------------+
              | reader.py: read_topic()       |
              |   probe(deserialize, struct)  |
              |   choose fast path per topic  |
              |   pre-allocated numpy buffers |
              +---------------+---------------+
                              |
                              v
              +---------------+---------------+
              |  pickle cache (per topic)     |
              +---------------+---------------+
                              |
                              v
              +---------------+---------------+         +-------------------+
              |  loader.py: ChainLoaderThread |         | metadata.yaml     |
              |  ThreadPool(N=cpu_count)      | <-----> | (counts, splits)  |
              |  one mutex on progress dict   |         +-------------------+
              +---------------+---------------+
                              |
            chain join         |          rate binning
                              v
        +---------------------+---------------------+
        | latency.py                       frequency.py
        |   exact merge → fallback merge_asof
        |   per-hop & total ms             counts / bin / s
        |   stats_table()                  topic_rate_stats()
        +-------------------------+-------------------------+
                                  |
                                  v
                         +--------+---------+
                         |   plotting.py    |
                         |   PlotPane,      |
                         |   PopoutWindow,  |
                         |   TimeAxisItem   |
                         +------------------+
```

That is the entire computational pipeline. Each block is small enough to
read in one sitting; if anything is unclear, the file at the top of each
block is the canonical place to look.
