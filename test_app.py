import importlib
import io
import os
import unittest
from unittest.mock import patch

import paramiko

ENV = {
    "WEBHOOK_TOKEN": "test-token",
    "WEBHOOK_UUID": "test",
    "FORTIGATE_HOST": "fortigate.test",
    "FORTIGATE_USER": "shaper",
    "FORTIGATE_PASS": "secret",
    "FORTIGATE_HOSTKEY": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFIe/cPO+W4/3cve2Bad174XMQyOuz57wr+tks3AhQYd",
}

with patch.dict(os.environ, ENV, clear=True):
    with patch("threading.Thread.start"):
        webhook = importlib.import_module("app")

AUTH = {"X-Webhook-Token": "test-token"}


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.client = webhook.app.test_client()

    def tearDown(self):
        webhook.cancel_delayed_disable()

    def test_authenticated_and_rejected_responses_are_not_cached(self):
        for headers, status in [({}, 401), ({"X-Webhook-Token": "wrong"}, 401), (AUTH, 200)]:
            with self.subTest(status=status, headers=headers):
                response = self.client.post("/webhook/test", json={"event": "unknown"}, headers=headers)
                self.assertEqual(response.status_code, status)
                self.assertTrue(response.cache_control.private)
                self.assertTrue(response.cache_control.no_store)

    def test_playback_start_executes_ssh_command_with_pinned_host_key(self):
        with patch.object(webhook.paramiko, "SSHClient", autospec=True) as client:
            ssh = client.return_value
            ssh.get_host_keys.return_value = paramiko.HostKeys()
            ssh.exec_command.return_value = (None, io.BytesIO(b"ok"), io.BytesIO())
            response = self.client.post("/webhook/test", json={"event": "playback_start"}, headers=AUTH)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("output", response.json)
            policy = ssh.set_missing_host_key_policy.call_args.args[0]
            self.assertIsInstance(policy, paramiko.RejectPolicy)
            self.assertIn("fortigate.test", ssh.get_host_keys.return_value)
            ssh.connect.assert_called_once()
            ssh.exec_command.assert_called_once()
            ssh.close.assert_called_once()

    def test_ssh_failure_returns_generic_error(self):
        with patch.object(webhook.paramiko, "SSHClient", autospec=True) as client:
            client.return_value.get_host_keys.return_value = paramiko.HostKeys()
            client.return_value.connect.side_effect = paramiko.SSHException("host key mismatch details")
            response = self.client.post("/webhook/test", json={"event": "playback_start"}, headers=AUTH)
            self.assertEqual(response.status_code, 500)
            self.assertNotIn("mismatch", response.get_data(as_text=True))

    def test_invalid_event_payload_is_rejected(self):
        for payload in [None, [], {"event": 5}]:
            with self.subTest(payload=payload):
                response = self.client.post("/webhook/test", json=payload, headers=AUTH)
                self.assertEqual(response.status_code, 400)

    def test_pause_events_share_a_single_cancellable_timer(self):
        with patch.object(webhook.threading, "Timer", autospec=True) as timer:
            for _ in range(3):
                response = self.client.post("/webhook/test", json={"event": "playback_pause"}, headers=AUTH)
                self.assertEqual(response.status_code, 200)
            self.assertEqual(timer.call_count, 3)
            self.assertEqual(timer.return_value.cancel.call_count, 2)
            self.assertIs(webhook.disable_timer, timer.return_value)

            with patch.object(webhook, "ssh_execute_command", return_value={"success": True}):
                self.client.post("/webhook/test", json={"event": "playback_resume"}, headers=AUTH)
            self.assertEqual(timer.return_value.cancel.call_count, 3)
            self.assertIsNone(webhook.disable_timer)

    def test_superseded_timer_does_not_disable(self):
        with patch.object(webhook, "ssh_execute_command") as ssh_execute:
            webhook.delayed_disable("playback_stop", 0)
            ssh_execute.assert_not_called()


class ConfigTests(unittest.TestCase):
    def test_missing_fortigate_settings_fail_closed(self):
        for name in ["FORTIGATE_HOST", "FORTIGATE_USER", "FORTIGATE_PASS", "FORTIGATE_HOSTKEY"]:
            env = {k: v for k, v in ENV.items() if k != name}
            with self.subTest(missing=name), patch.dict(os.environ, env, clear=True):
                with self.assertRaises(ValueError):
                    with patch("threading.Thread.start"):
                        importlib.reload(webhook)
        with patch.dict(os.environ, {**ENV, "FORTIGATE_HOSTKEY": "ssh-ed25519 not-a-key"}, clear=True):
            with self.assertRaises(ValueError), patch("threading.Thread.start"):
                importlib.reload(webhook)
        with patch.dict(os.environ, ENV, clear=True), patch("threading.Thread.start"):
            importlib.reload(webhook)


if __name__ == "__main__":
    unittest.main()
