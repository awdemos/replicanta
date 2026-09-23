"""View builders for the organism TUI's inspection dashboards (the F3
being overlay).

Mind and Memory are plain strings; Inner also has a rich-renderable
version with gauges and panels. No textual imports — unit testable
without a terminal."""

import hashlib
import json
import re
import contextlib

from rich import box
from rich.bar import Bar
from rich.color import Color
from rich.console import Group
from rich.panel import Panel
from rich.style import Style
from rich.table import Table
from rich.text import Text

from replicanta import activity
from replicanta.learning import describe

STYLE_USER = "cyan"
STYLE_ORG = "green"
STYLE_DIM = "dim"
STYLE_DREAM = "magenta"
STYLE_LEARNED = "yellow"
STYLE_SELF = "italic yellow"
STYLE_WARN = "red"

_BELIEF_LIMIT = 12
_RULE_LIMIT = 10


def conf_bar(conf, width=5):
    """A tiny confidence meter: ▮▮▮▮▯."""
    filled = max(0, min(width, round(conf * width)))
    return "▮" * filled + "▯" * (width - filled)


def empty_mind():
    """Empty-state renderable for the Mind section."""
    return Panel(
        Text(
            "No beliefs yet. Tell the organism something about yourself.",
            style="dim",
        ),
        title="mind",
        border_style="cyan",
    )


def empty_memory():
    """Empty-state renderable for the Memory section."""
    return Panel(
        Text("No memories yet. Memories form as you talk.", style="dim"),
        title="memory",
        border_style="magenta",
    )


def empty_inner():
    """Empty-state renderable for the Inner section."""
    return Panel(
        Text(
            "Mental-state gauges appear here: mood, stress, grounding, chaos, and recent thought metabolism.",
            style="dim",
        ),
        title="inner",
        border_style="cyan",
    )


def chat_card(who, text, timestamp=None, border_style=None):
    """A consistent panel card for chat utterances. The border uses the
    heavy solid box — light '│' rules read as raw pipe characters and
    break the card metaphor when entity text wraps."""
    border_style = border_style or (STYLE_USER if who == "you" else STYLE_ORG)
    title = f"{who} · {timestamp}" if timestamp else who
    return Panel(
        Text(text),
        title=title,
        title_align="left",
        border_style=border_style,
        padding=(0, 1),
        box=box.HEAVY,
    )


# -----------------------------------------------------------------------------
# DOOM half-block renderer
# -----------------------------------------------------------------------------

_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")
_BLACK = (0, 0, 0)
_WHITE = (255, 255, 255)


def _rgb(triplet):
    return Color.from_rgb(triplet[0], triplet[1], triplet[2])


def doom_halfblock_text(frame):
    """Convert a doom-ascii block frame into a half-block truecolor Text.

    The engine's -chars block -nograd wire format is a stream of SGR
    color changes and two identical characters per pixel. Discarding the
    characters and keeping only the per-pixel colors lets each terminal
    cell carry TWO vertical pixels (▀ = fg on top / bg on bottom), so the
    same 80x25 footprint shows an 80x50 image — double the resolution of
    painting one pixel per cell, at correct aspect ratio. Returns None
    when the frame does not parse (caller falls back to Text.from_ansi).
    """
    pixels = []  # rows of (r, g, b)
    row = []
    color = _WHITE  # the engine starts each frame at 0x00FFFFFF
    pair = []  # two characters make one pixel

    def consume(text):
        nonlocal row, pair
        for ch in text:
            if ch == "\n":
                pixels.append(row)
                row = []
                continue
            pair.append(ch)
            if len(pair) == 2:
                row.append(color)
                pair = []

    pos = 0
    for m in _SGR_RE.finditer(frame):
        consume(frame[pos : m.start()])
        params = m.group(1)
        if params.startswith("38;2;"):
            parts = params.split(";")
            with contextlib.suppress(IndexError, ValueError):
                color = (int(parts[2]), int(parts[3]), int(parts[4]))
        elif params in ("", "0"):
            color = _WHITE
        # "1" (bold) and other SGRs carry no color information
        pos = m.end()
    consume(frame[pos:])
    if row:
        pixels.append(row)
    if not pixels or not pixels[0]:
        return None
    width = max(len(r) for r in pixels)
    if width == 0:
        return None
    for r in pixels:
        if len(r) < width:
            r.extend([_BLACK] * (width - len(r)))
    if len(pixels) % 2:
        pixels.append([_BLACK] * width)

    text = Text()
    for y in range(0, len(pixels), 2):
        top_row, bot_row = pixels[y], pixels[y + 1]
        for x in range(width):
            top, bot = top_row[x], bot_row[x]
            if top == bot:
                if top == _BLACK:
                    text.append(" ")
                else:
                    text.append("█", style=Style(color=_rgb(top)))
            elif bot == _BLACK:
                text.append("▀", style=Style(color=_rgb(top), bgcolor=_rgb(_BLACK)))
            elif top == _BLACK:
                text.append("▄", style=Style(color=_rgb(bot), bgcolor=_rgb(_BLACK)))
            else:
                text.append("▀", style=Style(color=_rgb(top), bgcolor=_rgb(bot)))
        if y + 2 < len(pixels):
            text.append("\n")
    return text


def _human_size(n):
    """Bytes -> compact human form (B, KB, MB)."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def mind_view(org):
    """The Mind section: a human-readable snapshot of what the organism is
    currently holding as true, aiming for, and paying attention to.
    Read-only; rebuilt on every tick."""
    m = org.metrics()
    lines = [
        "top beliefs",
        "",
        ("(▮ = confidence; more blocks means the organism holds it more strongly)"),
        "",
    ]
    top = sorted(org.store.beliefs().items(), key=lambda kv: -kv[1])
    for (obj, attr, val), conf in top[:_BELIEF_LIMIT]:
        lines.append(f"{conf_bar(conf)} {conf:.2f} {obj}:{attr}={val}")
    if not top:
        lines += [
            ('(no beliefs yet — say something like "my name is Sam" or "i like rain")'),
            "",
        ]
    if org.store.goals:
        lines += ["", "goals", ""]
        active = org.store.active_goal()
        if active:
            strategy = active.get("strategy")
            strategy_line = f"   strategy: {strategy}" if strategy else ""
            lines.append(f"→ now trying: {active['text']} (since cycle {active['created_cycle']}){strategy_line}")
        for g in [g for g in org.store.goals if g["done_cycle"] is not None][-3:]:
            lines.append(f"   done (cycle {g['done_cycle']}): {g['text']}")
    skill_store = getattr(org, "skills", None)
    skill_list = skill_store.list() if skill_store is not None else []
    if skill_list:
        lines += [
            "",
            "skills",
            "",
            "(techniques the organism has learned and can reuse)",
            "",
        ]
        for s in skill_list[:8]:
            lines.append(f"{s.name} (used {s.uses}×) — when {s.when}")
    if org.store.rules:
        lines += [
            "",
            "committed rules",
            "",
            "(derived patterns the organism treats as reliable)",
            "",
        ]
        lines += [text for text, _depth in org.store.rules[:_RULE_LIMIT]]
    if org.store.attention:
        pairs = sorted(f"{a}={v}" for a, v in org.store.attention)
        lines += [
            "",
            "attention: " + ", ".join(pairs),
            ("(only beliefs matching the focus window strongly influence replies right now)"),
        ]
    lines += [
        "",
        (
            f"genome: {m.belief_count} beliefs · {m.rule_count} rules · "
            f"depth {m.total_depth} · consciousness score "
            f"{m.score():.1f}"
        ),
    ]
    activity_lines = activity.summary_lines(org.store)
    if activity_lines:
        lines += [""] + activity_lines
    return "\n".join(lines)


def memory_view(org):
    """The Memory section: the organism's episodic diary, what it has learned
    about the user, how the user describes it, and any saved artifacts.
    Read-only; rebuilt on every tick."""
    lines = [
        "episodes",
        "",
        ("(notable moments from the organism's life, stamped by the cycle they happened)"),
        "",
    ]
    for ep in org.store.memory:
        lines.append(f"cycle {ep['cycle']:<4} {ep['kind']:<8} {ep['text']}")
    if not org.store.memory:
        lines += [
            ("(nothing remembered yet — events appear here when the organism dreams, learns, or fades)"),
            "",
        ]
    beliefs = org.store.beliefs()
    user_facts = [describe(b) for b in beliefs if b[0] == "user"]
    if user_facts:
        lines += [
            "",
            "what it knows about you",
            "",
            "(facts extracted from what you said)",
            "",
        ]
        lines += [f"- {f}" for f in user_facts]
    views = [v for (o, a, v) in beliefs if (o, a) == ("self", "described_as")]
    if views:
        lines += [
            "",
            "what you said it is",
            "",
            "(labels you have given the organism)",
            "",
        ]
        lines += [f"- {v}" for v in views]
    artifacts = org.store.dir_path / "artifacts"
    if artifacts.is_dir():
        files = sorted(p for p in artifacts.iterdir() if p.is_file())
        if files:
            lines += ["", "artifacts", "", "(files the organism has written)", ""]
            lines += [f"- {p.name} ({_human_size(p.stat().st_size)})" for p in files]
    return "\n".join(lines)


def _mental_state(org):
    """Current mental-state scalars: (label, value) pairs, mood last.
    Values are the raw store attributes when present (they are floats in
    [0, 1]); mood is a string belief, not a scalar."""
    store = org.store
    scalars = []
    for attr, label in (
        ("arousal", "arousal"),
        ("stress", "stress"),
        ("coherence", "coherence"),
        ("incoherence", "incoherence"),
    ):
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
    if not isinstance(reg, dict):
        return None
    entry = reg.get("pending")
    if not entry:
        return None
    kind = entry.get("kind", "?")
    if kind == "pattern":
        preview = f"{entry.get('template', '?')} ← /{entry.get('regex', '?')}/"
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


def _gauge_color(label, value):
    """Bar color per gauge; stress is banded green/amber/red."""
    if label == "stress":
        if value > 0.7:
            return "red"
        if value > 0.4:
            return "yellow"
        return "green"
    return {
        "arousal": "magenta",
        "coherence": "green",
        "incoherence": "yellow",
    }.get(label, "blue")


def _mental_state_panel(org):
    """MENTAL STATE: one ruled table row per scalar — bar gauge plus a
    right-aligned two-decimal value; mood rides along as a text row."""
    state = _mental_state(org)
    if not state:
        return None
    table = Table(box=box.MINIMAL, padding=(0, 2), header_style="bold")
    table.add_column("", style="bold")
    table.add_column("Gauge")
    table.add_column("Value", justify="right", style="dim")
    for label, value in state:
        if label == "mood":
            table.add_row("mood", "", str(value))
        else:
            table.add_row(
                label,
                Bar(size=1.0, begin=0.0, end=value, width=24, color=_gauge_color(label, value)),
                f"{value:.2f}",
            )
    caption = Text(
        "scalars in [0,1] · stress bands: green < 0.40 · amber < 0.70 · red above",
        style="dim",
    )
    return Panel(Group(table, Text(""), caption), title="mental state", border_style="cyan")


def _mind_metrics_panel(org):
    """MIND METRICS: structural counters and scores as a Metric/Value table."""
    m = org.metrics()
    store = org.store
    goals_active = 1 if store.active_goal() else 0
    goals_done = sum(1 for g in store.goals if g["done_cycle"] is not None)
    depth = m.total_depth / m.rule_count if m.rule_count else 0.0
    rows = (
        ("beliefs", str(m.belief_count)),
        ("rules", str(m.rule_count)),
        ("mean rule depth", f"{depth:.2f}"),
        ("consciousness score", f"{m.score():.1f}"),
        ("goals", f"{goals_active} active · {goals_done} done"),
        ("memories", str(len(store.memory))),
        ("cycle", str(store.cycle)),
    )
    table = Table(box=box.MINIMAL, padding=(0, 2), header_style="bold")
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right", style="dim")
    for name, value in rows:
        table.add_row(name, value)
    return Panel(table, title="mind metrics", border_style="blue")


_ACTIVITY_GROUPS = (
    (
        "symbolic",
        (
            ("rules tried", "rules_tried"),
            ("derivations", "derivations"),
            ("beliefs new", "beliefs_new"),
            ("beliefs strengthened", "beliefs_strengthened"),
            ("rules committed", "rules_committed"),
            ("dreams promoted", "dreams_promoted"),
            ("dreams discarded", "dreams_discarded"),
        ),
    ),
    (
        "neural",
        (
            ("llm calls", "llm_calls"),
            ("prompt tokens", "prompt_tokens"),
            ("gen tokens", "gen_tokens"),
            ("utterances", "utterances"),
            ("fallbacks", "fallbacks"),
        ),
    ),
    (
        "coupling",
        (
            ("facts learned", "facts_learned"),
            ("grounded utterances", "grounded_utterances"),
        ),
    ),
)


def _activity_panel(store):
    """ACTIVITY: exact counters as Total + per-cycle rates, grouped into
    the symbolic / neural / coupling families. None when nothing has run."""
    a = store.activity
    if not a:
        return None
    cycle = max(store.cycle, 1)
    table = Table(box=box.MINIMAL, padding=(0, 2), header_style="bold")
    table.add_column("Counter", style="bold")
    table.add_column("Total", justify="right")
    table.add_column("/cycle", justify="right", style="dim")
    for group, counters in _ACTIVITY_GROUPS:
        table.add_row(Text(group, style="bold dim"), "", "")
        for label, key in counters:
            total = a.get(key, 0)
            table.add_row(f"  {label}", str(total), f"{total / cycle:.2f}")
    caption = Text("exact event counters · rates per lifecycle cycle", style="dim")
    return Panel(Group(table, Text(""), caption), title="activity", border_style="yellow")


_HOST_SENSE_KEYS = (
    (("cpu", "load"), "cpu load"),
    (("mem", "usage"), "mem usage"),
    (("disk", "space"), "disk space"),
    (("temp", "cpu"), "temp cpu"),
)


def _host_sense_strip(store):
    """One-line dim strip of the latest host readings, or None when the
    organism has not sensed the host (or nothing is held above threshold)."""
    beliefs = store.beliefs()
    senses = []
    for (obj, attr), label in _HOST_SENSE_KEYS:
        best = None
        for (o, a, v), conf in beliefs.items():
            if o == obj and a == attr and conf >= 0.5 and (best is None or conf > best[1]):
                best = (v, conf)
        if best is not None:
            senses.append(f"{label}={best[0]}")
    if not senses:
        return None
    return Text.assemble(("host sense  ", "bold dim"), (" · ".join(senses), "dim"))


def mind_renderable(org):
    """The Mind section as rich renderables: top beliefs with confidence bars,
    goals, skills, rules, attention, and genome/activity footer. Rebuilt
    every tick."""
    panels = []
    store = org.store

    top = sorted(org.store.beliefs().items(), key=lambda kv: -kv[1])
    if top:
        grid = Table(box=box.MINIMAL, padding=(0, 1), header_style="bold")
        grid.add_column("Conf", justify="right", style="dim")
        grid.add_column("Bar", justify="left")
        grid.add_column("Belief", justify="left")
        grid.add_column("", justify="right", style="dim")
        for (obj, attr, val), conf in top[:_BELIEF_LIMIT]:
            style = _belief_style(obj)
            grid.add_row(
                f"{conf:.2f}",
                Text(conf_bar(conf), style=style),
                Text(f"{obj}:{attr}={val}", style=style),
                f"({obj})",
            )
        caption = Text(
            "▮ = confidence; more blocks means the organism holds it more strongly",
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
        caption = Text("techniques the organism has learned and can reuse", style="dim")
        panels.append(Panel(Group(grid, Text(""), caption), title="skills", border_style="yellow"))

    if store.rules:
        grid = Table.grid(padding=(0, 1))
        grid.add_column(justify="left")
        for text, _depth in store.rules[:_RULE_LIMIT]:
            grid.add_row(Text(text, style="blue"))
        caption = Text("derived patterns the organism treats as reliable", style="dim")
        panels.append(
            Panel(
                Group(grid, Text(""), caption),
                title="committed rules",
                border_style="blue",
            )
        )

    if store.attention:
        pairs = sorted(f"{a}={v}" for a, v in store.attention)
        body = Group(
            Text(", ".join(pairs)),
            Text(
                "only beliefs matching the focus window strongly influence replies right now",
                style="dim",
            ),
        )
        panels.append(Panel(body, title="attention", border_style="yellow"))

    activity_lines = activity.summary_lines(store)
    if not panels and not activity_lines:
        return empty_mind()

    m = org.metrics()
    genome_text = (
        f"{m.belief_count} beliefs · {m.rule_count} rules · depth {m.total_depth} · consciousness score {m.score():.1f}"
    )
    footer = Text.assemble(
        ("genome: ", "bold"),
        (genome_text, ""),
    )
    if activity_lines:
        footer = Group(footer, Text(""), Text("\n".join(activity_lines)))
    panels.append(Panel(footer, title="activity", border_style="dim"))

    return Group(*panels)


def memory_renderable(org):
    """The Memory section as rich renderables: episodes, user facts,
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
    files = []
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

    if not panels:
        return empty_memory()

    return Group(*panels)


def inner_renderable(org):
    """The Inner section as a live dashboard: mental-state gauges, mind
    metrics, the perpetuation loop with progress bars, activity counters
    as totals + per-cycle rates, a host-sense strip, and any pending
    proposal. Rebuilt every tick."""
    store = org.store
    panels = []

    state_panel = _mental_state_panel(org)
    if state_panel is not None:
        panels.append(state_panel)

    panels.append(_mind_metrics_panel(org))

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
            f"{tried} questions → {derived} derivations ({derived / max(tried, 1):.0%} yield) over {cycle} cycles",
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
            f"{committed} rules committed · {promoted} dreams promoted / {discarded} discarded",
        )
        panels.append(Panel(grid, title="perpetuation loop", border_style="magenta"))

    activity_panel = _activity_panel(store)
    if activity_panel is not None:
        panels.append(activity_panel)

    sense = _host_sense_strip(store)
    if sense is not None:
        panels.append(sense)

    proposal = _pending_proposal(org)
    if proposal:
        auto = getattr(getattr(org, "store", None), "auto_apply_patches", False)
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
        return empty_inner()
    return Group(*panels)


def inner_view(org):
    """The Inner section: the organism's internal activity — mental-state
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
            f"{tried} questions → {derived} derivations ({derived / max(tried, 1):.0%} yield) over {cycle} cycles"
        )
        lines.append(
            f"{committed} rules committed ({committed / max(derived, 1):.0%}"
            f" of derivations) · {promoted} dreams promoted / "
            f"{discarded} discarded"
        )
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
        auto = getattr(getattr(org, "store", None), "auto_apply_patches", False)
        lines.append("auto-applied" if auto else "(/approve to apply · /reject to discard)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Neural memory grid (cells section)
# ---------------------------------------------------------------------------

CELLS_COLS = 48
CELLS_ROWS = 20
_CELLS_BG = "#0b0f1a"


def _hex_to_rgb(h):
    return tuple(int(h[i : i + 2], 16) for i in (1, 3, 5))


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
    digest = hashlib.md5(key.encode(), usedforsecurity=False).digest()
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
        items.append(
            (
                kind,
                conf,
                f"{obj}:{attr}={val}",
                {"object": obj, "attribute": attr, "value": val},
            )
        )
    for text, depth in org.store.rules:
        items.append(("rule", 0.5 + min(depth, 4) / 8, text, {"text": text, "depth": depth}))
    for entry in org.store.memory[-50:]:
        mkind = entry.get("kind", "memory")
        items.append(
            (
                "memory",
                0.7,
                f"{mkind}:{entry.get('text', '')}",
                {
                    "cycle": entry.get("cycle"),
                    "tag": mkind,
                    "text": entry.get("text", ""),
                },
            )
        )
    goal = org.store.active_goal()
    if goal:
        items.append(
            (
                "goal",
                0.9,
                goal["text"],
                {
                    "text": goal["text"],
                    "created_cycle": goal.get("created_cycle"),
                    "strategy": goal.get("strategy"),
                },
            )
        )

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
    text.append(f"neural memory · {len(items)} cells\n", style="bold #e2e8f0")
    for row in range(CELLS_ROWS):
        for col in range(CELLS_COLS):
            cell = grid[row * CELLS_COLS + col]
            if cell is None:
                text.append("  ", style=f"on {_CELLS_BG}")
            else:
                text.append("  ", style=f"on {_cell_color(cell['kind'], cell['confidence'])}")
        text.append("\n")
    # legend: real swatches in the exact colors the grid uses — each kind
    # shows its weak->strong endpoints, because brightness is confidence
    legend = Text()
    legend.append("legend: ", style="#94a3b8")
    for kind, label in (
        ("belief", "beliefs"),
        ("self", "self"),
        ("rule", "rules"),
        ("memory", "memory"),
        ("goal", "goals"),
    ):
        legend.append("  ", style=f"on {_cell_color(kind, 0.15)}")
        legend.append("  ", style=f"on {_cell_color(kind, 1.0)}")
        legend.append(f" {label} · ", style="#94a3b8")
    legend.append("dim→bright = weak→strong · click a cell to inspect it", style="#94a3b8")
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
        lines += [
            f"object:    {cell['object']}",
            f"attribute: {cell['attribute']}",
            f"value:     {cell['value']}",
        ]
    elif kind == "rule":
        lines += [f"depth: {cell['depth']}", "", cell["text"]]
    elif kind == "memory":
        lines += [f"cycle: {cell['cycle']}", f"tag:   {cell['tag']}", "", cell["text"]]
    elif kind == "goal":
        lines += [
            f"created cycle: {cell['created_cycle']}",
            f"strategy:      {cell['strategy'] or '-'}",
            "",
            cell["text"],
        ]
    return "\n".join(lines)
