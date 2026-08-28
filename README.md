# FortiGate webhook shaper

[![CI](https://github.com/arumes31/fgt-webhook-shaper/actions/workflows/ci.yml/badge.svg)](https://github.com/arumes31/fgt-webhook-shaper/actions/workflows/ci.yml)
[![Container](https://github.com/arumes31/fgt-webhook-shaper/actions/workflows/container.yml/badge.svg)](https://github.com/arumes31/fgt-webhook-shaper/actions/workflows/container.yml)

This service accepts a narrowly defined media-playback webhook and enables or disables selected FortiGate shaping policies over SSH. It deliberately supports one Gunicorn worker because one in-memory scheduler serializes device changes and owns delayed/inactivity actions.

## Security model

- the webhook token, UUIDv4 path, FortiGate host/user/password, and pinned SSH host-key file are required; there are no production credential defaults
- secrets can be supplied as environment variables or `_FILE` paths; Compose uses secret files
- webhook tokens are compared in constant time and never logged
- request bodies are limited to 16 KiB, must be JSON, accept only known fields/events, and are globally rate-limited after authentication
- request JSON, forwarded headers, FortiGate output, passwords, and detailed SSH failures are never logged or returned
- SSH rejects unknown or changed host keys, disables agent/key discovery, disables legacy `ssh-rsa`, and applies connect/auth/banner/channel/command timeouts
- policy IDs are parsed as bounded integers and webhook input is never interpolated into device commands
- a single condition-based scheduler replaces request-created delay threads and serializes enable/disable operations to preserve final state
- the container runs as UID/GID 65532 with a read-only root, all capabilities dropped, resource limits, and no runtime package manager

The UUID path is defense in depth, not a substitute for the token. Expose the service only behind HTTPS and an authenticated or allowlisted reverse proxy. Add network egress policy so the container can reach only the FortiGate SSH address and required DNS infrastructure.

## Required migration

Do this before deploying the updated image:

1. Preserve restricted copies of existing application/proxy logs for incident review. Determine where the logged webhook token was retained or exported.
2. Rotate the webhook token and FortiGate automation password. Treat the former source defaults and any logged values as exposed.
3. Create a dedicated least-privilege FortiGate automation account that can change only the required shaping policies. Do not use `admin`.
4. Obtain the FortiGate host-key fingerprint over an authenticated, independent channel. Export the matching OpenSSH `known_hosts` entry and verify it before installation; never accept an unverified `ssh-keyscan` result.
5. Generate a new UUIDv4 webhook path and update the sender only after the service is ready.
6. Confirm the FortiGate supports modern RSA-SHA2, ECDSA, or Ed25519 SSH host keys/signatures. Legacy SHA-1 `ssh-rsa` is rejected.
7. Test start/resume/pause/stop events against a maintenance device or window, then remove the old endpoint and credentials.

## Deployment

Create local, untracked secret/config files:

```bash
install -d -m 700 secrets config
openssl rand -base64 48 > secrets/webhook_token
printf '%s\n' 'rotated-fortigate-password' > secrets/fortigate_password
chmod 600 secrets/* config/fortigate_known_hosts
cp .env.example .env
# Set a real UUIDv4 and non-secret device settings in .env.
```

Place the independently verified host entry in `config/fortigate_known_hosts`. For port 22 it looks like:

```text
fortigate.example.com ssh-ed25519 AAAA...
```

For a nonstandard port, OpenSSH uses `[fortigate.example.com]:2222` as the host field.

Validate and start:

```bash
docker compose config --quiet
docker compose build
docker compose up -d
docker compose ps
```

The default bind is `127.0.0.1:25001`; terminate TLS in a reverse proxy. Send webhooks to:

```text
POST /webhook/<WEBHOOK_UUID>
Content-Type: application/json
X-Webhook-Token: <WEBHOOK_TOKEN>

{"event":"playback_start","wan_streams":0}
```

Supported events are `playback_start`, `playback_resume`, `playback_pause`, and `playback_stop`. Pause is ignored with two or more active WAN streams; stop is ignored with one or more. Accepted events reset the inactivity timer.

## Host-key rotation and rollback

A changed key fails closed. Investigate a key mismatch as a possible interception event. If the device was intentionally reprovisioned, verify the new fingerprint independently and atomically replace the `known_hosts` file before restarting.

Before upgrades, retain the previous image digest and non-secret `.env`. Rollback must not restore old webhook/device credentials or a superseded host key. The new service has no persistent application data.

## Development and verification

Python 3.14 is supported. Runtime and development dependency graphs are exact and hash-locked.

```bash
uv venv
uv pip install --python .venv/bin/python --require-hashes -r requirements-dev.txt
.venv/bin/ruff check .
.venv/bin/pytest --cov --cov-report=term-missing --cov-fail-under=90
.venv/bin/bandit -c pyproject.toml -r app.py config.py service.py -ll -ii
.venv/bin/pip-audit -r requirements.txt
```

Refresh locks with `uv pip compile --python-version 3.14 --generate-hashes` only after reviewing upstream changes.

## Operational checks

- `/healthz` returns only process/scheduler liveness and no device or credential details
- alert on repeated `401`, `429`, `502`, container restarts, and host-key/authentication failures
- periodically reconcile actual policy status on the FortiGate; this service does not treat local state as authoritative
- review and rotate automation credentials, verify the pinned host key, and exercise rollback at least quarterly
