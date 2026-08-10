# Organism Evolution, Goal-Seeking, and Self-Awareness Improvements

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or manual step-by-step execution. This plan touches multiple files; implement in the phases below, running tests and committing after each phase.

**Goal:** Feed the organism's own learning trajectory into its prompts so it can evolve, pursue goals, and become more self-aware.

**Scope:** Add activity digest and goal progress to the prompt; track surprise/contradiction events; add reflection triggers; support subgoals and stalled-goal reformulation; add self-model beliefs and attention rationale; track skill effectiveness.

**Tech Stack:** Python 3.14, Textual, Scallop via lupa, Ollama.

---

## File map

- `activity.py` — add `digest()` and per-window counters.
- `goals.py` — new module: goal progress, subgoal/strategy helpers.
- `learning.py` — contradiction/surprise event emission.
- `organism.py` — integrate goals, reflection triggers, self-model, attention rationale.
- `extensions.py` — skill effectiveness tracking.
- `narration.py` — include digest/progress/subgoals/self-model in snapshot and prompt.
- `skills.py` — skill use/outcome recording.
- `tests/test_evolution.py` — new regression tests.

---

## Phase 1: Activity digest in the prompt

**Files:** `activity.py`, `narration.py`

- [ ] **Step 1: Add `activity_digest(store, cycles=30)` in `activity.py`**

  Compute deltas over the last N cycles from `store.activity` counters and return a short narrative string, e.g.:
  ```python
  def digest(store, cycles=30):
      a = store.activity
      if not a:
          return "no activity yet"
      elapsed = max(store.cycle - a.get("snapshot_cycle", store.cycle), 1)
      if elapsed > cycles:
          elapsed = cycles
      tried = a.get("rules_tried", 0)
      derived = a.get("derivations", 0)
      committed = a.get("rules_committed", 0)
      promoted = a.get("dreams_promoted", 0)
      discarded = a.get("dreams_discarded", 0)
      beliefs_new = a.get("beliefs_new", 0)
      llm_calls = a.get("llm_calls", 0)
      fallbacks = a.get("fallbacks", 0)
      rate = derived / max(tried, 1)
      lines = [
          f"over roughly the last {elapsed} cycles:",
          f"- asked {tried} self-questions, produced {derived} derivations "
          f"({rate:.0%} yield)",
          f"- committed {committed} rules, promoted {promoted} dreams, "
          f"discarded {discarded} dreams",
          f"- formed {beliefs_new} new beliefs, used the inner voice "
          f"{llm_calls} times ({fallbacks} fallbacks)",
      ]
      if promoted + discarded > 0:
          dream_rate = promoted / max(promoted + discarded, 1)
          lines.append(f"- dream promotion rate: {dream_rate:.0%}")
      return "\n".join(lines)
  ```

- [ ] **Step 2: Include digest in `state_snapshot()`**

  In `narration.py`, add:
  ```python
  import activity
  ...
  "activity_digest": activity.digest(org.store),
  ```

- [ ] **Step 3: Render digest in `build_prompt()`**

  After the background numbers block, add:
  ```python
  if snapshot.get("activity_digest"):
      lines += ["", "your recent learning activity:", snapshot["activity_digest"]]
  ```

- [ ] **Step 4: Add tests**

  In `tests/test_evolution.py`:
  ```python
  def test_activity_digest_appears_in_prompt():
      org = make_org_with_activity()
      snap = state_snapshot(org)
      assert "derivations" in snap["activity_digest"].lower()
      prompt = build_prompt(snap)
      assert "learning activity" in prompt
  ```

- [ ] **Step 5: Run tests and commit**

  Run: `.venv/bin/python -m pytest tests/test_narration.py tests/test_evolution.py -q`
  Expected: PASS.

  ```bash
  git add activity.py narration.py tests/test_evolution.py
  git commit -m "feat(evolution): include activity digest in voice prompts"
  ```

---

## Phase 2: Goal progress and subgoals

**Files:** `goals.py` (new), `organism.py`, `narration.py`

- [ ] **Step 1: Create `goals.py` module**

  ```python
  """Goal progress and strategy helpers for the organism."""

  LEARN_GOAL_PREFIXES = ("learn", "know", "understand")

  def goal_progress(store):
      """Return a human-readable progress line for the active goal."""
      goal = store.active_goal()
      if not goal:
          return None
      text = goal.get("text", "")
      start = goal.get("created_cycle", store.cycle)
      elapsed = store.cycle - start
      # count relevant user facts
      target = _target_count(text)
      current = _count_relevant_facts(store, text)
      return (
          f"goal: {text}  (started cycle {start}, "
          f"{elapsed} cycles ago, progress {current}/{target})"
      )

  def _target_count(text):
      nums = [int(n) for n in __import__('re').findall(r'\d+', text)]
      return nums[-1] if nums else 5

  def _count_relevant_facts(store, text):
      """Crude relevance: count user facts whose description overlaps words
      with the goal text."""
      import re
      words = set(re.findall(r"[a-z]{3,}", text.lower()))
      from learning import describe
      count = 0
      for (obj, attr, val), _conf in store.beliefs().items():
          if obj != "user":
              continue
          fact = describe((obj, attr, val)).lower()
          if words & set(re.findall(r"[a-z]{3,}", fact)):
              count += 1
      return count

  def formulate_subgoals(goal_text):
      """Return a short strategy string for a goal."""
      return f"strategy: break '{goal_text}' into small questions and ask one at a time."
  ```

- [ ] **Step 2: Add goal progress to `state_snapshot()`**

  ```python
  from goals import goal_progress
  ...
  "goal_progress": goal_progress(org.store),
  ```

- [ ] **Step 3: Render goal progress in `build_prompt()`**

  Replace the existing goal line with:
  ```python
  if snapshot.get("goal_progress"):
      lines.append(snapshot["goal_progress"])
  elif snapshot.get("goal"):
      lines.append(f"what you are trying to do: {snapshot['goal']}")
  ```

- [ ] **Step 4: Add subgoal/strategy to goal formation**

  In `organism.py`, `add_goal()` should also store a `strategy` field:
  ```python
  from goals import formulate_subgoals
  ...
  def add_goal(self, text):
      self.store.goals.append({
          "text": text,
          "created_cycle": self.store.cycle,
          "done_cycle": None,
          "marker": "...",
          "strategy": formulate_subgoals(text),
      })
  ```

  In `narration.py`, include `goal_strategy` in the snapshot and render it in the prompt.

- [ ] **Step 5: Tests**

  ```python
  def test_goal_progress_in_prompt():
      org = make_org_with_goal("learn three things about the user")
      snap = state_snapshot(org)
      assert "progress" in snap.get("goal_progress", "")
      prompt = build_prompt(snap)
      assert "goal:" in prompt
  ```

- [ ] **Step 6: Commit**

  ```bash
  git add goals.py organism.py narration.py tests/test_evolution.py
  git commit -m "feat(goals): progress tracking and strategy in prompts"
  ```

---

## Phase 3: Reflection triggers and surprise tracking

**Files:** `learning.py`, `organism.py`, `narration.py`

- [ ] **Step 1: Emit surprise events on contradiction**

  In `learning.py`, when a belief is archived due to contradiction, add to `store.activity["surprises"]`: a list of `{cycle, old, new}` dicts. Keep only the last 10.

- [ ] **Step 2: Add reflection trigger conditions in organism.py**

  Add a method `_reflection_triggers()` that returns events when:
  - Just recovered from insanity (`store.insane` flipped false)
  - 3+ dreams discarded in a row
  - 5+ new beliefs in one tick
  - A surprise was recorded this tick
  - Faded (state became dead)

  In `_render_event`, emit `want_reflect` for these conditions.

- [ ] **Step 3: Include recent surprises in snapshot**

  ```python
  "surprises": store.activity.get("surprises", [])[-3:],
  ```

  Render in prompt:
  ```python
  if snapshot.get("surprises"):
      lines += ["", "recent surprises (things you thought were true but were not):"]
      lines.extend(f"- cycle {s['cycle']}: {s['old']} -> {s['new']}" for s in snapshot["surprises"])
  ```

- [ ] **Step 4: Tests and commit**

  ```bash
  git add learning.py organism.py narration.py tests/test_evolution.py
  git commit -m "feat(self-awareness): surprise tracking and reflection triggers"
  ```

---

## Phase 4: Self-model beliefs and attention rationale

**Files:** `organism.py`, `narration.py`

- [ ] **Step 1: Maintain self-model beliefs**

  In `organism.py`, after reflection or on goal completion, derive a self-model belief if the reflection mentions a pattern:
  ```python
  def _record_self_model(self, insight_text):
      """Store a durable belief about the organism's own behavior."""
      # simple extraction: first sentence that starts with "I tend to" etc.
      ...
  ```

  Simpler approach: after `reflect()` returns a skill, also record a belief `(self, insight, <short summary>)` with moderate confidence.

- [ ] **Step 2: Include self-model in snapshot**

  ```python
  "self_model": [
      describe(b) for b in beliefs if b[0] == "self" and b[1] in ("insight", "tends_to", "poor_at")
  ],
  ```

  Render in prompt under "what you know about yourself".

- [ ] **Step 3: Attention rationale**

  In `organism.py`, when the attention window changes, store a rationale:
  ```python
  self.store.attention_rationale = f"you are focused on ... because ..."
  ```

  Include in snapshot and prompt.

- [ ] **Step 4: Tests and commit**

  ```bash
  git add organism.py narration.py tests/test_evolution.py
  git commit -m "feat(self-awareness): self-model beliefs and attention rationale"
  ```

---

## Phase 5: Skill effectiveness tracking

**Files:** `skills.py`, `extensions.py`, `organism.py`, `narration.py`

- [ ] **Step 1: Record skill outcomes**

  In `skills.py`, extend `record_use()` to accept an `outcome` dict with `grounded`, `new_belief`, `user_replied`. Maintain a rolling average effectiveness per skill.

- [ ] **Step 2: Update callers**

  In `narration.py`, after a skill-influenced utterance is delivered, call `record_use()` with outcome inferred from the chat log.

- [ ] **Step 3: Include effectiveness in snapshot**

  ```python
  "skills": [
      f"{s.name} (effectiveness {s.effectiveness:.0%}, used {s.uses}x): {s.how}"
      for s in relevant_skills
  ],
  ```

- [ ] **Step 4: Deprecate low-effectiveness skills**

  In `SkillStore.flush()`, archive skills with effectiveness below 0.2 after 10+ uses.

- [ ] **Step 5: Tests and commit**

  ```bash
  git add skills.py extensions.py organism.py narration.py tests/test_evolution.py
  git commit -m "feat(evolution): skill effectiveness tracking and deprecation"
  ```

---

---

**Completed as of 2026-08-10.** Phases 1–5 have all been merged into the current codebase:
- `activity.record_digest()` in `activity.py` provides the activity digest (Phase 1).
- `goals.py` plus `goal_progress`/`goal_strategy` in `narration.py` (Phase 2).
- `store.activity["surprises"]` and related snapshot rendering (Phase 3).
- `self_model` beliefs and `attention_rationale` in snapshots (Phase 4).
- Skill effectiveness tracking and rendering in snapshots (Phase 5).
`tests/test_evolution.py` covers activity digest, goal progress/strategy, surprise rendering,
self-model/attention rationale, and skill effectiveness. All 656 tests pass; ruff clean.

## Phase 6: Full verification

- [ ] **Step 1: Run full test suite**

  Run: `.venv/bin/python -m pytest -q`
  Expected: all tests PASS.

- [ ] **Step 2: Run ruff**

  Run: `.venv/bin/ruff check --ignore I001,UP017 .`
  Expected: All checks passed!

- [ ] **Step 3: Restart live TUI**

  Kill the existing `orgtui-fix:1.1` process and restart to verify the organism can still run, form goals, and reflect.

- [ ] **Step 4: Push**

  ```bash
  git push origin main
  ```
