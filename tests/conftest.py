from pathlib import Path

import pytest

from config import Settings


@pytest.fixture
def settings():
    return Settings(
        webhook_token="test-webhook-token-that-is-long-enough",
        webhook_path="/webhook/123e4567-e89b-42d3-a456-426614174000",
        fortigate_host="fortigate.example.com",
        fortigate_port=22,
        fortigate_user="shaper-automation",
        fortigate_password="test-device-password",
        known_hosts_file=Path("tests/fixtures/known_hosts"),
        policy_ids=(17, 21, 22),
        disable_delay_seconds=120,
        inactivity_seconds=7200,
        command_timeout_seconds=15,
        rate_limit_per_minute=60,
    )
