# Replicanta Deep Bug-Fix Pass

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` or `superpowers:subagent-driven-development`. Steps use checkbox syntax for tracking.

**Date:** 2026-08-17  
**Project:** `/var/home/a/code/replicanta`  
**Priority order:** Security → Performance → Architecture  
**Goal:** Close all known critical/high bugs in the three categories without breaking the public CLI/TUI.

---

## Cross-cutting rules

- One atomic commit per task.
- Every code change must be preceded by a failing test that demonstrates the bug or enforces the new behavior (TDD).
- No `as any`, `@ts-ignore`, empty catch blocks, or deleting failing tests.
- After each task: `python -m pytest <relevant tests> -q` must pass.
- Final verification after all tasks: `python -m pytest --tb=short`, `ruff check src/replicanta tests`, `ruff format --check src/replicanta tests`.

---

## Security tasks

### S1: Harden Lua sandbox against RCE escape

**Files:**
- Modify: `src/replicanta/hooks.py` (Lua runtime setup and `fire`/`run`)
- Modify: `src/replicanta/modules.py` (shared Lua module runtime)
- Test: `tests/test_hooks.py`, `tests/test_modules.py`

**Current flaw:** `_BLOCKED_GLOBALS` in `hooks.py` only nils a short list of top-level names. `python` is not blocked, so Lua scripts can walk `python.none.__class__.__base__.__subclasses__()` to reach `os` and call `os.system()`. The same runtime setup is used by `modules.py` to run `init.lua` for every loaded module.

**Change:**
- Add a small helper in `hooks.py` that builds a hardened `LuaRuntime`:
  - Create with `register_eval=False`, `register_builtins=False`.
  - After creation, walk `lua.globals()` and remove any key that is not in an explicit `_ALLOWED_LUA_GLOBALS` allow-list.
  - Explicitly remove `python`, `rawset`, `rawget`, `getmetatable`, `setmetatable`, `module`, `package`, `load`, `loadfile`, `dofile`, and any alias that can reconstruct blocked globals.
  - Set a metatable on `_G` with `__newindex` that rejects creating new globals; keep `__index` for allowed read-only access.
- Use this helper from both `hooks.py` and `modules.py` so both sandboxes are identical.

**Failing test:**

```python
def test_lua_sandbox_cannot_reach_python_os():
    from replicanta.hooks import HookEngine

    engine = HookEngine()
    script = """
local ok, err = pcall(function()
    local os = python.none.__class__.__base__.__subclasses__()
    -- if we got here, the sandbox leaked
    return true
end)
return not ok
"""
    result = engine.run("test", script)
    assert result is True
```

**Verification:**

```bash
pytest tests/test_hooks.py tests/test_modules.py -q
```

Expected: all sandbox tests pass; the existing PoC marker cannot be reproduced.

**Commit message:** `security(lua): harden Lua sandbox and share runtime setup between hooks and modules`

---

### S2: Add bearer token authentication to `/api/*` endpoints

**Files:**
- Modify: `src/replicanta/web.py` (server setup and request handler)
- Modify: `src/replicanta/web_static.py` (client `api()` helper)
- Test: `tests/test_web.py`

**Current flaw:** `make_server()` binds to `127.0.0.1` and serves `/api/*` without any authentication. Any local process or malicious local page can POST `/api/chat`, `/api/settings`, `/api/mutation`, etc.

**Change:**
- In `make_server()`, generate a random 32-byte token (or accept `REPLICANTA_WEB_TOKEN` from env). Attach it to the `Glasshouse` instance.
- In the request handler, for every path under `/api/*` (except static asset routes), require a header `X-Replicanta-Token` matching the server token. Return 403 otherwise.
- Inject the token into the served HTML as `window.__REPLICANTA_TOKEN__ = '<token>';` inside `APP_HTML`.
- Update the bundled JS `api()` function to send the token in `X-Replicanta-Token`.

**Failing test:**

```python
def test_api_requires_token():
    glasshouse = Glasshouse(organism=make_test_organism())
    server = make_server(glasshouse, port=0)
    token = server.token
    base = f"http://127.0.0.1:{server.server_port}"
    # missing token -> 403
    r = requests.post(f"{base}/api/chat", json={"text": "hi"})
    assert r.status_code == 403
    # valid token -> success
    r = requests.post(f"{base}/api/chat", json={"text": "hi"}, headers={"X-Replicanta-Token": token})
    assert r.status_code == 200
```

**Verification:**

```bash
pytest tests/test_web.py -q
```

**Commit message:** `security(web): require bearer token for all /api/* mutation endpoints`

---

### S3: Constrain `/export` to safe filenames and export directory

**Files:**
- Modify: `src/replicanta/web.py` (`_export_chat`, `/api/command /export` handler)
- Modify: `src/replicanta/fileutil.py` (`atomic_write_text` path guard helper)
- Test: `tests/test_web.py`, `tests/test_fileutil.py`

**Current flaw:** The export command passes user input through `Path(path).expanduser()` and writes directly to it. Absolute paths (`/etc/passwd`) and parent traversal (`../../.bashrc`) are allowed.

**Change:**
- In `fileutil.py`, add a helper `safe_filename(name: str, base_dir: Path) -> Path` that:
  - rejects names containing `/`, `\\`, `..`, or leading `.`;
  - resolves the result under `base_dir` and ensures it stays inside `base_dir`;
  - raises `ValueError("unsafe export path")` otherwise.
- In `web.py`, force exports into `Path.home() / "replicanta-exports"` and validate the supplied name through `safe_filename`.

**Failing test:**

```python
def test_export_rejects_traversal():
    glasshouse = Glasshouse(organism=make_test_organism())
    glasshouse._export_chat("../pwned.md")
    assert not (Path.home() / "pwned.md").exists()
    # and raises ValueError
```

**Verification:**

```bash
pytest tests/test_web.py -k export tests/test_fileutil.py -q
```

**Commit message:** `security(web): constrain /export to safe filenames and export directory`

---

### S4: Restrict LLM backend URLs to localhost/loopback to block SSRF

**Files:**
- Modify: `src/replicanta/llmclient.py` (`ollama_url`, `llama_cpp_url`, request helpers)
- Test: `tests/test_llmclient.py`

**Current flaw:** `ollama_url()` and `llama_cpp_url()` read URLs from `os.environ` with no validation and pass them directly to `urllib.request.urlopen`. A manipulated environment can redirect LLM traffic to cloud metadata endpoints, internal services, or `file://` URLs.

**Change:**
- Add `_is_allowed_url(url: str) -> bool` that parses the URL and permits only:
  - schemes `http` or `https`;
  - hosts `localhost`, `127.0.0.1`, `::1`, or hostnames ending in `.local`;
  - no userinfo, no `file://`, no `ftp://`, and no link-local/metadata IPs such as `169.254.169.254`.
- Wrap every outgoing request through `_safe_urlopen(req, timeout)` that validates `req.full_url` and raises `RuntimeError("refusing to connect to disallowed URL")` otherwise.

**Failing test:**

```python
def test_generate_rejects_metadata_ssrf():
    from replicanta.llmclient import _is_allowed_url

    assert not _is_allowed_url("http://169.254.169.254/latest/meta-data/")
    assert not _is_allowed_url("file:///etc/passwd")
    assert _is_allowed_url("http://localhost:11434/api/generate")
```

**Verification:**

```bash
pytest tests/test_llmclient.py -q
```

**Commit message:** `security(llm): restrict backend URLs to localhost/loopback to block SSRF`

---

### S5: Validate every proposed and approved extension entry

**Files:**
- Modify: `src/replicanta/extensions.py` (`propose`, `approve`, `validate`)
- Test: `tests/test_modules.py`, `tests/test_web.py`

**Current flaw:** `propose()` does not call `validate()`. `approve()` applies pending entries without re-validation. `auto_apply=True` silently commits invalid entries.

**Change:**
- In `propose(path, entry, auto_apply=False)`, call `ok, reason = validate(entry)` first; if not ok, raise `ValueError(f"invalid extension: {reason}")`.
- In `approve(path)`, after reading the pending entry, call `validate(entry)`; if invalid, clear pending, write the registry back, and return `None` (or raise a clear error). Never append an invalid entry.
- Keep `auto_apply` gated by the same validation.

**Failing test:**

```python
def test_propose_rejects_invalid_entry():
    reg = ExtensionRegistry()
    bad = {"type": "pattern", "term": "("}  # invalid regex
    with pytest.raises(ValueError, match="invalid extension"):
        reg.propose("test", bad)
```

**Verification:**

```bash
pytest tests/test_modules.py tests/test_web.py -k extension -q
```

**Commit message:** `security(extensions): validate every proposed and approved entry`

---

### S6: Harden auto-apply patch integrity with allow-list and hash verification

**Files:**
- Modify: `src/replicanta/organism.py` (`auto_apply_patches`, patch application path)
- Modify: `src/replicanta/config.py` (add `patch_policy` setting)
- Test: `tests/test_organism.py`

**Current flaw:** `auto_apply_patches` defaults to `True` and applies LLM-generated patches without a signature or content hash. Patches can target any file the process can write, including source files outside the organism directory.

**Change:**
- Add a config setting `patch_policy` with values `off`, `ask`, `signed`, `auto` (default `ask`).
- Add an allow-list: a patch may only modify files inside the organism's own directory (`<base>/<name>/`).
- Each patch record must include:
  - `path` (relative to organism dir);
  - `old_hunk` and `new_hunk`;
  - `sha256` of `path + old_hunk + new_hunk`.
- Before applying, verify the hash and ensure the current file content contains `old_hunk`. If the hash or context mismatch, reject the patch.
- Store applied patches in `<organism>/patches/` with a manifest instead of mutating files directly from JSON.

**Failing test:**

```python
def test_auto_apply_rejects_bad_hash_and_outside_path():
    org = make_test_organism(patch_policy="auto")
    bad_patch = {
        "path": "../evil.py",
        "old_hunk": "x",
        "new_hunk": "y",
        "sha256": "deadbeef",
    }
    with pytest.raises(ValueError, match="outside organism"):
        org.apply_patch(bad_patch)
```

**Verification:**

```bash
pytest tests/test_organism.py -k patch -q
```

**Commit message:** `security(organism): harden auto-apply patches with allow-list and hash verification`

---

### S7: Sanitize voice/speech subprocess and download paths

**Files:**
- Modify: `src/replicanta/speech.py` (`download_voice`, text-to-speech shell-out)
- Modify: `src/replicanta/web.py` (`/voice` GET)
- Test: `tests/test_speech.py`, `tests/test_web.py`

**Current flaw:** `download_voice()` shells out to `curl` with a voice name/path from environment/user input. The web `/voice` endpoint passes `request.args` directly. Text passed to `piper-tts`/`espeak` is not quoted, allowing command injection.

**Change:**
- Validate voice names against `^[a-zA-Z0-9_\-]+$`; reject path separators and `..`.
- Force downloads into a controlled cache directory (`Path.home() / ".cache/replicanta/voices"`).
- Replace `curl` subprocess with `urllib.request.urlretrieve` (or `httpx`) restricted by the same URL allow-list as S4.
- Use `shlex.quote` for all shell arguments passed to `piper-tts`/`espeak`/`aplay`.

**Failing test:**

```python
def test_download_voice_rejects_bad_name():
    from replicanta.speech import download_voice

    with pytest.raises(ValueError, match="invalid voice name"):
        download_voice("../system")
```

**Verification:**

```bash
pytest tests/test_speech.py tests/test_web.py -k voice -q
```

**Commit message:** `security(speech): sanitize voice names, downloads, and subprocess arguments`

---

## Performance tasks

### P1: Cache `state_snapshot` across unchanged organism state

**Files:**
- Modify: `src/replicanta/narration.py` (`state_snapshot`)
- Test: `tests/test_narration.py`

**Current flaw:** `state_snapshot()` is called before every voice utterance and rebuilds everything: full belief sort, skill list disk read, Scallop derivation, host uname subprocess, memory ranking.

**Change:**
- Add an internal cache keyed by a snapshot of the inputs that affect the returned dict: `(org.store.cycle, org.store.dirty, id(org.store.beliefs_map), id(org.store.chat_log), id(org.store.memory), id(org.store.rules))`.
- Return the cached dict when inputs are unchanged.
- Keep side-effecting calls (`record_digest`, `mark_recalled`) outside the cache path so they still run each tick, but only when inputs actually changed.

**Failing test:**

```python
def test_state_snapshot_cached():
    org = make_test_organism()
    snap1 = state_snapshot(org)
    snap2 = state_snapshot(org)
    assert snap1 is snap2
```

**Verification:**

```bash
pytest tests/test_narration.py -q
```

**Commit message:** `perf(narration): cache state_snapshot across unchanged organism state`

---

### P2: Use `ThreadingHTTPServer` with RLock guard

**Files:**
- Modify: `src/replicanta/web.py` (`make_server`, `run`)
- Test: `tests/test_web.py`

**Current flaw:** The web server uses single-threaded `HTTPServer`. A single LLM call in `/api/chat` blocks all other web clients and the organism loop.

**Change:**
- Switch to `ThreadingHTTPServer` from `http.server`.
- Keep the existing `Glasshouse` RLock as the only concurrency guard so Scallop thread affinity is preserved while reads and non-mutating requests can run concurrently.

**Failing test:**

```python
def test_concurrent_state_requests():
    glasshouse = Glasshouse(organism=make_test_organism())
    server = make_server(glasshouse, port=0)
    base = f"http://127.0.0.1:{server.server_port}"
    token = server.token
    # fire two /api/state requests in parallel
    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(requests.get, f"{base}/api/state", headers={"X-Replicanta-Token": token})
        f2 = pool.submit(requests.get, f"{base}/api/state", headers={"X-Replicanta-Token": token})
    assert f1.result().status_code == 200
    assert f2.result().status_code == 200
```

**Verification:**

```bash
pytest tests/test_web.py -k concurrent -q
pytest tests/test_web.py -q
```

**Commit message:** `perf(web): use ThreadingHTTPServer with app-level RLock for concurrency`

---

### P3: Index `BeliefStore` by `(obj, attr)` and avoid per-add Scallop rebuild

**Files:**
- Modify: `src/replicanta/organism.py` (`BeliefStore.add`, `_note_scallop_contradictions`, `derived`)
- Test: `tests/test_organism.py`

**Current flaw:** `add()` scans the entire `beliefs_map` for each new belief and calls `derived()`, which rebuilds a fresh `ScallopContext` and adds every belief as a fact.

**Change:**
- Maintain `self._by_obj_attr: dict[tuple[str,str], dict[str, float]]` and update it in `add`, `observe`, `load`, and legacy migration.
- Detect contradictions in O(1) via the index.
- Cache the contradiction/derived result and only invalidate it when beliefs/rules change.

**Failing test:**

```python
def test_add_many_beliefs_scales():
    store = BeliefStore()
    for i in range(500):
        store.add(fact(f"obj{i}", "attr", "val"), confidence=0.9)
    assert len(store.beliefs()) == 500
```

**Verification:**

```bash
pytest tests/test_organism.py -q
```

**Commit message:** `perf(beliefs): index by (obj, attr) and avoid per-derived Scallop rebuild`

---

### P4: Cache `SkillStore.list` based on directory mtime

**Files:**
- Modify: `src/replicanta/skills.py` (`SkillStore.list`, `save`, `_archive`)
- Test: `tests/test_skills.py`

**Current flaw:** `list()` reads and parses every skill markdown file from disk on every call. It is invoked by `relevant`, archive functions, and `state_snapshot`.

**Change:**
- Cache the skill list keyed by the directory's mtime and inode.
- Invalidate the cache in `save()`, `_archive()`, and any method that writes skill files.

**Failing test:**

```python
def test_list_uses_mtime_cache():
    store = SkillStore(tmp_path)
    store.save(make_skill("a"))
    store.list()
    with patch.object(Path, "read_text") as mock_read:
        store.list()
    mock_read.assert_not_called()
```

**Verification:**

```bash
pytest tests/test_skills.py -q
```

**Commit message:** `perf(skills): cache skill list based on directory mtime`

---

### P5: Replace `probe.uname` subprocess with pure Python

**Files:**
- Modify: `src/replicanta/probe.py` (`SystemProbe.uname`, `_host_uname`)
- Test: `tests/test_probe.py`

**Current flaw:** `_host_uname()` spawns `uname -snrm` via `subprocess.run` every time `state_snapshot` is called (once per voice utterance).

**Change:**
- Use `platform.uname()` and `platform.release()` from the standard library to build the same string.
- Keep the `uname=None` injectable constructor parameter for tests; default to the pure-Python function.

**Failing test:**

```python
def test_uname_uses_platform_not_subprocess():
    probe = SystemProbe()
    with patch("subprocess.run") as mock_run:
        result = probe.uname()
    mock_run.assert_not_called()
    assert result is not None
```

**Verification:**

```bash
pytest tests/test_probe.py -k uname -q
```

**Commit message:** `perf(probe): replace uname subprocess with platform module`

---

## Architecture tasks

### A1: Isolate global extension and voice registries per thread

**Files:**
- Modify: `src/replicanta/extensions.py` (`_REGISTRY`)
- Modify: `src/replicanta/llmclient.py` (`_voice`)
- Test: `tests/test_modules.py`, `tests/test_organism.py`, `tests/test_llmclient.py`

**Current flaw:** `extensions._REGISTRY` and `llmclient._voice` are process-global mutable state. Tests leak into each other, and multi-organism nurseries are unreliable.

**Change:**
- Wrap `_REGISTRY` in `threading.local()` or convert `extensions` functions to delegate to a default per-thread registry.
- Move `_voice` state into a thread-local or an instance held by the voice interface. Update `reset_voice()` to clear the thread-local.
- Keep module-level convenience functions for callers in `learning.py`, `llmclient.py`, `sentiment.py`.

**Failing test:**

```python
def test_global_registry_isolation():
    reg_a = get_registry()
    reg_b = get_registry()
    reg_a.propose("x", {"type": "seed", "term": "a"}, auto_apply=True)
    assert "x" not in reg_b.entries()
```

**Verification:**

```bash
pytest tests/test_modules.py tests/test_organism.py tests/test_llmclient.py -q
```

**Commit message:** `arch: isolate global extension/voice registries per thread`

---

### A2: Keep `ThreadPool` transient and non-serializable

**Files:**
- Modify: `src/replicanta/threads.py` (`ThreadPool`)
- Modify: `src/replicanta/organism.py` (where the pool is persisted/loaded)
- Test: `tests/test_threads.py`, `tests/test_organism.py`

**Current flaw:** `ThreadPool` holds a live `ThreadPoolExecutor`, which cannot be pickled and should not be serialized into `state.json`.

**Change:**
- Remove `ThreadPool` from serialized state; keep it as a transient attribute on `Organism`.
- Add `__getstate__` to `ThreadPool` that raises `TypeError` or excludes executor fields.
- Add `__setstate__` or a re-init path that recreates the executor on load.

**Failing test:**

```python
def test_save_does_not_pickle_threadpool():
    org = make_test_organism()
    org.store.save()
    state_file = org.store.state_path
    text = state_file.read_text()
    assert "ThreadPoolExecutor" not in text
```

**Verification:**

```bash
pytest tests/test_organism.py tests/test_threads.py -q
```

**Commit message:** `arch(threads): keep ThreadPool transient and non-serializable`

---

### A3: Unify shared command dispatch between TUI and web

**Files:**
- Modify: `src/replicanta/tui_commands.py`
- Modify: `src/replicanta/web.py`
- Modify: `src/replicanta/tui.py`
- Test: `tests/test_tui_commands.py`, `tests/test_web.py`, `tests/test_tui.py`

**Current flaw:** Command handling is duplicated across `web.py`, `tui.py`, and the static `COMMANDS` list in `tui_commands.py`. Behavior diverges and new commands require editing multiple places.

**Change:**
- Promote `tui_commands.py` to a single `CommandRegistry` with handler functions, argument schemas, and metadata (`destructive`, `requires_confirmation`, `contexts`).
- Move shared command implementations from `web.py` and `tui.py` into registered handlers.
- Make both TUI and web dispatch through `CommandRegistry.dispatch(name, args, ctx)`.
- Keep TUI-only commands (`/look`, `/listen`, MUD group chat) out of the registry in this pass.

**Failing test:**

```python
def test_shared_commands_have_identical_behavior():
    for name in SHARED_COMMANDS:
        tui_result = tui_dispatch(name, args)
        web_result = web_dispatch(name, args)
        assert tui_result == web_result
```

**Verification:**

```bash
pytest tests/test_tui_commands.py tests/test_web.py tests/test_tui.py -q
```

**Commit message:** `arch(commands): unify TUI and web slash command dispatch`

---

### A4: Make `Mind` a view over `BeliefStore`

**Files:**
- Modify: `src/replicanta/organism.py` (`Mind`, `BeliefStore`)
- Modify: `src/replicanta/narration.py` (`state_snapshot`)
- Test: `tests/test_organism.py`

**Current flaw:** `Mind` loads `organism.scl` into its own Scallop context while `BeliefStore` keeps a separate `beliefs_map`. They can drift, and `state_snapshot` builds yet another context.

**Change:**
- Make `Mind` a thin wrapper that always operates on the `BeliefStore` map: `Mind.rebuild()` imports `store.render_scl()` and caches the context; `Mind.beliefs()` returns `store.beliefs()`; `Mind.derive()` runs transient rules against the cached context.
- Add `BeliefStore.mind` lazily and rebuild only when `store.genome_dirty` is True.

**Failing test:**

```python
def test_mind_and_store_never_drift():
    org = make_test_organism()
    org.store.add(fact("user", "name", "Ada"), confidence=0.9)
    org.store.save()
    org2 = Organism.load(org.path)
    assert org2.mind.beliefs() == org2.store.beliefs()
```

**Verification:**

```bash
pytest tests/test_organism.py -q
```

**Commit message:** `arch(organism): make Mind a view over BeliefStore instead of duplicate source`

---

### A5: Narrow broad `except Exception` handlers

**Files:**
- Modify: `src/replicanta/config.py`
- Modify: `src/replicanta/organism.py`
- Modify: `src/replicanta/web.py`
- Modify: `src/replicanta/modules.py`
- Test: `tests/test_config.py`, `tests/test_organism.py`, `tests/test_web.py`, `tests/test_modules.py`

**Current flaw:** Broad `except Exception` hides programming errors and makes debugging hard.

**Change:**
- In `config.py`: catch `OSError`, `tomllib.TOMLDecodeError`, `UnicodeDecodeError`, `TypeError` only.
- In `organism.py::load_mud_session`: catch `OSError`, `json.JSONDecodeError`, `KeyError`, `AttributeError`.
- In `web.py`: catch `ImportError`, `OSError`, `subprocess.SubprocessError`, and module-specific exceptions; let unexpected exceptions propagate with context.
- In `modules.py::_init_module`: catch `lupa.LuaError`, `OSError`, `TypeError`, `ValueError`.
- If a fallback `except Exception` is needed, log at `CRITICAL` and re-raise; never silently swallow.

**Failing test:**

```python
def test_config_catches_toml_errors_not_bugs():
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".toml", delete=False, mode="w") as f:
        f.write("[invalid\n")
        f.flush()
    cfg = load_config(f.name)
    # falls back to defaults without crashing
    assert cfg is not None
```

**Verification:**

```bash
pytest tests/test_config.py tests/test_organism.py tests/test_web.py tests/test_modules.py -q
rg "except Exception" src/replicanta  # should be zero in changed files
```

**Commit message:** `arch(errors): narrow exception handlers to expected failure modes`

---

## Final verification

After all commits:

```bash
python -m pytest --tb=short
ruff check src/replicanta tests
ruff format --check src/replicanta tests
```

Expected final state:
- `pytest` passes with no failures.
- `ruff check` reports zero errors.
- No new broad `except Exception` or silent swallow remains in changed files.
