from __future__ import annotations

import atexit
import hmac
import logging
import threading
import time
from collections import deque

from flask import Flask, jsonify, request

from config import Settings
from service import CommandError, FortiGateSSH, ShapingController

logger = logging.getLogger(__name__)
ALLOWED_EVENTS = {
    "playback_pause",
    "playback_resume",
    "playback_start",
    "playback_stop",
}


class FixedWindowRateLimit:
    def __init__(self, limit: int, *, clock=time.monotonic):
        self.limit = limit
        self.clock = clock
        self.events: deque[float] = deque()
        self.lock = threading.Lock()

    def allow(self) -> bool:
        now = self.clock()
        with self.lock:
            while self.events and self.events[0] <= now - 60:
                self.events.popleft()
            if len(self.events) >= self.limit:
                return False
            self.events.append(now)
            return True


def create_app(
    settings: Settings | None = None,
    *,
    set_enabled=None,
    start_worker: bool = True,
) -> Flask:
    settings = settings or Settings.from_environment()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    application = Flask(__name__)
    application.config.update(MAX_CONTENT_LENGTH=16 * 1024)
    executor = set_enabled or FortiGateSSH(settings).set_enabled
    controller = ShapingController(
        executor,
        disable_delay_seconds=settings.disable_delay_seconds,
        inactivity_seconds=settings.inactivity_seconds,
    )
    limiter = FixedWindowRateLimit(settings.rate_limit_per_minute)
    application.extensions["shaping_controller"] = controller
    if start_worker:
        controller.start()
        atexit.register(controller.close)

    @application.after_request
    def security_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @application.get("/healthz")
    def health():
        healthy = not start_worker or controller.worker_alive()
        return jsonify({"status": "ok" if healthy else "unhealthy"}), 200 if healthy else 503

    @application.post(settings.webhook_path)
    def webhook():
        supplied = request.headers.get("X-Webhook-Token", "")
        if not hmac.compare_digest(supplied.encode(), settings.webhook_token.encode()):
            logger.warning("Rejected webhook authentication")
            return jsonify({"error": "unauthorized"}), 401
        if not limiter.allow():
            logger.warning("Webhook rate limit exceeded")
            return jsonify({"error": "rate limit exceeded"}), 429
        if not request.is_json:
            return jsonify({"error": "application/json required"}), 415
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) - {"event", "wan_streams"}:
            return jsonify({"error": "invalid request"}), 400
        event = payload.get("event")
        if not isinstance(event, str) or event not in ALLOWED_EVENTS:
            return jsonify({"error": "invalid event"}), 400
        wan_streams = payload.get("wan_streams", 0)
        if isinstance(wan_streams, bool):
            return jsonify({"error": "invalid wan_streams"}), 400
        try:
            wan_streams = int(wan_streams)
        except (TypeError, ValueError):
            return jsonify({"error": "invalid wan_streams"}), 400
        if not 0 <= wan_streams <= 10_000:
            return jsonify({"error": "invalid wan_streams"}), 400

        logger.info("Processing authenticated event=%s wan_streams=%d", event, wan_streams)
        try:
            result = controller.handle(event, wan_streams)
        except CommandError:
            return jsonify({"error": "device operation failed"}), 502
        status_code = 202 if result.status == "scheduled" else 200
        return jsonify({"event": event, "status": result.status}), status_code

    return application


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=25001)
