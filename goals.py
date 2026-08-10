"""Goal progress and strategy helpers for the organism."""

import re

import learning

LEARN_GOAL_PREFIXES = ("learn", "know", "understand")

# cycles without progress before a goal is considered stalled
STALLED_CYCLES = 10


def formulate_subgoals(goal_text):
    """Return a short strategy string for a goal."""
    return (
        f"strategy: break '{goal_text}' into small questions "
        "and ask one at a time."
    )


def _target_count(text):
    nums = [int(n) for n in re.findall(r"\d+", text)]
    return nums[-1] if nums else 5


def _relevant_facts(store, text):
    """Count user facts whose description overlaps words with the goal text."""
    words = set(re.findall(r"[a-z]{3,}", text.lower()))
    count = 0
    for belief in store.beliefs():
        obj, _attr, _val = belief
        if obj != "user":
            continue
        fact = learning.describe(belief).lower()
        if words & set(re.findall(r"[a-z]{3,}", fact)):
            count += 1
    return count


def _is_learn_goal(text):
    return any(text.lower().startswith(p) for p in LEARN_GOAL_PREFIXES)


def update_progress(goal, cycle, current):
    """Stamp progress on a goal so stalled detection can compare."""
    if current != goal.get("last_progress_current", -1):
        goal["last_progress_current"] = current
        goal["last_progress_cycle"] = cycle


def is_stalled(goal, cycle, current):
    """True when progress has not moved for STALLED_CYCLES."""
    last = goal.get("last_progress_current")
    if last is None:
        return False
    if current != last:
        return False
    elapsed = cycle - goal.get("last_progress_cycle", goal.get("created_cycle", cycle))
    return elapsed >= STALLED_CYCLES


def goal_progress(store):
    """Return a human-readable progress line for the active goal."""
    goal = store.active_goal()
    if not goal:
        return None
    text = goal.get("text", "")
    start = goal.get("created_cycle", store.cycle)
    elapsed = max(store.cycle - start, 0)
    target = _target_count(text)
    current = _relevant_facts(store, text)
    stalled = " (stalled)" if is_stalled(goal, store.cycle, current) else ""
    return (
        f"goal: {text}  (started cycle {start}, {elapsed} cycles ago, "
        f"progress {current}/{target}){stalled}"
    )
