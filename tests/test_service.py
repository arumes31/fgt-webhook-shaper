import logging
from unittest.mock import MagicMock, patch

import paramiko
import pytest

from service import (
    MAX_COMMAND_OUTPUT,
    CommandError,
    FortiGateSSH,
    ShapingController,
    build_policy_command,
)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_command_is_generated_only_from_numeric_policy_ids():
    command = build_policy_command((17, 21), enabled=True)

    assert command == (
        "config firewall shaping-policy\n"
        "edit 17\nset status enable\nnext\n"
        "edit 21\nset status enable\nnext\nend"
    )


def test_controller_schedules_and_executes_one_disable():
    calls = []
    clock = Clock()
    controller = ShapingController(
        calls.append,
        disable_delay_seconds=120,
        inactivity_seconds=7200,
        clock=clock,
    )

    assert controller.handle("playback_stop", 0).status == "scheduled"
    clock.now += 119
    assert controller.run_due_for_test() is False
    clock.now += 1
    assert controller.run_due_for_test() is True
    assert controller.run_due_for_test() is False
    assert calls == [False]


def test_resume_cancels_pending_disable_and_finishes_enabled():
    calls = []
    clock = Clock()
    controller = ShapingController(
        calls.append,
        disable_delay_seconds=120,
        inactivity_seconds=7200,
        clock=clock,
    )
    controller.handle("playback_pause", 0)
    clock.now += 60

    result = controller.handle("playback_resume", 0)
    clock.now += 120

    assert result.enabled is True
    assert controller.run_due_for_test() is False
    assert calls == [True]


@pytest.mark.parametrize(
    ("event", "streams"),
    [("playback_pause", 2), ("playback_stop", 1)],
)
def test_active_streams_prevent_disable(event, streams):
    controller = ShapingController(
        lambda _: pytest.fail("device must not be changed"),
        disable_delay_seconds=120,
        inactivity_seconds=7200,
    )

    assert controller.handle(event, streams).status == "ignored"


def test_inactivity_disables_once():
    calls = []
    clock = Clock()
    controller = ShapingController(
        calls.append,
        disable_delay_seconds=120,
        inactivity_seconds=300,
        clock=clock,
    )
    clock.now += 300

    assert controller.run_due_for_test() is True
    assert calls == [False]


def test_controller_worker_lifecycle_is_idempotent():
    controller = ShapingController(
        lambda _: None,
        disable_delay_seconds=120,
        inactivity_seconds=7200,
    )

    controller.start()
    first_thread = controller._thread
    controller.start()

    assert controller.worker_alive() is True
    assert controller._thread is first_thread
    controller.close()
    assert controller.worker_alive() is False


def test_worker_waits_for_nearest_deadline_then_stops():
    clock = Clock()
    controller = ShapingController(
        lambda _: None,
        disable_delay_seconds=120,
        inactivity_seconds=7200,
        clock=clock,
    )
    controller.handle("playback_stop", 0)

    class StopCondition:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def wait(self, timeout):
            assert timeout == 120
            controller._stopping = True

    controller._condition = StopCondition()
    controller._run()


def test_ssh_enforces_pinned_host_keys_and_disables_legacy_rsa(settings):
    client = MagicMock()
    client.exec_command.return_value = (MagicMock(), MagicMock(), MagicMock())
    client.exec_command.return_value[1].read.return_value = b""
    client.exec_command.return_value[2].read.return_value = b""

    with patch("service.paramiko.SSHClient", return_value=client):
        FortiGateSSH(settings).set_enabled(True)

    client.load_host_keys.assert_called_once_with("tests\\fixtures\\known_hosts")
    assert isinstance(client.set_missing_host_key_policy.call_args.args[0], paramiko.RejectPolicy)
    kwargs = client.connect.call_args.kwargs
    assert kwargs["allow_agent"] is False
    assert kwargs["look_for_keys"] is False
    assert kwargs["disabled_algorithms"] == {"keys": ["ssh-rsa"], "pubkeys": ["ssh-rsa"]}
    client.close.assert_called_once()


def test_ssh_failure_is_generic_and_does_not_log_exception_text(settings, caplog):
    client = MagicMock()
    client.connect.side_effect = paramiko.SSHException("test-device-password")

    with (
        patch("service.paramiko.SSHClient", return_value=client),
        caplog.at_level(logging.ERROR),
        pytest.raises(CommandError, match="operation failed"),
    ):
        FortiGateSSH(settings).set_enabled(True)

    assert "test-device-password" not in caplog.text
    client.close.assert_called_once()


def test_ssh_rejects_unbounded_device_output(settings):
    client = MagicMock()
    client.exec_command.return_value = (MagicMock(), MagicMock(), MagicMock())
    client.exec_command.return_value[1].read.return_value = b"x" * (MAX_COMMAND_OUTPUT + 1)
    client.exec_command.return_value[2].read.return_value = b""

    with (
        patch("service.paramiko.SSHClient", return_value=client),
        pytest.raises(CommandError, match="operation failed"),
    ):
        FortiGateSSH(settings).set_enabled(True)


def test_ssh_rejects_device_stderr(settings):
    client = MagicMock()
    client.exec_command.return_value = (MagicMock(), MagicMock(), MagicMock())
    client.exec_command.return_value[1].read.return_value = b""
    client.exec_command.return_value[2].read.return_value = b"command failed"

    with (
        patch("service.paramiko.SSHClient", return_value=client),
        pytest.raises(CommandError, match="operation failed"),
    ):
        FortiGateSSH(settings).set_enabled(False)
