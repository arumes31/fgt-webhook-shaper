import logging

import pytest

from app import FixedWindowRateLimit, create_app
from service import CommandError


@pytest.fixture
def calls():
    return []


@pytest.fixture
def client(settings, calls):
    application = create_app(settings, set_enabled=calls.append, start_worker=False)
    application.config.update(TESTING=True)
    return application.test_client()


def auth(settings):
    return {"X-Webhook-Token": settings.webhook_token}


def test_health_has_no_operational_details(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json == {"status": "ok"}
    assert response.headers["Cache-Control"] == "no-store"


def test_unauthorized_token_is_not_logged(client, settings, caplog):
    secret = "attacker-token-that-must-not-be-logged"

    with caplog.at_level(logging.WARNING):
        response = client.post(
            settings.webhook_path,
            headers={"X-Webhook-Token": secret},
            json={"event": "playback_start"},
        )

    assert response.status_code == 401
    assert secret not in caplog.text


def test_start_executes_enable_without_returning_device_output(client, settings, calls):
    response = client.post(
        settings.webhook_path,
        headers=auth(settings),
        json={"event": "playback_start", "wan_streams": 0},
    )

    assert response.status_code == 200
    assert response.json == {"event": "playback_start", "status": "executed"}
    assert calls == [True]


def test_stop_schedules_single_bounded_worker_action(client, settings, calls):
    response = client.post(
        settings.webhook_path,
        headers=auth(settings),
        json={"event": "playback_stop"},
    )

    assert response.status_code == 202
    assert response.json["status"] == "scheduled"
    assert calls == []


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"event": "unknown"}, 400),
        ({"event": "playback_start", "extra": "value"}, 400),
        ({"event": "playback_start", "wan_streams": True}, 400),
        ({"event": "playback_start", "wan_streams": -1}, 400),
        ({"event": "playback_start", "wan_streams": 10001}, 400),
    ],
)
def test_invalid_payloads_fail_closed(client, settings, body, expected):
    response = client.post(settings.webhook_path, headers=auth(settings), json=body)

    assert response.status_code == expected


def test_non_json_body_is_rejected(client, settings):
    response = client.post(
        settings.webhook_path,
        headers={**auth(settings), "Content-Type": "text/plain"},
        data="playback_start",
    )

    assert response.status_code == 415


def test_malformed_json_is_rejected(client, settings):
    response = client.post(
        settings.webhook_path,
        headers={**auth(settings), "Content-Type": "application/json"},
        data="{",
    )

    assert response.status_code == 400


def test_non_numeric_wan_streams_is_rejected(client, settings):
    response = client.post(
        settings.webhook_path,
        headers=auth(settings),
        json={"event": "playback_start", "wan_streams": "many"},
    )

    assert response.status_code == 400


def test_request_body_limit_is_enforced(client, settings):
    response = client.post(
        settings.webhook_path,
        headers={**auth(settings), "Content-Type": "application/json"},
        data='{"event":"playback_start","padding":"' + "x" * 17000 + '"}',
    )

    assert response.status_code == 413
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_device_error_returns_only_generic_message(settings):
    def fail(_):
        raise CommandError("sensitive device response")

    application = create_app(settings, set_enabled=fail, start_worker=False)
    application.config.update(TESTING=True)
    client = application.test_client()

    response = client.post(
        settings.webhook_path,
        headers=auth(settings),
        json={"event": "playback_start"},
    )

    assert response.status_code == 502
    assert response.json == {"error": "device operation failed"}
    assert "sensitive" not in response.text


def test_rate_limit_is_enforced(settings):
    limited = settings.__class__(**{**settings.__dict__, "rate_limit_per_minute": 1})
    application = create_app(limited, set_enabled=lambda _: None, start_worker=False)
    application.config.update(TESTING=True)
    client = application.test_client()

    first = client.post(
        settings.webhook_path,
        headers=auth(settings),
        json={"event": "playback_start"},
    )
    second = client.post(
        settings.webhook_path,
        headers=auth(settings),
        json={"event": "playback_start"},
    )

    assert first.status_code == 200
    assert second.status_code == 429


def test_rate_limiter_expires_old_entries():
    now = [100.0]
    limiter = FixedWindowRateLimit(1, clock=lambda: now[0])

    assert limiter.allow() is True
    assert limiter.allow() is False
    now[0] += 60
    assert limiter.allow() is True


def test_production_app_starts_scheduler_and_registers_cleanup(settings):
    with (
        pytest.MonkeyPatch.context() as monkeypatch,
    ):
        started = []
        registered = []
        monkeypatch.setattr("app.ShapingController.start", lambda self: started.append(self))
        monkeypatch.setattr("app.atexit.register", registered.append)

        application = create_app(settings, set_enabled=lambda _: None, start_worker=True)

    assert started == [application.extensions["shaping_controller"]]
    assert registered == [application.extensions["shaping_controller"].close]
    response = application.test_client().get("/healthz")
    assert response.status_code == 503
