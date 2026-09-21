"""Shared test fixtures: the cached ollama voice state, the spoken-voice
state and the extension registry are module-global, so reset them around
every test to keep reachability, speech and registry deterministic."""

import io

import pytest
from rich.console import Console

from replicanta import extensions, llmclient, speech


def renderable_text(widget, width=80):
    """Render a Static widget's current content to a plain string. Chrome
    widgets carry styled Rich renderables (top-bar grid, key-cap Text), so
    str() alone yields the object repr. Textual 8 stores Static content in
    the name-mangled private attribute `_Static__content`; the suite pins
    Textual 8.2.8, so an empty fallback shows up as a failed assertion if
    that ever moves."""
    content = getattr(widget, "_Static__content", "")
    console = Console(
        width=width,
        force_terminal=False,
        color_system=None,
        record=True,
        file=io.StringIO(),
    )
    console.print(content)
    return console.export_text()


def neuter_background_loops(monkeypatch, app):
    """Disable OrganismApp's private background loops for headless tests.

    NOTE: neutering `_on_tick` papers over a real teardown race in
    src/replicanta/tui.py — the 1s tick can fire while run_test() is
    tearing the DOM down, making `_append_log`'s query_one("#dreams")
    raise NoMatches. The race belongs fixed in tui.py (tracked by the
    desloppify plan, cluster test-strategy-hardening); until it lands,
    these patches keep that failure mode out of tests that assert
    unrelated behavior."""
    monkeypatch.setattr(app, "_probe_voice", lambda: None)
    monkeypatch.setattr(app, "_maybe_narrate", lambda: None)
    monkeypatch.setattr(app, "_on_tick", lambda: None)
    return app


@pytest.fixture
def headless_app(monkeypatch, tmp_path):
    """An OrganismApp hosting a freshly loaded tmp-path organism, with the
    voice probe, narration loop and tick timer neutered."""
    from replicanta.organism import Organism
    from replicanta.tui import OrganismApp

    org = Organism(tmp_path)
    org.load()
    return neuter_background_loops(monkeypatch, OrganismApp(org))


@pytest.fixture
def nursery_app(monkeypatch, tmp_path):
    """Like headless_app, but the organism lives in a real nursery under
    tmp_path (organisms/ + organism.scl seed) and app.root is wired."""
    from replicanta import nursery as nursery_mod
    from replicanta.organism import Organism
    from replicanta.tui import OrganismApp

    seed = tmp_path / "organism.scl"
    seed.write_text("type bel(x: String, a: String, v: String)\n")
    nursery_mod.create(tmp_path, "default", seed)
    org = Organism(nursery_mod.organism_dir(tmp_path, "default"))
    org.load()
    return neuter_background_loops(monkeypatch, OrganismApp(org, root=tmp_path))


async def wait_until(predicate, timeout=5.0, interval=0.02, message="condition"):
    """Poll a predicate with a deadline. Use only for waits that are
    genuinely about a background worker or interval timer — pilot.pause()
    covers all render/event-loop settling and should be preferred."""
    import asyncio
    import time

    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(f"wait_until timed out after {timeout}s: {message}")
        await asyncio.sleep(interval)


def patch_generate(monkeypatch, fake):
    """Patch the llmclient generation seam with a text-returning fake.

    Arena calls llmclient.generate_with_stats, which returns
    (text, stats); most tests only care about the text, so this wraps a
    classic str-returning fake and supplies zeroed token stats. Fakes
    that raise still raise. Tests asserting metered token counts should
    patch generate_with_stats directly with their own stats dict."""

    def wrapper(*a, **k):
        return fake(*a, **k), {"prompt_tokens": 0, "gen_tokens": 0}

    monkeypatch.setattr("replicanta.llmclient.generate_with_stats", wrapper)


@pytest.fixture(autouse=True)
def _reset_voice_state():
    llmclient.reset_voice()
    extensions.reset()
    speech.reset()
    yield
    llmclient.reset_voice()
    extensions.reset()
    speech.reset()
