import os

import paramiko
import pytest

from config import Settings, integer_env, policy_ids_env, read_secret, required_env

RELEVANT_ENV = {
    "WEBHOOK_TOKEN",
    "WEBHOOK_TOKEN_FILE",
    "WEBHOOK_UUID",
    "FORTIGATE_HOST",
    "FORTIGATE_PORT",
    "FORTIGATE_USER",
    "FORTIGATE_PASSWORD",
    "FORTIGATE_PASSWORD_FILE",
    "FORTIGATE_KNOWN_HOSTS_FILE",
    "FORTIGATE_POLICY_IDS",
}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in RELEVANT_ENV:
        monkeypatch.delenv(name, raising=False)


def set_required_environment(monkeypatch):
    values = {
        "WEBHOOK_TOKEN": "a-secure-webhook-token-with-32-characters",
        "WEBHOOK_UUID": "123e4567-e89b-42d3-a456-426614174000",
        "FORTIGATE_HOST": "fortigate.example.com",
        "FORTIGATE_USER": "shaper-automation",
        "FORTIGATE_PASSWORD": "a-secure-device-password",
        "FORTIGATE_KNOWN_HOSTS_FILE": "tests/fixtures/known_hosts",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_settings_fail_closed_without_required_values():
    with pytest.raises(RuntimeError, match="WEBHOOK_UUID is required"):
        Settings.from_environment()


def test_settings_load_validated_values(monkeypatch):
    set_required_environment(monkeypatch)
    monkeypatch.setenv("FORTIGATE_POLICY_IDS", "17,21,21,22")

    result = Settings.from_environment()

    assert result.webhook_path.endswith("123e4567-e89b-42d3-a456-426614174000")
    assert result.policy_ids == (17, 21, 22)


@pytest.mark.parametrize("value", ["password", "short"])
def test_secret_rejects_known_or_short_values(monkeypatch, value):
    monkeypatch.setenv("TEST_SECRET", value)

    with pytest.raises(RuntimeError, match="too weak"):
        read_secret("TEST_SECRET")


def test_secret_rejects_ambiguous_sources(monkeypatch):
    monkeypatch.setenv("TEST_SECRET", "a-long-enough-direct-secret")
    monkeypatch.setenv("TEST_SECRET_FILE", "tests/fixtures/known_hosts")

    with pytest.raises(RuntimeError, match="only one"):
        read_secret("TEST_SECRET")


def test_secret_reads_file_source(monkeypatch):
    monkeypatch.setenv("TEST_SECRET_FILE", "tests/fixtures/known_hosts")

    assert read_secret("TEST_SECRET").startswith("fortigate.example.com ssh-rsa ")


def test_secret_file_read_failure_is_generic(monkeypatch):
    monkeypatch.setenv("TEST_SECRET_FILE", "tests/fixtures/missing")

    with pytest.raises(RuntimeError, match="unable to read TEST_SECRET_FILE"):
        read_secret("TEST_SECRET")


def test_missing_secret_is_rejected():
    with pytest.raises(RuntimeError, match="is required"):
        read_secret("TEST_SECRET")


def test_required_environment_rejects_excessive_length(monkeypatch):
    monkeypatch.setenv("TEST_VALUE", "x" * 4)

    with pytest.raises(RuntimeError, match="too long"):
        required_env("TEST_VALUE", maximum_length=3)


@pytest.mark.parametrize("value", ["not-an-int", "0", "11"])
def test_integer_environment_is_validated(monkeypatch, value):
    monkeypatch.setenv("TEST_INTEGER", value)

    with pytest.raises(RuntimeError, match="must be"):
        integer_env("TEST_INTEGER", 5, minimum=1, maximum=10)


def test_policy_ids_reject_injection(monkeypatch):
    monkeypatch.setenv("FORTIGATE_POLICY_IDS", "17\nend")

    with pytest.raises(RuntimeError, match="integers"):
        policy_ids_env()


@pytest.mark.parametrize("value", ["", "0", "2147483648"])
def test_policy_ids_reject_empty_or_out_of_range_values(monkeypatch, value):
    monkeypatch.setenv("FORTIGATE_POLICY_IDS", value)

    with pytest.raises(RuntimeError, match="integers|invalid policy ID"):
        policy_ids_env()


def test_uuid_must_be_canonical_v4(monkeypatch):
    set_required_environment(monkeypatch)
    monkeypatch.setenv("WEBHOOK_UUID", "123e4567-e89b-12d3-a456-426614174000")

    with pytest.raises(RuntimeError, match="UUIDv4"):
        Settings.from_environment()


def test_uuid_must_parse(monkeypatch):
    set_required_environment(monkeypatch)
    monkeypatch.setenv("WEBHOOK_UUID", "not-a-uuid")

    with pytest.raises(RuntimeError, match="must be a UUID"):
        Settings.from_environment()


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("FORTIGATE_HOST", "bad host!", "FORTIGATE_HOST is invalid"),
        ("FORTIGATE_USER", "bad user!", "FORTIGATE_USER is invalid"),
    ],
)
def test_device_identity_is_strictly_validated(monkeypatch, name, value, message):
    set_required_environment(monkeypatch)
    monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError, match=message):
        Settings.from_environment()


def test_known_hosts_file_must_exist(monkeypatch):
    set_required_environment(monkeypatch)
    monkeypatch.setenv("FORTIGATE_KNOWN_HOSTS_FILE", "tests/fixtures/missing")

    with pytest.raises(RuntimeError, match="non-empty file"):
        Settings.from_environment()


def test_known_hosts_file_must_parse(monkeypatch):
    set_required_environment(monkeypatch)
    monkeypatch.setattr(
        "config.paramiko.HostKeys",
        lambda _: (_ for _ in ()).throw(paramiko.SSHException("malformed")),
    )

    with pytest.raises(RuntimeError, match="is invalid"):
        Settings.from_environment()


def test_known_hosts_file_must_pin_configured_endpoint(monkeypatch):
    set_required_environment(monkeypatch)
    monkeypatch.setenv("FORTIGATE_HOST", "other.example.com")

    with pytest.raises(RuntimeError, match="does not pin"):
        Settings.from_environment()


def test_environment_is_not_mutated_by_settings(monkeypatch):
    set_required_environment(monkeypatch)
    before = dict(os.environ)

    Settings.from_environment()

    assert dict(os.environ) == before
