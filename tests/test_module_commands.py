from replicanta.modules import CommandService


def test_command_service_register_and_dispatch():
    cmds = CommandService()
    called = []
    cmds.register("/hi", lambda args: called.append(args))
    cmds.dispatch("/hi", ["a", "b"])
    assert called == [["a", "b"]]


def test_command_service_unknown_returns_none():
    cmds = CommandService()
    assert cmds.dispatch("/unknown", []) is None
