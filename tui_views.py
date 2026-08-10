"""View builders for the organism TUI's inspection tabs.

Mind and Memory are plain strings; Inner also has a rich-renderable
version with gauges and panels. No textual imports — unit testable
without a terminal."""

import json

from rich.panel import Panel
from rich.text import Text

import activity
from learning import describe

STYLE_USER = "cyan"
STYLE_ORG = "green"
STYLE_DIM = "dim"

_BELIEF_LIMIT = 12
_RULE_LIMIT = 10


def conf_bar(conf, width=5):
    """A tiny confidence meter: ▮▮▮▮▯."""
    filled = max(0, min(width, round(conf * width)))
    return "▮" * filled + "▯" * (width - filled)


def chat_card(who, text, timestamp=None, border_style=None):
    """A consistent panel card for chat utterances."""
    border_style = border_style or (STYLE_USER if who == "you" else STYLE_ORG)
    title = f"{who} · {timestamp}" if timestamp else who
    return Panel(
        Text(text),
        title=title,
        title_align="left",
        border_style=border_style,
        padding=(0, 1),
    )


def _human_size(n):
    """Bytes -> compact human form (B, KB, MB)."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def mind_view(org):
    """The Mind tab: a human-readable snapshot of what the organism is
    currently holding as true, aiming for, and paying attention to.
    Read-only; rebuilt on every tick."""
    m = org.metrics()
    lines = ["top beliefs", "",
             ("(▮ = confidence; more blocks means the organism holds it "
              "more strongly)"), ""]
    top = sorted(org.store.beliefs().items(), key=lambda kv: -kv[1])
    for (obj, attr, val), conf in top[:_BELIEF_LIMIT]:
        lines.append(f"{conf_bar(conf)} {conf:.2f} {obj}:{attr}={val}")
    if not top:
        lines += [("(no beliefs yet — say something like \"my name is "
                   "Sam\" or \"i like rain\")"), ""]
    if org.store.goals:
        lines += ["", "goals", ""]
        active = org.store.active_goal()
        if active:
            strategy = active.get("strategy")
            strategy_line = f"   strategy: {strategy}" if strategy else ""
            lines.append(
                f"→ now trying: {active['text']} "
                f"(since cycle {active['created_cycle']}){strategy_line}")
        for g in [g for g in org.store.goals
                  if g["done_cycle"] is not None][-3:]:
            lines.append(f"   done (cycle {g['done_cycle']}): {g['text']}")
    skill_store = getattr(org, "skills", None)
    skill_list = skill_store.list() if skill_store is not None else []
    if skill_list:
        lines += ["", "skills", "",
                  "(techniques the organism has learned and can reuse)", ""]
        for s in skill_list[:8]:
            lines.append(f"{s.name} (used {s.uses}×) — when {s.when}")
    if org.store.rules:
        lines += ["", "committed rules", "",
                  "(derived patterns the organism treats as reliable)", ""]
        lines += [text for text, _depth in org.store.rules[:_RULE_LIMIT]]
    if org.store.attention:
        pairs = sorted(f"{a}={v}" for a, v in org.store.attention)
        lines += ["", "attention: " + ", ".join(pairs),
                  ("(only beliefs matching the focus window strongly "
                   "influence replies right now)")]
    lines += ["",
              (f"genome: {m.belief_count} beliefs · {m.rule_count} rules · "
               f"depth {m.total_depth} · consciousness score "
               f"{m.score():.1f}")]
    activity_lines = activity.summary_lines(org.store)
    if activity_lines:
        lines += [""] + activity_lines
    return "\n".join(lines)


def memory_view(org):
    """The Memory tab: the organism's episodic diary, what it has learned
    about the user, how the user describes it, and any saved artifacts.
    Read-only; rebuilt on every tick."""
    lines = ["episodes", "",
             ("(notable moments from the organism's life, stamped by the "
              "cycle they happened)"), ""]
    for ep in org.store.memory:
        lines.append(f"cycle {ep['cycle']:<4} {ep['kind']:<8} {ep['text']}")
    if not org.store.memory:
        lines += [("(nothing remembered yet — events appear here when the "
                   "organism dreams, learns, or fades)"), ""]
    beliefs = org.store.beliefs()
    user_facts = [describe(b) for b in beliefs if b[0] == "user"]
    if user_facts:
        lines += ["", "what it knows about you", "",
                  "(facts extracted from what you said)", ""]
        lines += [f"- {f}" for f in user_facts]
    views = [v for (o, a, v) in beliefs if (o, a) == ("self", "described_as")]
    if views:
        lines += ["", "what you said it is", "",
                  "(labels you have given the organism)", ""]
        lines += [f"- {v}" for v in views]
    artifacts = org.store.dir_path / "artifacts"
    if artifacts.is_dir():
        files = sorted(p for p in artifacts.iterdir() if p.is_file())
        if files:
            lines += ["", "artifacts", "",
                      "(files the organism has written)", ""]
            lines += [f"- {p.name} ({_human_size(p.stat().st_size)})"
                      for p in files]
    return "\n".join(lines)


def _mental_state(org):
    """Current mental-state scalars: (label, value) pairs, mood last.
    Values are the raw store attributes when present (they are floats in
    [0, 1]); mood is a string belief, not a scalar."""
    store = org.store
    scalars = []
    for attr, label in (("arousal", "arousal"),
                        ("stress", "stress"),
                        ("rationality", "rationality"),
                        ("irrationality", "irrationality")):
        value = getattr(store, attr, None)
        if isinstance(value, float):
            scalars.append((label, value))
    mood = store.belief_value("self", "mood")
    return scalars + ([("mood", mood)] if mood is not None else [])


def _pending_proposal(org):
    """The staged extension patch (kind + one-line preview), or None."""
    path = org.store.dir_path / "artifacts" / "extensions.json"
    try:
        reg = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    entry = reg.get("pending")
    if not entry:
        return None
    kind = entry.get("kind", "?")
    if kind == "pattern":
        preview = (f"{entry.get('template', '?')} "
                   f"← /{entry.get('regex', '?')}/")
    else:
        preview = entry.get("text", "?")
    return f"{kind}: {preview}"


def _perpetuation_stats(store):
    """The perpetuation-loop counters shared by inner_renderable and
    inner_view; None when there is no activity yet."""
    a = store.activity
    if not a:
        return None
    return {
        "cycle": max(store.cycle, 1),
        "tried": a.get("rules_tried", 0),
        "derived": a.get("derivations", 0),
        "committed": a.get("rules_committed", 0),
        "promoted": a.get("dreams_promoted", 0),
        "discarded": a.get("dreams_discarded", 0),
    }


def _belief_style(obj):
    if obj == "self":
        return "magenta"
    if obj == "user":
        return "cyan"
    return "blue"


def mind_renderable(org):
    """The Mind tab as rich renderables: top beliefs with confidence bars,
    goals, skills, rules, attention, and genome/activity footer. Rebuilt
    every tick."""
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    panels = []
    store = org.store

    top = sorted(org.store.beliefs().items(), key=lambda kv: -kv[1])
    if top:
        grid = Table.grid(padding=(0, 1))
        grid.add_column(style="dim", justify="right")
        grid.add_column(justify="left")
        grid.add_column(justify="left")
        grid.add_column(justify="right", style="dim")
        for (obj, attr, val), conf in top[:_BELIEF_LIMIT]:
            style = _belief_style(obj)
            grid.add_row(
                f"{conf:.2f}",
                Text(conf_bar(conf), style=style),
                Text(f"{obj}:{attr}={val}", style=style),
                f"({obj})",
            )
        caption = Text(
            "▮ = confidence; more blocks means the organism holds it "
            "more strongly",
            style="dim",
        )
        panels.append(
            Panel(
                Group(grid, Text(""), caption),
                title="top beliefs",
                border_style="cyan",
            )
        )

    if store.goals:
        grid = Table.grid(padding=(0, 1))
        grid.add_column(style="bold")
        grid.add_column(justify="left")
        active = store.active_goal()
        if active:
            strategy = active.get("strategy")
            strategy_text = f"  · strategy: {strategy}" if strategy else ""
            grid.add_row(
                Text("→", style="green"),
                Text.assemble(
                    ("now trying: ", "bold"),
                    (active["text"], ""),
                    (f"  (since cycle {active['created_cycle']})", "dim"),
                    (strategy_text, "dim"),
                ),
            )
        completed = [g for g in store.goals if g["done_cycle"] is not None][-3:]
        for g in completed:
            grid.add_row(
                Text("✓", style="dim"),
                Text.assemble(
                    (f"done (cycle {g['done_cycle']}): ", "dim"),
                    (g["text"], "italic"),
                ),
            )
        panels.append(Panel(grid, title="goals", border_style="green"))

    skill_store = getattr(org, "skills", None)
    skill_list = skill_store.list() if skill_store is not None else []
    if skill_list:
        grid = Table.grid(padding=(0, 1))
        grid.add_column(style="bold")
        grid.add_column(justify="left")
        for s in skill_list[:8]:
            grid.add_row(
                Text(s.name, style="yellow"),
                Text.assemble(
                    (f"used {s.uses}×", "dim"),
                    ("  ·  ", "dim"),
                    (f"when {s.when}", ""),
                ),
            )
        caption = Text(
            "techniques the organism has learned and can reuse", style="dim"
        )
        panels.append(
            Panel(Group(grid, Text(""), caption), title="skills",
                  border_style="yellow")
        )

    if store.rules:
        grid = Table.grid(padding=(0, 1))
        grid.add_column(justify="left")
        for text, _depth in store.rules[:_RULE_LIMIT]:
            grid.add_row(Text(text, style="blue"))
        caption = Text(
            "derived patterns the organism treats as reliable", style="dim"
        )
        panels.append(
            Panel(Group(grid, Text(""), caption), title="committed rules",
                  border_style="blue")
        )

    if store.attention:
        pairs = sorted(f"{a}={v}" for a, v in store.attention)
        body = Group(
            Text(", ".join(pairs)),
            Text(
                "only beliefs matching the focus window strongly influence "
                "replies right now",
                style="dim",
            ),
        )
        panels.append(
            Panel(body, title="attention", border_style="yellow")
        )

    m = org.metrics()
    genome_text = (f"{m.belief_count} beliefs · {m.rule_count} rules · "
                   f"depth {m.total_depth} · consciousness score "
                   f"{m.score():.1f}")
    footer = Text.assemble(
        ("genome: ", "bold"),
        (genome_text, ""),
    )
    activity_lines = activity.summary_lines(store)
    if activity_lines:
        footer = Group(footer, Text(""), Text("\n".join(activity_lines)))
    panels.append(Panel(footer, title="activity", border_style="dim"))

    return Group(*panels)


def memory_renderable(org):
    """The Memory tab as rich renderables: episodes, user facts,
    self-description labels, and saved artifacts. Rebuilt every tick."""
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    panels = []
    store = org.store

    if store.memory:
        grid = Table.grid(padding=(0, 1))
        grid.add_column(style="dim", justify="right")
        grid.add_column(style="bold")
        grid.add_column(justify="left")
        for ep in store.memory:
            grid.add_row(
                f"cycle {ep['cycle']}",
                ep["kind"],
                ep["text"],
            )
        panels.append(Panel(grid, title="episodes", border_style="magenta"))
    else:
        panels.append(
            Panel(
                Text(
                    "nothing remembered yet — events appear here when the "
                    "organism dreams, learns, or fades",
                    style="dim",
                ),
                title="episodes",
                border_style="magenta",
            )
        )

    beliefs = store.beliefs()
    user_facts = [describe(b) for b in beliefs if b[0] == "user"]
    if user_facts:
        grid = Table.grid(padding=(0, 1))
        grid.add_column()
        for f in user_facts:
            grid.add_row(Text(f"• {f}"))
        caption = Text("facts extracted from what you said", style="dim")
        panels.append(
            Panel(
                Group(grid, Text(""), caption),
                title="what it knows about you",
                border_style="cyan",
            )
        )

    views = [v for (o, a, v) in beliefs if (o, a) == ("self", "described_as")]
    if views:
        grid = Table.grid(padding=(0, 1))
        grid.add_column()
        for v in views:
            grid.add_row(Text(f"• {v}"))
        caption = Text("labels you have given the organism", style="dim")
        panels.append(
            Panel(
                Group(grid, Text(""), caption),
                title="what you said it is",
                border_style="green",
            )
        )

    artifacts = store.dir_path / "artifacts"
    if artifacts.is_dir():
        files = sorted(p for p in artifacts.iterdir() if p.is_file())
        if files:
            grid = Table.grid(padding=(0, 1))
            grid.add_column(style="bold")
            grid.add_column(justify="right", style="dim")
            for p in files:
                grid.add_row(p.name, _human_size(p.stat().st_size))
            caption = Text("files the organism has written", style="dim")
            panels.append(
                Panel(
                    Group(grid, Text(""), caption),
                    title="artifacts",
                    border_style="blue",
                )
            )

    return Group(*panels)


def inner_renderable(org):
    """The Inner tab as rich renderables: mental-state gauges, the
    perpetuation loop with progress bars, the thought arena and any
    pending proposal. Rebuilt every tick."""
    from rich.bar import Bar
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    store = org.store
    panels = []

    state = _mental_state(org)
    if state:
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="bold")
        grid.add_column(justify="left")
        grid.add_column(justify="right", style="dim")
        for label, value in state:
            if label == "mood":
                grid.add_row(label, "", value)
            else:
                color = {
                    "arousal": "magenta",
                    "stress": "red",
                    "rationality": "green",
                    "irrationality": "yellow",
                }.get(label, "blue")
                grid.add_row(
                    label,
                    Bar(size=1.0, begin=0.0, end=value, width=24, color=color),
                    f"{value:.2f}",
                )
        panels.append(Panel(grid, title="mental state", border_style="cyan"))

    stats = _perpetuation_stats(store)
    if stats:
        cycle = stats["cycle"]
        tried = stats["tried"]
        derived = stats["derived"]
        committed = stats["committed"]
        promoted = stats["promoted"]
        discarded = stats["discarded"]
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="bold")
        grid.add_column(justify="left")
        grid.add_column(justify="right", style="dim")
        grid.add_row(
            "yield",
            Bar(
                size=1.0,
                begin=0.0,
                end=derived / max(tried, 1),
                width=24,
                color="green",
            ),
            f"{tried} questions → {derived} derivations "
            f"({derived / max(tried, 1):.0%} yield) over {cycle} cycles",
        )
        grid.add_row(
            "rules",
            Bar(
                size=1.0,
                begin=0.0,
                end=min(committed / max(derived, 1), 1.0),
                width=24,
                color="blue",
            ),
            f"{committed} rules committed · {promoted} dreams "
            f"promoted / {discarded} discarded",
        )
        panels.append(
            Panel(grid, title="perpetuation loop", border_style="magenta")
        )

    arena = activity.summary_lines(store)
    if arena:
        panels.append(
            Panel(Text("\n".join(arena)), title="thought arena",
                  border_style="yellow")
        )

    proposal = _pending_proposal(org)
    if proposal:
        auto = getattr(getattr(org, "store", None), "auto_apply_patches", True)
        action = "auto-applied" if auto else "/approve to apply · /reject to discard"
        panels.append(
            Panel(
                Group(
                    Text(proposal),
                    Text(f"({action})", style="dim"),
                ),
                title="pending proposal",
                border_style="yellow",
            )
        )

    if not panels:
        return Text("(nothing to show yet)")
    return Group(*panels)


def inner_view(org):
    """The Inner tab: the organism's internal activity — mental-state
    scalars, the perpetuation loop (how questions become derivations,
    rules and dreams), thought-arena metabolism, and any pending extension
    proposal. Read-only; rebuilt on every tick."""
    store = org.store
    lines = ["mental state", ""]
    state = _mental_state(org)
    if state:
        for label, value in state:
            if label == "mood":
                lines.append(f"mood: {value}")
            else:
                lines.append(f"{conf_bar(value)} {value:.2f} {label}")
    else:
        lines.append("(no mental state yet)")
    stats = _perpetuation_stats(store)
    if stats:
        cycle = stats["cycle"]
        tried = stats["tried"]
        derived = stats["derived"]
        committed = stats["committed"]
        promoted = stats["promoted"]
        discarded = stats["discarded"]
        lines += ["", "perpetuation loop", ""]
        lines.append(
            f"{tried} questions → {derived} derivations "
            f"({derived / max(tried, 1):.0%} yield) over {cycle} cycles")
        lines.append(
            f"{committed} rules committed ({committed / max(derived, 1):.0%}"
            f" of derivations) · {promoted} dreams promoted / "
            f"{discarded} discarded")
    else:
        lines += ["", "perpetuation loop", ""]
        lines.append("(no activity yet)")
    arena = activity.summary_lines(store)
    if arena:
        lines += ["", "thought arena", ""] + arena
    proposal = _pending_proposal(org)
    if proposal:
        lines += ["", "pending proposal", ""]
        lines.append(proposal)
        auto = getattr(getattr(org, "store", None), "auto_apply_patches", True)
        lines.append("auto-applied" if auto
                     else "(/approve to apply · /reject to discard)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Neural memory grid (cells tab)
# ---------------------------------------------------------------------------

import hashlib

CELLS_COLS = 48
CELLS_ROWS = 20
_CELLS_BG = "#0b0f1a"


def _hex_to_rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def _lerp_color(low, high, t):
    lo = _hex_to_rgb(low)
    hi = _hex_to_rgb(high)
    t = max(0.0, min(1.0, t))
    r = round(lo[0] + (hi[0] - lo[0]) * t)
    g = round(lo[1] + (hi[1] - lo[1]) * t)
    b = round(lo[2] + (hi[2] - lo[2]) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def _cell_color(kind, conf):
    if kind == "self":
        return _lerp_color("#7f1d1d", "#f472b6", conf)
    if kind == "rule":
        return _lerp_color("#065f46", "#34d399", conf)
    if kind == "memory":
        return _lerp_color("#78350f", "#fbbf24", conf)
    if kind == "goal":
        return _lerp_color("#701a75", "#e879f9", conf)
    return _lerp_color("#1e3a8a", "#22d3ee", conf)


def _stable_index(key, capacity):
    digest = hashlib.md5(key.encode()).digest()
    return int.from_bytes(digest[:8], "big") % capacity


def cells_layout(org):
    """Top-down neural memory grid: one cell per belief/rule/memory/goal.

    Colors encode the kind of thing remembered; brightness encodes how
    strongly the organism holds it. Empty cells use the deep-space
    background. Returns (text, cells): the renderable plus the
    CELLS_COLS*CELLS_ROWS grid with each occupied cell's full metadata,
    so a click on the grid can be resolved to what it holds."""
    capacity = CELLS_COLS * CELLS_ROWS
    items = []
    for (obj, attr, val), conf in org.store.beliefs().items():
        kind = "self" if obj == "self" else "belief"
        items.append((kind, conf, f"{obj}:{attr}={val}",
                      {"object": obj, "attribute": attr, "value": val}))
    for text, depth in org.store.rules:
        items.append(("rule", 0.5 + min(depth, 4) / 8, text,
                      {"text": text, "depth": depth}))
    for entry in org.store.memory[-50:]:
        mkind = entry.get("kind", "memory")
        items.append(("memory", 0.7, f"{mkind}:{entry.get('text', '')}",
                      {"cycle": entry.get("cycle"), "tag": mkind,
                       "text": entry.get("text", "")}))
    goal = org.store.active_goal()
    if goal:
        items.append(("goal", 0.9, goal["text"],
                      {"text": goal["text"],
                       "created_cycle": goal.get("created_cycle"),
                       "strategy": goal.get("strategy")}))

    # Most strongly held memories get a cell when space runs out.
    items.sort(key=lambda item: -item[1])
    items = items[:capacity]

    grid = [None] * capacity
    for kind, conf, key, meta in items:
        start = _stable_index(key, capacity)
        for probe in range(capacity):
            pos = (start + probe) % capacity
            if grid[pos] is None:
                grid[pos] = {"kind": kind, "confidence": conf, **meta}
                break

    text = Text()
    text.append(f"neural memory · {len(items)} cells\n",
                style="bold #e2e8f0")
    for row in range(CELLS_ROWS):
        for col in range(CELLS_COLS):
            cell = grid[row * CELLS_COLS + col]
            if cell is None:
                text.append("  ", style=f"on {_CELLS_BG}")
            else:
                text.append("  ", style=f"on {_cell_color(
                    cell['kind'], cell['confidence'])}")
        text.append("\n")
    # legend: real swatches in the exact colors the grid uses — each kind
    # shows its weak->strong endpoints, because brightness is confidence
    legend = Text()
    legend.append("legend: ", style="#94a3b8")
    for kind, label in (("belief", "beliefs"), ("self", "self"),
                        ("rule", "rules"), ("memory", "memory"),
                        ("goal", "goals")):
        legend.append("  ", style=f"on {_cell_color(kind, 0.15)}")
        legend.append("  ", style=f"on {_cell_color(kind, 1.0)}")
        legend.append(f" {label} · ", style="#94a3b8")
    legend.append("dim→bright = weak→strong · click a cell to inspect it",
                  style="#94a3b8")
    text.append(legend)
    return text, grid


def cells_view(org):
    """Just the renderable half of cells_layout (grid metadata unused)."""
    return cells_layout(org)[0]


def cell_detail_text(cell):
    """Human-readable description of one occupied cell: what kind of
    object it is plus every metadata field it carries."""
    kind = cell["kind"]
    lines = [f"kind: {kind}  (confidence {cell['confidence']:.2f})", ""]
    if kind in ("belief", "self"):
        lines += [f"object:    {cell['object']}",
                  f"attribute: {cell['attribute']}",
                  f"value:     {cell['value']}"]
    elif kind == "rule":
        lines += [f"depth: {cell['depth']}", "", cell["text"]]
    elif kind == "memory":
        lines += [f"cycle: {cell['cycle']}", f"tag:   {cell['tag']}",
                  "", cell["text"]]
    elif kind == "goal":
        lines += [f"created cycle: {cell['created_cycle']}",
                  f"strategy:      {cell['strategy'] or '-'}",
                  "", cell["text"]]
    return "\n".join(lines)
