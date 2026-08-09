"""Pure string builders for the organism TUI's inspection tabs (Mind,
Memory). No textual imports — unit testable without a terminal."""

from learning import describe

_BELIEF_LIMIT = 12
_RULE_LIMIT = 10


def conf_bar(conf, width=5):
    """A tiny confidence meter: ▮▮▮▮▯."""
    filled = max(0, min(width, round(conf * width)))
    return "▮" * filled + "▯" * (width - filled)


def mind_view(org):
    """The Mind tab: top beliefs with confidence bars, committed rules,
    attention focus, genome stats. Read-only; rebuilt on every tick."""
    m = org.metrics()
    lines = ["top beliefs", ""]
    top = sorted(org.store.beliefs().items(), key=lambda kv: -kv[1])
    for (obj, attr, val), conf in top[:_BELIEF_LIMIT]:
        lines.append(f"{conf_bar(conf)} {conf:.2f} {obj}:{attr}={val}")
    if not top:
        lines.append("(no beliefs yet)")
    if org.store.goals:
        lines += ["", "goals", ""]
        active = org.store.active_goal()
        if active:
            lines.append(
                f"→ {active['text']} (since cycle {active['created_cycle']})")
        for g in [g for g in org.store.goals
                  if g["done_cycle"] is not None][-3:]:
            lines.append(f"done (cycle {g['done_cycle']}): {g['text']}")
    if org.store.rules:
        lines += ["", "committed rules", ""]
        lines += [text for text, _depth in org.store.rules[:_RULE_LIMIT]]
    if org.store.attention:
        pairs = sorted(f"{a}={v}" for a, v in org.store.attention)
        lines += ["", "attention: " + ", ".join(pairs)]
    lines += ["",
              (f"genome: {m.belief_count} beliefs · {m.rule_count} rules · "
               f"depth {m.total_depth} · score {m.score():.1f}")]
    return "\n".join(lines)


def memory_view(org):
    """The Memory tab: every episode (cycle-stamped), what the organism
    knows about the user, and what the user said the organism is."""
    lines = ["episodes", ""]
    for ep in org.store.memory:
        lines.append(f"cycle {ep['cycle']:<4} {ep['kind']:<8} {ep['text']}")
    if not org.store.memory:
        lines.append("(nothing remembered yet)")
    beliefs = org.store.beliefs()
    user_facts = [describe(b) for b in beliefs if b[0] == "user"]
    if user_facts:
        lines += ["", "what it knows about you", ""]
        lines += [f"- {f}" for f in user_facts]
    views = [v for (o, a, v) in beliefs if (o, a) == ("self", "described_as")]
    if views:
        lines += ["", "what you said it is", ""]
        lines += [f"- {v}" for v in views]
    artifacts = org.store.dir_path / "artifacts"
    if artifacts.is_dir():
        files = sorted(p for p in artifacts.iterdir() if p.is_file())
        if files:
            lines += ["", "artifacts", ""]
            lines += [f"- {p.name} ({p.stat().st_size} bytes)"
                      for p in files]
    return "\n".join(lines)
