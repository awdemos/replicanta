"""Lazy lineage-based dataset (RDD-style) and visual renderer.

A tiny, dependency-free implementation of the RDD idea: transformations
build a DAG, execution is deferred until a collect/count/plot action, and
the lineage can be drawn as an SVG flow chart. Charts are also rendered as
terminal-friendly colored bars so they look good inside the TUI.

This module intentionally avoids pandas/numpy/matplotlib — it only uses the
standard library."""

from collections import Counter
from dataclasses import dataclass, field


@dataclass
class Node:
    """One node in the RDD lineage graph."""

    id: str
    label: str
    kind: str  # source / transform / action
    meta: dict = field(default_factory=dict)


class Dataset:
    """A lazy dataset with lineage tracking.

    Sources are plain Python iterables (lists, dicts, generators). Each
    transformation returns a new Dataset and records the operation without
    running it. Actions trigger execution and return concrete values.
    """

    _id_counter = 0

    def __init__(self, source=None, label="source", parent=None, op=None):
        Dataset._id_counter += 1
        self.id = f"n{Dataset._id_counter}"
        self.source = source
        self.label = label
        self.parent = parent
        self.op = op  # (name, fn) or None for a root source
        self._node = Node(self.id, label, "source" if parent is None else "transform")

    @classmethod
    def from_records(cls, records, label="records"):
        """Build a root dataset from a list of dict-like records."""
        return cls(source=list(records), label=label)

    @classmethod
    def from_beliefs(cls, store, label="beliefs"):
        """Convenience: build a dataset from an organism's beliefs map."""
        records = [
            {"object": o, "attribute": a, "value": v, "confidence": c} for (o, a, v), c in store.beliefs().items()
        ]
        return cls.from_records(records, label=label)

    @classmethod
    def from_activity(cls, store, label="activity"):
        """Convenience: build a dataset from activity counters."""
        records = [{"key": k, "count": v} for k, v in store.activity.items() if isinstance(v, (int, float))]
        return cls.from_records(records, label=label)

    @classmethod
    def from_memories(cls, store, label="memories"):
        """Convenience: build a dataset from episodic memories."""
        return cls.from_records(store.memory, label=label)

    @classmethod
    def from_mood_history(cls, store, label="mood history"):
        """Convenience: build a dataset from recorded mood history."""
        moods = getattr(store, "mood_history", []) or []
        return cls.from_records(moods, label=label)

    def map(self, fn, label="map"):
        """Return a new dataset with ``fn`` applied to each record."""
        return Dataset(parent=self, label=label, op=("map", fn))

    def filter(self, predicate, label="filter"):
        """Return a new dataset keeping records where predicate(record)."""
        return Dataset(parent=self, label=label, op=("filter", predicate))

    def group_by(self, key_fn, label="group_by"):
        """Return a new dataset grouped into {key: [records]} buckets."""
        return Dataset(parent=self, label=label, op=("group_by", key_fn))

    def sort(self, key_fn, reverse=False, label="sort"):
        """Return a new dataset sorted by key_fn(record)."""
        return Dataset(parent=self, label=label, op=("sort", (key_fn, reverse)))

    def top(self, n, key_fn, reverse=True, label="top"):
        """Return a new dataset with the top-n records by key_fn."""
        return Dataset(parent=self, label=label, op=("top", (n, key_fn, reverse)))

    def limit(self, n, label="limit"):
        """Return a new dataset with at most n records."""
        return Dataset(parent=self, label=label, op=("limit", n))

    def reduce(self, reducer, label="reduce"):
        """Return a new dataset reduced by ``fn(records) -> record``."""
        return Dataset(parent=self, label=label, op=("reduce", reducer))

    def lineage(self):
        """Return a list of Nodes from root to this dataset, plus edges."""
        nodes, edges = [], []

        def walk(ds):
            if ds.parent is not None:
                walk(ds.parent)
                edges.append((ds.parent.id, ds.id, ds.op[0] if ds.op else ""))
            nodes.append(ds._node)

        walk(self)
        return nodes, edges

    def _execute(self):
        """Execute the lineage and return a list of records."""
        if self.parent is None:
            data = list(self.source) if self.source is not None else []
        else:
            data = self.parent._execute()
        if self.op is None:
            return data
        name, arg = self.op
        if name == "map":
            return [arg(r) for r in data]
        if name == "filter":
            return [r for r in data if arg(r)]
        if name == "group_by":
            groups = {}
            for r in data:
                k = arg(r)
                groups.setdefault(k, []).append(r)
            return [{"key": k, "count": len(v), "values": v} for k, v in groups.items()]
        if name == "sort":
            key_fn, reverse = arg
            return sorted(data, key=key_fn, reverse=reverse)
        if name == "top":
            n, key_fn, reverse = arg
            return sorted(data, key=key_fn, reverse=reverse)[:n]
        if name == "limit":
            return data[:arg]
        if name == "reduce":
            return arg(data)
        raise ValueError(f"unknown op {name}")

    def collect(self):
        """Action: execute and return the records."""
        return self._execute()

    def count(self):
        """Action: execute and return the record count."""
        return len(self._execute())

    def counts_by(self, key_fn):
        """Action: execute and return a Counter of key frequencies."""
        return Counter(key_fn(r) for r in self._execute())


# -----------------------------------------------------------------------------
# Rendering helpers
# -----------------------------------------------------------------------------


def _escape(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _fmt_number(value):
    """Human-readable number: 1.4K, 2.3M, 42."""
    value = float(value)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:.0f}"


def _categorical_palette(n):
    """Return n distinct, accessible colors for charts."""
    base = [
        "#60a5fa",  # blue
        "#34d399",  # emerald
        "#fbbf24",  # amber
        "#f87171",  # red
        "#a78bfa",  # violet
        "#22d3ee",  # cyan
        "#fb923c",  # orange
        "#e879f9",  # fuchsia
        "#2dd4bf",  # teal
        "#f472b6",  # pink
    ]
    if n <= len(base):
        return base[:n]
    # cycle with small hue shift if we run out
    return [base[i % len(base)] for i in range(n)]


# -----------------------------------------------------------------------------
# Lineage SVG
# -----------------------------------------------------------------------------


def render_lineage_svg(dataset, title="RDD lineage", width=800, height=260):
    """Render the dataset lineage as a horizontal flow-chart SVG."""
    nodes, edges = dataset.lineage()
    n = len(nodes)
    if n == 0:
        return _empty_svg(title, width, height)

    pad_x = 70
    gap = (width - 2 * pad_x) / max(n - 1, 1)
    cy = height // 2
    radius = 32

    def node_xy(i):
        return (pad_x + i * gap, cy)

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" style="background:#0b1220">',
        "  <defs>",
        '    <linearGradient id="nodeGrad" x1="0%" y1="0%" x2="0%" y2="100%">',
        '      <stop offset="0%" style="stop-color:#3b82f6"/>',
        '      <stop offset="100%" style="stop-color:#1d4ed8"/>',
        "    </linearGradient>",
        '    <filter id="shadow" x="-20%" y="-20%" width="140%" height="140%">',
        '      <feDropShadow dx="1" dy="2" stdDeviation="2.5" flood-color="#000" flood-opacity="0.35"/>',
        "    </filter>",
        '    <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">',
        '      <path d="M 0 0 L 10 5 L 0 10 z" fill="#94a3b8"/>',
        "    </marker>",
        "  </defs>",
        f'  <rect width="{width}" height="{height}" fill="#0b1220" rx="14"/>',
        f'  <text x="{width // 2}" y="32" text-anchor="middle" fill="#e2e8f0" font-size="18" font-family="sans-serif" font-weight="600">{_escape(title)}</text>',
    ]

    for src_id, dst_id, label in edges:
        src_i = next(i for i, nd in enumerate(nodes) if nd.id == src_id)
        dst_i = next(i for i, nd in enumerate(nodes) if nd.id == dst_id)
        x1, y1 = node_xy(src_i)
        x2, y2 = node_xy(dst_i)
        x1 += radius
        x2 -= radius
        lines.append(
            f'  <line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#64748b" stroke-width="2.5" marker-end="url(#arrow)"/>'
        )
        if label:
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            lines.append(
                f'  <rect x="{mx - len(label) * 3 - 6}" y="{my - 18}" width="{len(label) * 6 + 12}" height="18" rx="4" fill="#1e293b" opacity="0.9"/>'
            )
            lines.append(
                f'  <text x="{mx}" y="{my - 6}" text-anchor="middle" fill="#e2e8f0" font-size="11" font-family="sans-serif">{_escape(label)}</text>'
            )

    for i, node in enumerate(nodes):
        x, y = node_xy(i)
        kind_color = "#f59e0b" if node.kind == "source" else "#8b5cf6"
        lines += [
            f'  <circle cx="{x}" cy="{y}" r="{radius}" fill="url(#nodeGrad)" filter="url(#shadow)" stroke="{kind_color}" stroke-width="3"/>',
            f'  <text x="{x}" y="{y + 5}" text-anchor="middle" fill="#ffffff" font-size="13" font-family="sans-serif" font-weight="bold">{_escape(node.label)}</text>',
        ]

    lines.append("</svg>")
    return "\n".join(lines)


def _empty_svg(title, width, height):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" style="background:#0b1220">'
        f'<rect width="{width}" height="{height}" fill="#0b1220" rx="14"/>'
        f'<text x="{width // 2}" y="{height // 2}" text-anchor="middle" fill="#94a3b8" font-size="14" font-family="sans-serif">{_escape(title)} — no data</text>'
        "</svg>"
    )


# -----------------------------------------------------------------------------
# Bar chart SVG
# -----------------------------------------------------------------------------


def render_bar_chart_svg(records, value_key, label_key, title="chart", caption="", cycle=None, width=800, height=460):
    """Render a horizontal bar chart SVG from grouped/sorted records."""
    if not records:
        return _empty_svg(title, width, height)

    values = [float(r.get(value_key, 0) or 0) for r in records]
    labels = [str(r.get(label_key, "?")) for r in records]
    max_v = max(values) if values else 1

    margin = {"top": 64, "right": 80, "bottom": 48, "left": 160}
    chart_w = width - margin["left"] - margin["right"]
    chart_h = height - margin["top"] - margin["bottom"]
    bar_h = min(32, chart_h // max(len(records), 1))
    gap = max(6, (chart_h - len(records) * bar_h) // max(len(records) - 1, 1))
    colors = _categorical_palette(len(records))

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" style="background:#0b1220">',
        "  <defs>",
        '    <filter id="barShadow" x="-10%" y="-10%" width="120%" height="130%">',
        '      <feDropShadow dx="1" dy="2" stdDeviation="2" flood-color="#000" flood-opacity="0.25"/>',
        "    </filter>",
        "  </defs>",
        f'  <rect width="{width}" height="{height}" fill="#0b1220" rx="14"/>',
        f'  <text x="{width // 2}" y="34" text-anchor="middle" fill="#e2e8f0" font-size="18" font-family="sans-serif" font-weight="600">{_escape(title)}</text>',
        f'  <text x="{width // 2}" y="54" text-anchor="middle" fill="#94a3b8" font-size="12" font-family="sans-serif">{_escape(caption)}</text>',
        f'  <text x="{width - margin["right"]}" y="{height - 14}" text-anchor="end" fill="#64748b" font-size="12" font-family="sans-serif">{len(records)} categories · max {_fmt_number(max_v)}</text>',
        f'  <text x="{margin["left"] + chart_w / 2}" y="{height - 32}" text-anchor="middle" fill="#94a3b8" font-size="12" font-family="sans-serif">value (higher is more)</text>',
    ]

    # grid lines
    for i in range(5):
        gx = margin["left"] + (chart_w * i / 4)
        lines += [
            f'  <line x1="{gx}" y1="{margin["top"]}" x2="{gx}" y2="{margin["top"] + chart_h}" stroke="#1e293b" stroke-width="1"/>',
            f'  <text x="{gx}" y="{margin["top"] - 8}" text-anchor="middle" fill="#64748b" font-size="11" font-family="sans-serif">{_fmt_number(max_v * i / 4)}</text>',
        ]

    for i, (label, value) in enumerate(zip(labels, values, strict=False)):
        y = margin["top"] + i * (bar_h + gap)
        bar_w = (value / max_v) * chart_w if max_v else 0
        color = colors[i % len(colors)]
        display = _fmt_number(value)
        lines += [
            f'  <text x="{margin["left"] - 12}" y="{y + bar_h // 2 + 4}" text-anchor="end" fill="#94a3b8" font-size="13" font-family="sans-serif">{_escape(label[:28])}</text>',
            f'  <rect x="{margin["left"]}" y="{y}" width="{bar_w}" height="{bar_h}" rx="5" fill="{color}" filter="url(#barShadow)"/>',
            f'  <text x="{margin["left"] + bar_w + 8}" y="{y + bar_h // 2 + 4}" fill="#e2e8f0" font-size="12" font-family="sans-serif" font-weight="500">{display}</text>',
        ]

    if cycle is not None:
        lines.append(
            f'  <text x="{width - 12}" y="{height - 32}" text-anchor="end" fill="#64748b" font-size="11" font-family="sans-serif">rendered at cycle {cycle}</text>'
        )
    lines.append("</svg>")
    return "\n".join(lines)


# -----------------------------------------------------------------------------
# Text chart for TUI
# -----------------------------------------------------------------------------


def render_text_bars(records, value_key, label_key, width=40, max_rows=18, caption=""):
    """Return a Rich-ready multi-line text chart for the TUI.

    Uses colored Unicode block characters and simple alignment.
    """
    if not records:
        return "(no data)" + (f"\n{caption}" if caption else "")

    records = records[:max_rows]
    labels = [str(r.get(label_key, "?")) for r in records]
    values = [float(r.get(value_key, 0) or 0) for r in records]
    max_v = max(values) if values else 1
    max_label = max(len(label) for label in labels)
    lines = []
    colors = ["cyan", "green", "yellow", "red", "magenta", "blue", "bright_cyan", "bright_green"]

    for i, (label, value) in enumerate(zip(labels, values, strict=False)):
        frac = value / max_v
        filled = max(1, round(frac * width)) if value else 0
        bar = "█" * filled + "░" * (width - filled)
        color = colors[i % len(colors)]
        lines.append(f"{label.rjust(max_label)} │[{color}]{bar}[/{color}] {_fmt_number(value)}")
    if caption:
        lines.append("")
        lines.append(f"[dim]{caption}[/dim]")
    return "\n".join(lines)


# -----------------------------------------------------------------------------
# Preset datasets for the organism
# -----------------------------------------------------------------------------


def belief_by_object_dataset(store):
    """Group beliefs by object, weighted by confidence."""
    return (
        Dataset.from_beliefs(store, label="beliefs")
        .map(lambda r: {"object": r["object"], "score": r["confidence"]}, label="extract")
        .group_by(lambda r: r["object"], label="by object")
        .map(
            lambda g: {"key": g["key"], "count": round(sum(r["score"] for r in g["values"]), 2)}, label="sum confidence"
        )
        .sort(lambda r: -r["count"], label="top")
        .top(15, lambda r: r["count"], label="top 15")
    )


def belief_attribute_dataset(store):
    """Group beliefs by (object, attribute) pair, confidence-weighted."""
    return (
        Dataset.from_beliefs(store, label="beliefs")
        .map(lambda r: {"pair": f"{r['object']}.{r['attribute']}", "score": r["confidence"]}, label="extract pair")
        .group_by(lambda r: r["pair"], label="by pair")
        .map(
            lambda g: {"key": g["key"], "count": round(sum(r["score"] for r in g["values"]), 2)}, label="sum confidence"
        )
        .sort(lambda r: -r["count"], label="top")
        .top(15, lambda r: r["count"], label="top 15")
    )


def activity_dataset(store):
    """Top numeric activity counters."""
    return (
        Dataset.from_activity(store, label="activity")
        .filter(lambda r: r.get("count", 0) > 0, label="positive")
        .sort(lambda r: -r["count"], label="top")
        .top(12, lambda r: r["count"], label="top 12")
    )


def memory_kind_dataset(store):
    """Group memories by kind and count them."""
    return (
        Dataset.from_memories(store, label="memories")
        .filter(lambda r: "kind" in r, label="with kind")
        .group_by(lambda r: r["kind"], label="by kind")
        .sort(lambda r: -r["count"], label="top")
        .top(15, lambda r: r["count"], label="top 15")
    )


def recent_memories_dataset(store):
    """Most recent memories by cycle."""
    return (
        Dataset.from_memories(store, label="memories")
        .filter(lambda r: "cycle" in r and "kind" in r, label="dated")
        .map(
            lambda r: {"key": f"{r['kind']} (c{r.get('cycle', '?')})", "count": 1, "cycle": r.get("cycle", 0)},
            label="label",
        )
        .sort(lambda r: -r["cycle"], label="recent first")
        .limit(15, label="last 15")
    )


def mood_dataset(store):
    """Distribution of mood history."""
    return (
        Dataset.from_mood_history(store, label="mood history")
        .filter(lambda r: "mood" in r, label="with mood")
        .group_by(lambda r: r["mood"], label="by mood")
        .sort(lambda r: -r["count"], label="top")
        .top(12, lambda r: r["count"], label="top 12")
    )


def stress_dataset(store):
    """Stress band distribution from store history if present."""
    history = getattr(store, "stress_history", []) or []
    if not history:
        # fall back to the current band only
        band = getattr(store, "stress_band", 0)
        history = [{"band": band}] if band else []
    return (
        Dataset.from_records(history, label="stress history")
        .map(lambda r: {"key": f"band {r.get('band', 0)}", "count": 1}, label="label")
        .group_by(lambda r: r["key"], label="by band")
        .sort(lambda r: -r["count"], label="top")
    )


PRESETS = {
    "beliefs": belief_by_object_dataset,
    "attributes": belief_attribute_dataset,
    "activity": activity_dataset,
    "memories": memory_kind_dataset,
    "recent": recent_memories_dataset,
    "mood": mood_dataset,
    "sentiment": mood_dataset,
    "stress": stress_dataset,
}

CAPTIONS = {
    "beliefs": "Total confidence mass of beliefs grouped by object.",
    "attributes": "Total confidence mass of beliefs grouped by object.attribute.",
    "activity": "Top positive numeric activity counters.",
    "memories": "Count of episodic memories grouped by kind.",
    "recent": "Most recent memory events ordered by organism cycle.",
    "mood": "Distribution of recorded mood states over time.",
    "sentiment": "Distribution of recorded mood states over time.",
    "stress": "Distribution of stress band recordings.",
    "summary": "Auto-selected richest view from current organism state.",
}


def _best_summary_kind(store):
    """Pick the richest preset for an automatic summary view."""
    scores = [
        ("beliefs", len(store.beliefs())),
        ("attributes", len(store.beliefs())),
        ("activity", sum(1 for v in store.activity.values() if isinstance(v, (int, float)) and v > 0)),
        ("memories", len(store.memory)),
        ("recent", len(store.memory)),
        ("mood", len(getattr(store, "mood_history", []) or [])),
        ("stress", len(getattr(store, "stress_history", []) or [])),
    ]
    scores.sort(key=lambda kv: -kv[1])
    return scores[0][0] if scores and scores[0][1] else "beliefs"


def build_chart(kind, store):
    """Build a Dataset and render both SVG and text chart for the given kind."""
    resolved_kind = _best_summary_kind(store) if kind == "summary" else kind
    builder = PRESETS.get(resolved_kind, belief_by_object_dataset)
    ds = builder(store)
    nodes, edges = ds.lineage()
    records = ds.collect()
    label_key = "key"
    value_key = "count"
    title_name = resolved_kind.replace("_", " ").title()
    caption = CAPTIONS.get(resolved_kind, "")
    cycle = getattr(store, "cycle", None)
    lineage_svg = render_lineage_svg(ds, title=f"RDD lineage · {title_name}")
    chart_svg = render_bar_chart_svg(records, value_key, label_key, title=f"{title_name}", caption=caption, cycle=cycle)
    text_chart = render_text_bars(records, value_key, label_key, caption=caption)
    return {
        "kind": resolved_kind,
        "caption": caption,
        "records": records,
        "lineage": {"nodes": [n.__dict__ for n in nodes], "edges": edges},
        "lineage_svg": lineage_svg,
        "chart_svg": chart_svg,
        "text_chart": text_chart,
    }


def supported_kinds():
    """Return the list of supported visualization kinds."""
    return list(PRESETS.keys()) + ["summary"]
