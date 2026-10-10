import importlib
import io
import os
import unittest
from unittest.mock import patch

with patch.dict(os.environ, WEBHOOK_TOKEN="test-token", WEBHOOK_UUID="test"):
    with patch("threading.Thread.start"):
        webhook = importlib.import_module("app")


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.client = webhook.app.test_client()

    def test_authenticated_and_rejected_responses_are_not_cached(self):
        for headers, status in [({}, 401), ({"X-Webhook-Token": "test-token"}, 200)]:
            with self.subTest(status=status):
                response = self.client.post("/webhook/test", json={"event": "unknown"}, headers=headers)
                self.assertEqual(response.status_code, status)
                self.assertTrue(response.cache_control.private)
                self.assertTrue(response.cache_control.no_store)

    def test_playback_start_executes_ssh_command(self):
        with patch.object(webhook.paramiko, "SSHClient", autospec=True) as client:
            client.return_value.exec_command.return_value = (None, io.BytesIO(b"ok"), io.BytesIO())
            response = self.client.post("/webhook/test", json={"event": "playback_start"}, headers={"X-Webhook-Token": "test-token"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["output"], "ok")
            client.return_value.connect.assert_called_once()
            client.return_value.exec_command.assert_called_once()
            client.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
