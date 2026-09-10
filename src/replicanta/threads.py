"""Cognitive threads: named, concurrent background thought processes.

The organism can run several small reasoning tasks at once (self-questions,
reflection, planning, sensing) without blocking the lifecycle. Scallop contexts
are thread-affine, so each worker rebuilds a transient context from the current
genome; results are merged back on the main thread.
"""

import logging
import re
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import scallopy

logger = logging.getLogger(__name__)

BEL = "bel"
PROVENANCE = "minmaxprob"

_THREAD_ID_RE = re.compile(r"^[a-z0-9_\-]+$", re.IGNORECASE)


def _valid_thread_id(value: str) -> bool:
    return bool(_THREAD_ID_RE.match(value))


def _fresh_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class CognitiveThread:
    """One background thought process."""

    id: str
    kind: str
    status: str = "pending"
    created_cycle: int = 0
    payload: dict = field(default_factory=dict)
    result: Any = None
    error: str | None = None

    def __post_init__(self):
        if not _valid_thread_id(self.id):
            raise ValueError(f"invalid thread id {self.id!r}")


class ThreadPool:
    """Thin wrapper around ThreadPoolExecutor with lifecycle helpers.

    Keeps submitted futures keyed by thread id so the organism can harvest
    results without blocking.
    """

    def __init__(self, max_workers: int = 4):
        self.executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="cognitive"
        )
        self.pending: dict[str, Future] = {}

    def submit(
        self,
        thread_id: str,
        fn: Callable[..., Any],
        *args,
        **kwargs,
    ) -> str:
        """Submit ``fn(*args, **kwargs)`` and track it under ``thread_id``."""
        future = self.executor.submit(fn, *args, **kwargs)
        self.pending[thread_id] = future
        return thread_id

    def harvest(self) -> list[tuple[str, Any | None, str | None]]:
        """Return done futures as (thread_id, result, error) and remove them."""
        done: list[tuple[str, Any | None, str | None]] = []
        for thread_id, future in list(self.pending.items()):
            if not future.done():
                continue
            del self.pending[thread_id]
            try:
                result = future.result()
                done.append((thread_id, result, None))
            except Exception as exc:  # noqa: BLE001 — thread errors are logged, not fatal
                logger.warning("thread %s failed: %s", thread_id, exc)
                done.append((thread_id, None, str(exc)))
        return done

    def shutdown(self, wait: bool = False):
        self.executor.shutdown(wait=wait, cancel_futures=True)


def derive_in_thread(genome_text: str, rule: str, head_relation: str):
    """Run one Scallop rule in a worker context built from ``genome_text``.

    Returns ``(tag, tuple)`` list. The worker creates its own context so the
    main thread's context is never touched from another thread.
    """
    ctx = scallopy.ScallopContext(provenance=PROVENANCE)
    if genome_text.strip():
        # Import the rendered genome directly; it already contains rel facts.
        ctx.add_program(genome_text)
    ctx.add_rule(rule)
    ctx.run()
    return [(float(tag), tuple(tup)) for (tag, tup) in ctx.relation(head_relation)]


def make_self_question_thread(
    attr_a: str,
    val_a: str,
    attr_b: str,
    val_b: str,
    rule_counter: int,
    created_cycle: int = 0,
) -> tuple[CognitiveThread, str, str]:
    """Build a thread that asks what follows from two attribute/value pairs.

    Returns the thread, the rule text, and the generated head relation name.
    """
    head = f"q{rule_counter}"
    rule = (
        f'{head}(x) = {BEL}(x, "{attr_a}", "{val_a}"), '
        f'{BEL}(x, "{attr_b}", "{val_b}")'
    )
    thread = CognitiveThread(
        id=f"self_question_{_fresh_id()}",
        kind="self_question",
        created_cycle=created_cycle,
        payload={
            "head": head,
            "rule": rule,
            "combo": f"{val_a}_{val_b}",
            "attr_a": attr_a,
            "val_a": val_a,
            "attr_b": attr_b,
            "val_b": val_b,
        },
    )
    return thread, rule, head
