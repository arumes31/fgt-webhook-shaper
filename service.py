from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import paramiko

from config import Settings

logger = logging.getLogger(__name__)
MAX_COMMAND_OUTPUT = 64 * 1024


class CommandError(RuntimeError):
    pass


def build_policy_command(policy_ids: tuple[int, ...], *, enabled: bool) -> str:
    status = "enable" if enabled else "disable"
    lines = ["config firewall shaping-policy"]
    for policy_id in policy_ids:
        lines.extend((f"edit {policy_id}", f"set status {status}", "next"))
    lines.append("end")
    return "\n".join(lines)


class FortiGateSSH:
    def __init__(self, settings: Settings):
        self.settings = settings

    def set_enabled(self, enabled: bool) -> None:
        client = paramiko.SSHClient()
        client.load_host_keys(str(self.settings.known_hosts_file))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        command = build_policy_command(self.settings.policy_ids, enabled=enabled)
        try:
            client.connect(
                hostname=self.settings.fortigate_host,
                port=self.settings.fortigate_port,
                username=self.settings.fortigate_user,
                password=self.settings.fortigate_password,
                allow_agent=False,
                look_for_keys=False,
                timeout=self.settings.command_timeout_seconds,
                banner_timeout=self.settings.command_timeout_seconds,
                auth_timeout=self.settings.command_timeout_seconds,
                channel_timeout=self.settings.command_timeout_seconds,
                disabled_algorithms={"keys": ["ssh-rsa"], "pubkeys": ["ssh-rsa"]},
            )
            # The command is constructed only from validated integer policy IDs.
            _, stdout, stderr = client.exec_command(  # nosec B601
                command,
                timeout=self.settings.command_timeout_seconds,
            )
            output = stdout.read(MAX_COMMAND_OUTPUT + 1)
            error = stderr.read(MAX_COMMAND_OUTPUT + 1)
            if len(output) > MAX_COMMAND_OUTPUT or len(error) > MAX_COMMAND_OUTPUT:
                raise CommandError("FortiGate response exceeded the output limit")
            if error.strip():
                raise CommandError("FortiGate rejected the shaping command")
        except (OSError, paramiko.SSHException, CommandError) as exc:
            logger.error("FortiGate SSH operation failed (%s)", type(exc).__name__)
            raise CommandError("FortiGate operation failed") from exc
        finally:
            client.close()


@dataclass(frozen=True)
class EventResult:
    status: str
    enabled: bool | None = None


class ShapingController:
    """Serialize device changes and own the single disable/inactivity scheduler."""

    def __init__(
        self,
        set_enabled: Callable[[bool], None],
        *,
        disable_delay_seconds: int,
        inactivity_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._set_enabled = set_enabled
        self._disable_delay = disable_delay_seconds
        self._inactivity = inactivity_seconds
        self._clock = clock
        self._condition = threading.Condition()
        self._last_event = clock()
        self._disable_deadline: float | None = None
        self._stopping = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self._condition:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._run,
                name="shaping-scheduler",
                daemon=True,
            )
            self._thread.start()

    def close(self) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def handle(self, event: str, wan_streams: int) -> EventResult:
        with self._condition:
            self._last_event = self._clock()
            if event in {"playback_start", "playback_resume"}:
                self._disable_deadline = None
                self._set_enabled(True)
                self._condition.notify_all()
                return EventResult("executed", enabled=True)

            threshold = 1 if event == "playback_stop" else 2
            if wan_streams >= threshold:
                return EventResult("ignored")

            if self._disable_deadline is None:
                self._disable_deadline = self._last_event + self._disable_delay
                self._condition.notify_all()
            return EventResult("scheduled", enabled=False)

    def worker_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def run_due_for_test(self) -> bool:
        with self._condition:
            return self._execute_due_locked(self._clock())

    def _execute_due_locked(self, now: float) -> bool:
        disable_due = self._disable_deadline is not None and now >= self._disable_deadline
        inactivity_due = now >= self._last_event + self._inactivity
        if not disable_due and not inactivity_due:
            return False
        self._disable_deadline = None
        self._last_event = now
        self._set_enabled(False)
        return True

    def _run(self) -> None:
        while True:
            with self._condition:
                if self._stopping:
                    return
                now = self._clock()
                if self._execute_due_locked(now):
                    continue
                deadlines = [self._last_event + self._inactivity]
                if self._disable_deadline is not None:
                    deadlines.append(self._disable_deadline)
                self._condition.wait(timeout=max(0.05, min(deadlines) - now))
