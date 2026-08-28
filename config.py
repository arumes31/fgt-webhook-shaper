from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

import paramiko

KNOWN_DEFAULTS = {
    "admin",
    "changeme",
    "default",
    "fortigate",
    "password",
    "secret",
}
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}[A-Za-z0-9]$|^[A-Za-z0-9]$")
USER_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")


def read_secret(name: str, *, minimum_length: int = 16) -> str:
    direct = os.environ.get(name)
    file_name = os.environ.get(f"{name}_FILE")
    if direct is not None and file_name:
        raise RuntimeError(f"set only one of {name} or {name}_FILE")
    if file_name:
        try:
            direct = Path(file_name).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"unable to read {name}_FILE") from exc
    if direct is None or not direct.strip():
        raise RuntimeError(f"{name} or {name}_FILE is required")
    value = direct.strip()
    if len(value) < minimum_length or value.casefold() in KNOWN_DEFAULTS:
        raise RuntimeError(f"{name} is too weak")
    return value


def required_env(name: str, *, maximum_length: int = 253) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    if len(value) > maximum_length:
        raise RuntimeError(f"{name} is too long")
    return value


def integer_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def policy_ids_env() -> tuple[int, ...]:
    raw = os.environ.get("FORTIGATE_POLICY_IDS", "17,21,22")
    try:
        values = tuple(dict.fromkeys(int(item.strip()) for item in raw.split(",")))
    except ValueError as exc:
        raise RuntimeError("FORTIGATE_POLICY_IDS must contain integers") from exc
    if not values or len(values) > 32 or any(value < 1 or value > 2_147_483_647 for value in values):
        raise RuntimeError("FORTIGATE_POLICY_IDS contains an invalid policy ID")
    return values


@dataclass(frozen=True)
class Settings:
    webhook_token: str
    webhook_path: str
    fortigate_host: str
    fortigate_port: int
    fortigate_user: str
    fortigate_password: str
    known_hosts_file: Path
    policy_ids: tuple[int, ...]
    disable_delay_seconds: int
    inactivity_seconds: int
    command_timeout_seconds: int
    rate_limit_per_minute: int

    @classmethod
    def from_environment(cls) -> Settings:
        webhook_id = required_env("WEBHOOK_UUID", maximum_length=36)
        try:
            parsed_id = uuid.UUID(webhook_id)
        except ValueError as exc:
            raise RuntimeError("WEBHOOK_UUID must be a UUID") from exc
        if parsed_id.version != 4 or str(parsed_id) != webhook_id.lower():
            raise RuntimeError("WEBHOOK_UUID must be a canonical UUIDv4")

        host = required_env("FORTIGATE_HOST")
        user = required_env("FORTIGATE_USER", maximum_length=64)
        if not HOST_RE.fullmatch(host):
            raise RuntimeError("FORTIGATE_HOST is invalid")
        if not USER_RE.fullmatch(user):
            raise RuntimeError("FORTIGATE_USER is invalid")
        port = integer_env("FORTIGATE_PORT", 22, minimum=1, maximum=65535)

        known_hosts = Path(required_env("FORTIGATE_KNOWN_HOSTS_FILE", maximum_length=4096))
        try:
            if not known_hosts.is_file() or known_hosts.stat().st_size == 0:
                raise RuntimeError("FORTIGATE_KNOWN_HOSTS_FILE must be a non-empty file")
        except OSError as exc:
            raise RuntimeError("unable to inspect FORTIGATE_KNOWN_HOSTS_FILE") from exc
        host_key_name = host if port == 22 else f"[{host}]:{port}"
        try:
            known_host_keys = paramiko.HostKeys(str(known_hosts))
        except (OSError, paramiko.SSHException) as exc:
            raise RuntimeError("FORTIGATE_KNOWN_HOSTS_FILE is invalid") from exc
        if known_host_keys.lookup(host_key_name) is None:
            raise RuntimeError("FORTIGATE_KNOWN_HOSTS_FILE does not pin the configured host and port")

        return cls(
            webhook_token=read_secret("WEBHOOK_TOKEN", minimum_length=32),
            webhook_path=f"/webhook/{parsed_id}",
            fortigate_host=host,
            fortigate_port=port,
            fortigate_user=user,
            fortigate_password=read_secret("FORTIGATE_PASSWORD", minimum_length=16),
            known_hosts_file=known_hosts,
            policy_ids=policy_ids_env(),
            disable_delay_seconds=integer_env(
                "DISABLE_DELAY_SECONDS", 120, minimum=1, maximum=3600
            ),
            inactivity_seconds=integer_env(
                "INACTIVITY_SECONDS", 7200, minimum=60, maximum=604800
            ),
            command_timeout_seconds=integer_env(
                "SSH_COMMAND_TIMEOUT_SECONDS", 15, minimum=1, maximum=60
            ),
            rate_limit_per_minute=integer_env(
                "WEBHOOK_RATE_LIMIT_PER_MINUTE", 60, minimum=1, maximum=1000
            ),
        )
