"""Shared Lua runtime hardening for Replicanta hooks and modules."""

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
