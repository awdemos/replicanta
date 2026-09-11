import logging

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


def test_dynamic_event_subscribe_and_emit():
    hooks = HookService()
    called = []
    hooks.on("hand_goal", lambda text: called.append(text))
    hooks.emit("hand_goal", "wave")
    assert called == ["wave"]


def test_declare_marks_event_first_class_and_known_lists_it():
    hooks = HookService()
    assert "hand_goal" not in hooks.known()
    hooks.declare("hand_goal")
    assert "hand_goal" in hooks.known()
    assert "birth" in hooks.known()  # core events stay known


def test_undeclared_event_still_emits_and_debug_logs(caplog):
    hooks = HookService()
    called = []
    hooks.on("whatever", lambda text: called.append(text))
    with caplog.at_level(logging.DEBUG, logger="replicanta.modules"):
        hooks.emit("whatever", "x")
    assert called == ["x"]  # dynamism is not blocked...
    assert any("whatever" in rec.message for rec in caplog.records)  # ...but logged for typos


def test_declare_is_idempotent():
    hooks = HookService()
    hooks.declare("hand_goal")
    hooks.declare("hand_goal")
    assert hooks.known().count("hand_goal") == 1


def test_undeclared_subscribe_logs_debug(caplog):
    hooks = HookService()
    received = []
    with caplog.at_level(logging.DEBUG, logger="replicanta.modules"):
        hooks.on("cylce", lambda text: received.append(text))  # typo of "cycle"
    assert any("cylce" in rec.message for rec in caplog.records)
    hooks.emit("cylce", "hi")  # subscription still registered despite the typo log
    assert received == ["hi"]


def test_raising_handler_on_dynamic_event_does_not_escape(caplog):
    hooks = HookService()
    hooks.on("x", lambda _text: 1 / 0)
    with caplog.at_level(logging.WARNING, logger="replicanta.modules"):
        hooks.emit("x", "boom")  # must not raise
    assert any("hook handler for x failed" in rec.message for rec in caplog.records)


def test_bus_never_raises_on_weird_input():
    hooks = HookService()
    hooks.declare(None)  # str-coerced to "None"; must not raise
    hooks.declare(object())
    hooks.on({}, lambda _text: None)  # unhashable pre-coercion; str() makes it hashable
    hooks.on("cycle", None)  # non-callable handler contained per-handler on emit
    hooks.emit(None)
    hooks.emit(object(), "text")
    hooks.emit("cycle")  # delivers to the None handler -> contained warning
    assert "None" in hooks.known()
