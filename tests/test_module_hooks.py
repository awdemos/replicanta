from replicanta.modules import HookService


def test_hook_service_subscribe_and_emit():
    hooks = HookService()
    called = []
    hooks.on("birth", lambda text: called.append(("birth", text)))
    hooks.emit("birth", "hello")
    assert called == [("birth", "hello")]


def test_hook_service_multiple_handlers():
    hooks = HookService()
    called = []
    hooks.on("cycle", lambda text: called.append(1))
    hooks.on("cycle", lambda text: called.append(2))
    hooks.emit("cycle", "wake")
    assert called == [1, 2]


def test_hook_service_unknown_event_is_noop():
    hooks = HookService()
    hooks.emit("unknown", "x")  # must not raise
