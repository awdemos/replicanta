# Design: Cognitive Threads, Typing Sensing, and Hybrid Memory Ranking

**Date:** 2026-08-17  
**Scope:** `src/replicanta` organism core, Glasshouse web UI, and TUI.

## 1. Goal

Give Replicanta organisms three new capabilities:

1. **Cognitive threads** — run multiple concurrent, named thought processes
   (self-questioning, reflection, planning) without blocking the main lifecycle.
2. **Typing sensing** — the web UI reports when the user is typing, which wakes
   the organism slightly and records activity.
3. **Hybrid memory ranking** — episodic memories are ranked by a combination of
   heuristic importance (at write time) and token-overlap relevance (at read
   time), so the voice recalls what matters most instead of only what happened
   most recently.

## 2. Cognitive Threads

### 2.1 Data model

```python
@dataclass
class CognitiveThread:
    id: str
    kind: str          # e.g. "self_question", "reflect", "plan", "sense"
    status: str        # "pending", "running", "done", "failed"
    created_cycle: int
    payload: dict
    result: Any = None
    error: str | None = None
```

Threads live on `BeliefStore`:

```python
self.threads: dict[str, CognitiveThread] = {}
self.thread_results: deque[dict] = deque(maxlen=20)
```

### 2.2 Thread pool

A `ThreadPoolExecutor` (default `max_workers=4`) is owned by the runtime layer:
`OrganismApp` (TUI) and `Glasshouse` (web). Both use the same helper
`ThreadPoolExecutor` and submit work through a small helper on `BeliefStore`:

```python
def queue_thread(self, thread: CognitiveThread) -> str:
    self.threads[thread.id] = thread
    self.dirty = True
    return thread.id

def finish_thread(self, thread_id: str, result=None, error=None):
    thread = self.threads.get(thread_id)
    if thread is None:
        return
    thread.status = "failed" if error else "done"
    thread.result = result
    thread.error = error
    self.thread_results.append({
        "id": thread.id,
        "kind": thread.kind,
        "cycle": thread.created_cycle,
        "result": result,
        "error": error,
    })
    self.dirty = True
```

### 2.3 Thread kinds

- `self_question` — pair of attention-window attributes → Scallop derivation.
- `reflect` — run the skill-reflection prompt in the background.
- `plan` — lightweight goal strategy generation.
- `sense` — host/git probe snapshot and belief ingestion.

The main `_wake()` loop currently asks 2–3 questions serially. With threads it
will still produce a small batch, but each question may run in parallel and the
lifecycle stays responsive.

### 2.4 Concurrency rules

- Scallop contexts are thread-affine. The worker must either create a fresh
  `ScallopContext` from the persisted `.scl` genome, or the main thread must
  rebuild a fork. We will spawn fresh contexts per worker by re-reading
  `store.render_scl()` into a temporary context. This is slightly slower but
  keeps thread safety simple.
- All writes back to the store happen through `finish_thread` on the main
  thread (via `concurrent.futures` callbacks or explicit polling).
- The TUI scheduler will poll `store.threads` each tick and harvest completed
  threads.

## 3. Typing Sensing

### 3.1 Web endpoint

`POST /api/typing` accepts `{"typing": bool}` and optionally a debounced
`duration_ms`. The server records:

```python
store.activity["typing_sessions"] = store.activity.get("typing_sessions", 0) + 1
store.note_activity("user_typing")
store.dirty = True
```

If `typing` is true and the organism is asleep, it is gently nudged toward wake
only if the lifecycle is already near a transition boundary (>= 80% of sleep
elapsed). This avoids constant wake-ups.

### 3.2 Web UI

The chat `<textarea>` gets `input` and `blur` listeners. On input, a 400 ms
debounced `POST /api/typing` fires. While typing, a small indicator appears near
the chat box and the organism orb pulses subtly.

### 3.3 TUI

`OrganismApp` adds an `on_input` handler on the chat `Input`. It records the
same activity counters and displays a transient "listening..." indicator.

## 4. Hybrid Memory Ranking

### 4.1 Write-time importance

Every memory entry gets an `importance` score computed by heuristics:

```python
def score_importance(kind: str, text: str, cycle: int, state: dict) -> float:
    base = 0.5
    kind_weights = {
        "faded": 1.0, "revived": 0.95, "born": 0.9,
        "harsh": 0.85, "kind": 0.75, "surprise": 0.8,
        "goal": 0.7, "learned": 0.65, "dream": 0.5,
        "command": 0.6, "diary": 0.55, "mud": 0.5,
    }
    score = base + kind_weights.get(kind, 0.0)
    if "user" in text.lower():
        score += 0.1
    if "?" in text:
        score += 0.05
    score += 0.02 * min(10, cycle / max(1, state.get("cycle", 1)))
    return min(1.0, score)
```

`BeliefStore.remember()` stores `importance` and a `recall` count.

### 4.2 Read-time relevance

```python
def score_relevance(memory: dict, query: str) -> float:
    mem_tokens = set(tokenize(memory["text"]))
    query_tokens = set(tokenize(query))
    if not mem_tokens or not query_tokens:
        return 0.0
    overlap = len(mem_tokens & query_tokens)
    return overlap / max(len(mem_tokens), len(query_tokens))
```

### 4.3 Final ranking

```python
def rank_memories(memories: list[dict], query: str, top_k: int = 8) -> list[dict]:
    return sorted(
        memories,
        key=lambda m: 0.6 * m.get("importance", 0.5)
                      + 0.3 * score_relevance(m, query)
                      + 0.1 * (m.get("recall", 0) / 100),
        reverse=True,
    )[:top_k]
```

Recall count is incremented each time a memory is selected for a prompt.

### 4.4 Integration points

- `state_snapshot()` in `narration.py` uses `MemoryScorer.rank_memories()` to
  select the episodes shown to the voice.
- `BeliefStore.remember()` computes and stores importance.
- The web snapshot exposes `memory_ranked` alongside the raw timeline.

## 5. Files to change

| File | Change |
|------|--------|
| `src/replicanta/threads.py` | New module: `CognitiveThread`, thread helpers, per-worker Scallop context. |
| `src/replicanta/memory.py` | New module: `MemoryScorer` with importance/relevance/rank. |
| `src/replicanta/organism.py` | Add `threads`/`thread_results` to `BeliefStore`; add `thread_pool` init to `Organism`. |
| `src/replicanta/narration.py` | Use `MemoryScorer` in `state_snapshot`. |
| `src/replicanta/web.py` | Add `/api/typing` and `typing()` adapter method. |
| `src/replicanta/web_static.py` | Add typing indicator and debounced `POST /api/typing`. |
| `src/replicanta/tui.py` | Add typing sensing in chat input; thread pool in `OrganismApp`. |
| `tests/test_threads.py` | New tests. |
| `tests/test_memory_scorer.py` | New tests. |
| `tests/test_web.py` | Add typing endpoint test. |
| `tests/test_narration.py` | Verify ranked memory exposure. |

## 6. Tests

- `test_threads.py`: lifecycle (pending→running→done), result harvest, no
  duplicate IDs, parallel execution finishes.
- `test_memory_scorer.py`: importance bounds, relevance matches query tokens,
  ranking prefers important + relevant memories.
- `test_web.py`: `POST /api/typing` returns 200, updates activity, nudge flag
  respects sleep boundary.
- `test_narration.py`: snapshot memory list respects rank.

## 7. Branches

Implementation lands on `main`, then `main` is fast-forward-merged into
`feature/ux-redesign` because the runtime is currently imported from
`.worktrees/ux-redesign`.
