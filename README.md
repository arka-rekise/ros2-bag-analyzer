# ROS 2 Bag Latency & Frequency Analyzer

A PyQt5 + PyQtGraph desktop tool that:

* Opens a **ROS 2 bag folder** (`metadata.yaml` + one or more `.db3` splits)
  and inspects every topic without loading payloads.
* Reconstructs message flow across a user-defined topic chain
  (`A → B → C → ...`) using either exact `header.stamp` joins or an
  approximate `merge_asof` with a configurable tolerance.
* Computes **two distinct kinds of latency** for each matched message and
  reports both side-by-side:
  - **Transport latency** (rosbag-based, always available):
    `t_bag(downstream) − t_bag(upstream)` — robust, measures only the
    delay your nodes contribute. (Also called *pipeline* or *inter-stage*.)
  - **End-to-End (E2E) latency** (header-based, when `header.stamp` is
    valid on the source): `t_bag(downstream) − header.stamp(source)`.
    Includes the source delay (publisher → bag), so it reflects the
    *total observed age* of the data at any downstream topic.
* Computes **per-topic publish frequency (Hz)** over time with adjustable
  bin width and smoothing.
* Every label, control, and table column has an **ⓘ hover icon** with a
  plain-English explanation, so a non-engineer can read the headline
  numbers and dig deeper only when they want context.
* Renders interactive zoomable plots, multiple analysis tabs, pop-out plot
  windows, SLA threshold lines, histograms, CDFs, rolling means.
* Caches per-topic timestamps to disk so re-running an analysis is instant.

The maths behind these computations is documented in
[`rosbag_analyzer/ALGORITHM.md`](rosbag_analyzer/ALGORITHM.md).

---

## Quick glossary (for non-engineers)

| Term | In one sentence |
|---|---|
| **Bag**            | A recording of a robot session. Contains every message every node published, with the time the recorder wrote it. |
| **Topic**          | A named channel on the robot, e.g. `/camera/image_raw`. |
| **Chain**          | An ordered list of topics that you believe a piece of data flows through, e.g. `camera → detector → tracker`. |
| **Latency**        | How long a message took. We report two kinds (see below). |
| **Transport latency** | "How long did the message take inside *your* pipeline?" Robust, always available. Uses bag-recorded times. (a.k.a. pipeline / inter-stage latency.) |
| **End-to-End (E2E) latency** | "How old is the data when it reaches this point?" Includes the publisher's source delay. Available only when the publisher fills in `header.stamp`. |
| **Source delay**   | Time between the publisher stamping the message and the bag recording it. |
| **Match: exact**   | Stamps lined up perfectly — trust the numbers. |
| **Match: approximate** | Paired by timing only, within a tolerance. Less reliable; use the histogram width to judge. |
| **SLA**            | Your acceptable maximum latency. The tool flags any message above it. |
| **p50 / p95 / p99**| 50%/95%/99% of messages were faster than this. p99 is your tail latency. |
| **Jitter**         | How bouncy consecutive latencies are. High jitter = unstable timing. |
| **Frequency (Hz)** | How many messages per second a topic publishes. |

Whenever you see one of these in the GUI, hover the **ⓘ** icon next to it
for a one- or two-line refresher.

---

## Repository layout

This repository (`~/ros_bag/`) holds the analyzer source plus the helper
script that flattens zipped bags. All Python modules live directly in
`rosbag_analyzer/` — flat, no nested packages.

```
~/ros_bag/                    # repo root (the directory you `git init` in)
├── README.md                 # this file
├── unzip_bags.sh             # extracts every *.zip in cwd into a bag dir
├── .gitignore                # whitelist: only README + rosbag_analyzer/ + .sh tracked
└── rosbag_analyzer/          # the analyzer source
    ├── bag_latency_gui.py    # entrypoint — run this
    ├── ALGORITHM.md          # how header.stamp / latency / frequency work
    ├── log_setup.py          # stderr logging
    ├── ui_main.py            # MainWindow + main()
    ├── ui_analysis_tab.py    # Latency-analysis tab
    ├── ui_frequency_tab.py   # Frequency-analysis tab
    ├── plotting.py           # PlotPane, PopoutWindow, TimeAxisItem
    ├── loader.py             # parallel ChainLoaderThread
    ├── reader.py             # fast sqlite + CDR header reader
    ├── latency.py            # chain join + per-hop stats
    ├── frequency.py          # rate-binning helpers
    ├── metadata.py           # BagMetadata
    ├── cache.py              # disk cache helpers
    ├── constants.py          # CDR consts, colours, cache dir
    ├── ros_imports.py        # lazy ROS imports
    └── .gitignore            # ignore .venv, __pycache__, *.db3, *.pkl, ...
```

Bag data (e.g. `20260419_050057/`, `*.zip`, `*.db3`) is **not** tracked —
the root `.gitignore` is a whitelist that only allows `README.md`,
`unzip_bags.sh`, and the `rosbag_analyzer/` source folder.

---

## 1. Prerequisites

* **Ubuntu 22.04** (or any distro that supports ROS 2 Humble).
* **ROS 2 Humble** installed system-wide (provides `rosbag2_py`, `rclpy`,
  `rosidl_runtime_py`, and the standard message packages).
* **Python ≥ 3.8** (Humble ships with Python 3.10).
* For bags that contain custom message types (e.g.
  `rkse_common_interfaces/...`), the workspace that **defines** those messages
  must be sourced before the GUI starts; otherwise the loader cannot construct
  message classes when the CDR fast-path falls back.

---

## 2. Installation — step by step

### 2.1 Install ROS 2 Humble (skip if already installed)

```bash
sudo apt update && sudo apt install -y curl gnupg lsb-release software-properties-common
sudo add-apt-repository universe
sudo curl -sSL https://raw.githubusercontent.com/ros/rosdistro/master/ros.key \
    -o /usr/share/keyrings/ros-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/ros-archive-keyring.gpg] \
    http://packages.ros.org/ros2/ubuntu $(lsb_release -cs) main" | \
    sudo tee /etc/apt/sources.list.d/ros2.list > /dev/null
sudo apt update
sudo apt install -y ros-humble-ros-base ros-humble-rosbag2 \
                    ros-humble-rosbag2-storage-default-plugins \
                    python3-rosbag2-py
```

### 2.2 Install Python dependencies

The GUI itself only needs five packages:

```bash
pip install --user pandas numpy PyQt5 pyqtgraph pyyaml
```

Optional but recommended (silences a parquet warning if you ever swap the
cache backend):

```bash
pip install --user pyarrow
```

### 2.3 Source ROS 2 (every shell)

```bash
source /opt/ros/humble/setup.bash
```

### 2.4 Source any custom-message workspaces

If your bag uses message types defined in your own ROS workspaces, source
those **after** ROS 2:

```bash
source ~/workspaces/swadheen_ws/install/setup.bash
source ~/bag_ws/install/setup.bash
```

A convenient one-liner you can put in `~/.bashrc`:

```bash
source /opt/ros/humble/setup.bash
[ -f ~/workspaces/swadheen_ws/install/setup.bash ] && source ~/workspaces/swadheen_ws/install/setup.bash
[ -f ~/bag_ws/install/setup.bash ]                 && source ~/bag_ws/install/setup.bash
```

---

## 3. Extracting bags from `.zip` files

If your bag is delivered as multiple zip files (one per split), drop the
zips into the repo root and run the helper script:

```bash
cd ~/ros_bag           # repo root (contains README.md, unzip_bags.sh, rosbag_analyzer/)
bash unzip_bags.sh     # or: ./unzip_bags.sh   (chmod +x first if needed)
sudo apt install -y sqlite3 
ros2 bag reindex <folder name> -s sqlite3
```

What it does:

1. Reads every `*.zip` in the directory the script lives in (= `~/ros_bag/`).
2. Creates an output folder named `20260419_050057/` next to the script.
3. Extracts every zip with `unzip -jo` (flatten paths, overwrite without
   prompting) so the resulting folder contains `metadata.yaml` plus all
   `*.db3` splits at the top level — exactly the layout this GUI expects.
4. If `metadata.yaml` not present, use the following commands to gneerate it.

Adjust `OUTPUT_DIR` in `unzip_bags.sh` if you want a different folder name.
The extracted folder is automatically ignored by the root `.gitignore`,
so it will not be committed.

---

## 4. Launching the GUI

The Python modules import each other by bare name, so you must run from
inside `rosbag_analyzer/`:

```bash
cd ~/ros_bag/rosbag_analyzer
python3 bag_latency_gui.py
```

Or, in a single line from the repo root:

```bash
( cd ~/ros_bag/rosbag_analyzer && python3 bag_latency_gui.py )
```

For verbose terminal output:

```bash
cd ~/ros_bag/rosbag_analyzer
BAG_ANALYZER_LOG=DEBUG python3 bag_latency_gui.py
```

The window opens with one empty *Latency 1* tab. Click **Open Bag Folder…**
(or use the dropdown for recent bags) and pick the folder containing
`metadata.yaml`.

---

## 5. Using the application

### 5.1 Latency analysis (default tab type)

1. **Open a bag** — the topic table on the left lists every topic with its
   type and message count.
2. In the topic table, **select two or more topics in the order
   `source → ... → destination`** and click **Add selected ➜ active tab**
   (or double-click). The selected topics appear in the chain box.
3. Reorder rows in the chain with ↑ / ↓; remove rows with **Remove**.
4. Set the two parameters (each has an **ⓘ** icon next to it for the
   long-form explanation):
   * **Tolerance (ms)** — only used if exact `header.stamp` matching fails.
     50 ms is fine for most pipelines.
   * **SLA (ms)** — optional. Plots get a red dashed line at this value and
     the stats table reports how many messages exceeded it.
5. Click **Compute Latency ▶**. The loader reads only the selected topics in
   parallel.
6. Read the results — the layout is intentionally crisp:
   * **Status line** (one line, top of the stats area). Reads like:
     ```
     1,000 matched · match: exact · E2E latency (100%)   ⓘ ⓘ
     ```
     Match tag is colour-coded: blue = `exact`, orange = `approximate ±X ms`.
     The two **ⓘ** icons hover-explain (a) what was just computed and why,
     and (b) the durable definitions of **Transport** vs **End-to-End** latency.
   * **Stats table** — one row per latency series.
     - First column **Kind**: `Transport` (light-blue tint) or `E2E`
       (light-green tint).
     - Hover any column header (`p50`, `p95`, `p99`, `jitter`, `above SLA`,
       …) for a one-sentence definition.
     - Series shown: every transport hop, transport total, and — when
       available — source delay, per-topic E2E latency, and E2E
       end-to-end.
   * **Survival line** — one line, e.g.
     `Survival: 5.84% of source · A→B: -0.2% · B→C: -98.0%`.
     The **ⓘ** next to it shows the full per-topic counts.
   * **Plot panes** — each pane has these dropdowns, each with an **ⓘ**:
     - **Plot:** Line, Line+markers, Scatter, Histogram, CDF, Rolling mean.
     - **X:** Time / Message index.
     - **Hop:** prefixed selections — `[Trans] A→B`, `[Trans] Total`,
       `[Trans] All hops overlay`, `[E2E] Source delay @ A`,
       `[E2E] @ Bᵢ`, `[E2E] End-to-end`, `[E2E] All E2E overlay`,
       and the killer one, `[Trans+E2E] Compare end-to-end` — overlays
       transport-total and E2E-end-to-end on the same axes; the gap
       between the two curves at any moment **is** the source delay at
       that moment.
     - **Y log** toggle, **Window** spin (rolling mean / histogram bins).
     - Y-axis units are dynamic: ns / µs / ms / s as you zoom; the cursor
       readout follows the same scale.
   * **+ Add Plot Pane** stacks more panes; the **Plots: Vertical / Horizontal**
     combo flips the splitter orientation.
   * **⛶** maximizes a pane within the splitter; **⇱** pops it out into a
     free-floating window you can move to a second monitor.
7. **Export CSV…** writes one row per matched message with all per-topic
   timestamps and per-hop latencies.
8. **Save…/Load…** under the chain list dump the full chain + tolerance +
   threshold to a JSON preset for later re-use.

### 5.2 Frequency analysis

1. Click **➕ Frequency Analysis** on the right of the tab bar — a new tab
   opens.
2. Select topics in the topic table and click **Add selected ➜ active tab**.
3. Set the **bin width** (default 1 s) and **smoothing window** (1 = none).
4. Click **Compute Frequencies ▶**. The loader uses the same caching path as
   latency analysis, so if you already analysed those topics for latency,
   this step is essentially free.
5. The stats table reports `msgs, duration, mean Hz, median Hz, min Hz,
   max Hz, stddev Hz` (mean/median/min/stddev are computed over **non-zero**
   bins so that idle stretches don't drag the average to zero).
6. The plot overlays publish-rate-vs-time for every selected topic on a
   single Hz axis. Bin width / smoothing recompute live without touching the
   bag again.

### 5.3 Multiple analyses

The right side is a tab widget. Run as many analyses in parallel as you like:

* **➕ Latency Analysis** — new latency tab
* **➕ Frequency Analysis** — new frequency tab
* **⎘ Duplicate** — copies the active tab's chain/topics/parameters into a
  fresh tab (handy for A/B comparing tolerances or thresholds)
* **✎ Rename** — give tabs meaningful names
* Drag tabs to reorder; close them with the × on the tab.

### 5.4 Cache

* Per-topic parsed timestamps are cached in `~/.cache/bag_latency_gui/` keyed
  by `(absolute_bag_path, topic, latest_db3_mtime)`.
* The first read of a topic on a multi-GiB bag may take a few seconds; every
  subsequent read of the same topic is essentially a `pd.read_pickle()`.
* **Clear cache** in the top toolbar wipes the cache directory.

### 5.5 Window state

* Window geometry is persisted across runs (via `QSettings`).
* The 10 most recently opened bags are remembered. Click the ▾ next to
  **Open Bag Folder…** to jump directly to one.

---

## 6. Performance notes

* Each chain topic is read on its own Python thread by a
  `ThreadPoolExecutor`. Default worker count is `min(len(topics), CPU cores)`.
* Each thread opens its own read-only sqlite connection (sqlite connections
  are not thread-safe). `mmap_size = 8 GiB`, `cache_size = 256 MiB` and
  `temp_store = MEMORY` PRAGMAs are set per connection so the OS happily
  fills RAM with bag pages.
* The `header.stamp` value is extracted directly from the CDR-encoded bytes
  (see [`rosbag_analyzer/ALGORITHM.md`](rosbag_analyzer/ALGORITHM.md)). For most topics this skips
  `deserialize_message` entirely — the fast path is roughly an order of
  magnitude faster than full deserialization.
* Output arrays are pre-allocated from `metadata.yaml`'s message counts and
  resized geometrically when the metadata is missing, so there are no
  Python list-to-numpy conversions on the hot path.
* PyQtGraph plots use `setDownsampling(auto, peak)` and `setClipToView(True)`
  so even multi-million-point series stay smooth when zooming.

The only mutex in the codebase is a single `threading.Lock` guarding the
shared per-thread progress message dict in `loader.py` — no nested locking
means deadlocks are structurally impossible.

---

## 7. Limitations — what this tool is *not* good at

This is a competent first-pass exploratory analyzer, **not** a finished
fault-analysis platform. Be aware of the following before quoting numbers
out of context.

### Things that can mislead you

* **Approximate matches are causally blind.** When `header.stamp` is not
  preserved across the chain, the loader falls back to
  `merge_asof(direction="forward", tolerance=±X ms)`. That algorithm pairs
  upstream rows with the next downstream row purely by clock proximity —
  with no awareness of whether the two messages are actually related.
  Two unrelated topics can produce a non-zero "matched %" entirely from
  coincidental timing. Symptoms: a wide, near-uniform latency histogram
  whose width ≈ tolerance, mean ≈ tolerance/2.
  **Trust pipeline latency only when** *Match: exact* **is shown, or when
  the latency histogram is tight relative to the tolerance.**
* **The status-line / ⓘ explanations are descriptive, not a confidence
  score.** They tell you *which* match method was used, not whether the
  resulting numbers are trustworthy. No quality metric is attached to each
  reported value.
* **Header fast-path is decided once per topic.** If a publisher changes
  the message layout mid-bag (very rare — bag rotations across mismatched
  workspaces, ABI breaks), the byte-offset extractor would not re-validate
  and could report wrong stamps silently for the rest of that topic.

### Things it does not do

* **No region-of-interest stats.** A 5-second outage in a 4-hour bag
  disappears in the global p99. You can zoom the plot, but the **stats
  table is computed over all matched rows, not the visible window**.
* **No cross-chain / cross-pane correlation.** You can open multiple tabs
  and panes, but the tool will not tell you "every time chain A spikes,
  chain B also spikes 50 ms later". Plot panes also do not share x-axis
  zoom — visual correlation is manual.
* **No bag-vs-bag diff.** No overlay mode to compare a "good" bag against
  a "bad" bag for regression analysis.
* **No anomaly / change-point detection.** Stats summarise, they do not
  flag spikes, bursts of consecutive SLA violations, regime shifts, or
  outliers.
* **No QoS / DDS visibility.** Drops can be QoS-driven (queue overflow,
  unreliable QoS, deadline miss). The bag rows alone cannot show this and
  the tool does not parse QoS profile fields.
* **No payload-size correlation.** Latency vs message size is unavailable
  — typically the most useful axis for image-pipeline faults.
* **No annotations / bookmarks.** You can't tag "alarm fired at 13:42:11"
  so a colleague sees what you saw.
* **No headless / CLI mode.** Everything is GUI-driven. Not suitable as a
  CI gate without writing a separate driver around the programmatic API.
* **No automated report export** (HTML / PDF / per-bag summary).
* **No live ROS topic mode.** The tool reads bags only; it does not attach
  to a running graph.

### Suitable for

* Daily engineering questions: *"what's my p95 latency", "is this node
  dropping", "what's our pipeline rate"*.
* Pre-fix / post-fix sanity checks when the symptom is already known.
* Producing numbers for a status report **when the chain matches exactly**.

### Not suitable for

* Forensic root-cause investigation of a complex multi-node incident
  without additional tooling.
* Automated regression gating in CI.
* Any situation where the recipient of the number has no context to
  question whether the chain matched exactly or approximately.

### What would lift it to "production fault-analysis"

In rough priority order:

1. **Match-quality confidence score** attached to every reported number
   (exact = 1.0; approximate = histogram-tightness / tolerance ratio).
2. **Region-of-interest stats** — drag-select a time window, recompute.
3. **Synced x-axis across panes** + multi-chain overlay for correlation.
4. **Headless CLI** that emits a JSON summary for CI gating.
5. **Anomaly / burst detector** on top of the existing rolling mean.
6. **Bag-vs-bag diff view** for regression checks.

These are small, well-bounded additions; the modular layout makes each
land in one or two files.

---

## 8. Troubleshooting

| Symptom | Probable cause / fix |
|---|---|
| `ModuleNotFoundError: rosbag2_py` on Compute | ROS 2 not sourced. Run `source /opt/ros/humble/setup.bash`. |
| `Cannot import message class …` on Compute | The workspace defining that custom message isn't sourced. Source it and relaunch. |
| `Selected folder does not contain metadata.yaml` | Wrong folder picked. The bag folder must contain `metadata.yaml` *and* the `.db3` files at its top level. Use `unzip_bags.sh` to flatten zipped bags. |
| 0 matched messages, exact then approximate both fail | Either the chain is wrong (topics aren't causally linked) or the tolerance is too tight. Try doubling the tolerance, or pick a chain whose first stamp survives end-to-end. |
| GUI freezes briefly when opening | Reading `metadata.yaml` only; should be sub-second. If it stalls, check disk I/O on the bag folder. |
| First Compute is slow, second is instant | First run populates `~/.cache/bag_latency_gui/`. That is the intended behaviour. Use **Clear cache** to force re-reads. |

---

## 9. Programmatic API

If you want to use the same machinery from a notebook or a CI script:

```python
import sys
sys.path.insert(0, "/home/arka/ros_bag/rosbag_analyzer")
from metadata import BagMetadata
from reader import read_topic
from latency import compute_chain_latency, stats_table
from frequency import topic_rates, topic_rate_stats
from ros_imports import import_ros

bag = BagMetadata.from_path("/home/arka/ros_bag/20260419_050057")
_, deserialize_message, get_message = import_ros()

dfs = {
    t: read_topic(bag, t, get_message(bag.topics[t]), deserialize_message)
    for t in ["/topic_a", "/topic_b", "/topic_c"]
}

merged, method, counts, result = compute_chain_latency(
    dfs, ["/topic_a", "/topic_b", "/topic_c"], tolerance_ms=50.0)

print("match:", method, "  rows:", len(merged),
      "  E2E latency available:", result.has_e2e_latency)

# Plain-English explanation of what was computed and why:
for line in result.reasoning_lines():
    print(" -", line)

# One stats dict per series — transport hops, transport total,
# source delay, E2E-at-each-topic, and E2E end-to-end:
for r in stats_table(merged, ["/topic_a", "/topic_b", "/topic_c"],
                     threshold_ms=20.0):
    print(f"[{r['kind']:>9}] {r['hop']:<48}  mean={r['mean_ms']:.3f} ms")
```
