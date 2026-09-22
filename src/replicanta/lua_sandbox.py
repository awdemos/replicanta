"""Shared Lua runtime hardening plus the script ctx contract and dispatch
helpers for Replicanta hooks and modules."""

import logging

from lupa import LuaRuntime

logger = logging.getLogger(__name__)

# Lua standard library primitives we intentionally keep available.
_ALLOWED_LUA_GLOBALS = frozenset(
    {
        "assert",
        "error",
        "ipairs",
        "next",
        "pairs",
        "pcall",
        "print",
        "select",
        "tonumber",
        "tostring",
        "type",
        "xpcall",
        # Safe standard libraries only.
        "string",
        "table",
        "math",
        "coroutine",
    }
)


def _attribute_getter(obj, name):
    """Lupa attribute handler: expose public attributes, hide privates.

    Callers hand live Python objects (service registries, closures) to the
    sandbox. Without this, Lua can reach ``__class__.__base__.__subclasses__()``
    or ``fn.__globals__['__builtins__']`` from any injected object and escape
    to arbitrary Python — blocking underscore access closes that path while
    normal method calls (``services.get(...)``) keep working.
    """
    if isinstance(name, str) and name.startswith("_"):
        raise AttributeError(name)
    return getattr(obj, name)


def _attribute_setter(obj, name, value):
    if isinstance(name, str) and name.startswith("_"):
        raise AttributeError(name)
    setattr(obj, name, value)


# Names that must be removed from the Lua global table, including aliases that
# could be used to reconstruct blocked functionality.
_BLOCKED_GLOBALS = (
    "os",
    "io",
    "load",
    "loadfile",
    "loadstring",
    "require",
    "dofile",
    "package",
    "debug",
    "rawset",
    "rawget",
    "rawequal",
    "rawlen",
    "getmetatable",
    "setmetatable",
    "module",
    "collectgarbage",
    "python",  # lupa exposes this by default; removing it blocks Python escape
)


def build_runtime():
    """Create a hardened LuaRuntime for untrusted Replicanta scripts.

    The returned runtime:
    - keeps only a small allow-list of Lua globals;
    - blocks access to Python via the ``python`` global;
    - blocks underscore-prefixed attribute access on any Python object handed
      into the sandbox (no ``__class__``/``__globals__`` introspection);
    - prevents scripts from creating new globals during sandboxed execution.
    """
    lua = LuaRuntime(
        register_eval=False,
        register_builtins=False,
        attribute_handlers=(_attribute_getter, _attribute_setter),
    )

    # Block known-dangerous globals and aliases.
    for name in _BLOCKED_GLOBALS:
        try:
            lua.execute(f"{name} = nil")
        except Exception as _exc:  # noqa: BLE001
            # Some names may not exist in this Lua version; ignore.
            logger.debug("could not clear lua global %s: %s", name, _exc)

    globals_ = lua.globals()

    # Remove any remaining globals that are not in the allow-list. This also
    # catches lupa/Python internals that leak in unexpectedly.
    for key in list(globals_.keys()):
        if key not in _ALLOWED_LUA_GLOBALS:
            globals_[key] = None

    return lua


def sandboxed_execute(lua, code, name="script"):
    """Execute ``code`` in the already-hardened Lua runtime.

    ``name`` is ignored; it is accepted for caller convenience.
    """
    lua.execute(code)


def set_chaos(org, x):
    """ctx.set_chaos actuator: retune randomness (clamped to 0..1)."""
    org.store.chaos = max(0.0, min(1.0, float(x)))


def focus(org, attr):
    """ctx.focus actuator: steer attention (nil/empty clears)."""
    org.window.focus(attr if attr else None)
    org.store.attention = org.window.pairs


def build_hook_ctx(lua, org, event, text, emit):
    """Build the ctx table every on_<event>/main(ctx) handler receives.

    One copy of the documented ctx contract for both Lua engines
    (HookEngine and lua_host.LuaHost): the event summary plus live
    organism state and neurosymbolic activity counters, and the safe
    actuators ctx.log / ctx.set_chaos / ctx.focus. ``emit`` receives
    ctx.log lines.
    """
    m = org.metrics()
    mood = org.store.belief_value("self", "mood", "calm")
    return lua.table(
        event=event,
        text=text,
        state=org.lifecycle.state,
        cycle=org.store.cycle,
        mood=mood,
        belief_count=m.belief_count,
        rule_count=m.rule_count,
        score=m.score(),
        chaos=org.store.chaos,
        stress=org.store.stress,
        arousal=org.store.arousal,
        coherence=org.store.coherence,
        incoherence=org.store.incoherence,
        insane=org.store.insane,
        organism=org.dir_path.name,
        activity=lua.table_from(dict(org.store.activity)),
        log=lambda msg: emit(str(msg)),
        set_chaos=lambda x: set_chaos(org, x),
        focus=lambda attr: focus(org, attr),
    )


def collect_script_handlers(lua, scripts, event, emit):
    """Collect on_<event> handlers from every script, in script order.

    Classic scripts share one global table, so a script only counts when
    executing it CHANGES the current on_<event> global: the first
    definition wins and later redefinitions are skipped (identity check),
    while a script that defines no handler leaves the global untouched.
    A script that fails to execute reports one error line through
    ``emit`` and is skipped. Never raises.
    """
    handlers = []
    prev = lua.globals()[f"on_{event}"]
    same = lua.eval("function(a, b) return a == b end")
    for script in scripts:
        try:
            sandboxed_execute(lua, script.read_text(), name=script.name)
            hook = lua.globals()[f"on_{event}"]
            if hook is not None and not same(hook, prev):
                handlers.append((script.name, hook))
                prev = hook
        except Exception as exc:  # noqa: BLE001 — user scripts must never kill the organism
            emit(f"{script.name}: {exc}")
    return handlers


class DictProxy:
    """Expose Python dict keys as Lua-compatible attributes.

    Lupa does not expose dict keys as table pairs, and the sandbox's
    attribute getter hides dict keys from dot access; wrapping results in
    this proxy lets Lua modules read ``svc.info().neurons`` naturally.
    """

    def __init__(self, data):
        self._data = data

    def __getattr__(self, name):
        try:
            val = self._data[name]
        except KeyError as exc:
            raise AttributeError(name) from exc
        if isinstance(val, dict):
            return DictProxy(val)
        return val

    def __contains__(self, name):
        return name in self._data

    def __iter__(self):
        return iter(self._data)

    def items(self):
        return [(k, DictProxy(v) if isinstance(v, dict) else v) for k, v in self._data.items()]

    def __len__(self):
        return len(self._data)

    def __getitem__(self, key):
        return self._data[key]


def to_py(obj, seen=None):
    """Recursively convert lupa Lua tables to plain Python dicts/lists."""
    if seen is None:
        seen = set()
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    obj_id = id(obj)
    if obj_id in seen:
        return None
    seen.add(obj_id)
    try:
        if hasattr(obj, "items"):
            items = list(obj.items())
            if items and all(isinstance(k, int) for k, _ in items):
                keys = [k for k, _ in items]
                if min(keys) == 1 and max(keys) == len(keys):
                    return [to_py(v, seen) for _, v in sorted(items)]
            return {k: to_py(v, seen) for k, v in items}
        return obj
    finally:
        seen.discard(obj_id)
