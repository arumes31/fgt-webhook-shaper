from flask import Flask, request, jsonify
import paramiko
import base64
import hmac
import os
import logging
from datetime import datetime, timedelta
import threading
import time

app = Flask(__name__)


@app.after_request
def prevent_response_caching(response):
    response.cache_control.private = True
    response.cache_control.no_store = True
    return response

# Custom logging formatter (unchanged)
class ColoredFormatter(logging.Formatter):
    COLORS = {
        'INFO': '\033[92m',    # Green
        'WARNING': '\033[93m', # Yellow
        'ERROR': '\033[91m',   # Red
        'RESET': '\033[0m'     # Reset color
    }
    EVENT_COLORS = {
        'playback_start': '\033[38;5;117m',  # Blue
        'playback_resume': '\033[96m',       # Cyan
        'playback_pause': '\033[95m',        # Magenta
        'playback_stop': '\033[38;5;208m'    # Orange
    }

    def format(self, record):
        log_message = super().format(record)
        for event, color in self.EVENT_COLORS.items():
            if event in log_message.lower():
                return f"{color}{log_message}{self.COLORS['RESET']}"
        return f"{self.COLORS.get(record.levelname, self.COLORS['RESET'])}{log_message}{self.COLORS['RESET']}"

# Logging setup (unchanged)
handler = logging.StreamHandler()
handler.setFormatter(ColoredFormatter('%(asctime)s - %(levelname)s - %(message)s'))
logging.basicConfig(level=logging.INFO, handlers=[handler])
logger = logging.getLogger(__name__)

def get_env(name, default=None):
    """Read a setting from NAME or from the file referenced by NAME_FILE (Docker secrets)"""
    file_path = os.environ.get(f'{name}_FILE')
    if file_path:
        with open(file_path, encoding='utf-8') as f:
            return f.read().strip()
    return os.environ.get(name, default)

def require_env(name):
    value = get_env(name)
    if not value:
        logger.error(f"{name} environment variable is required")
        raise ValueError(f"{name} environment variable is required")
    return value

def sanitize(value, max_length=64):
    """Make untrusted input safe to log (no newlines/control characters, bounded length)"""
    return repr(str(value)[:max_length])

# Environment variables
WEBHOOK_TOKEN = require_env('WEBHOOK_TOKEN')
WEBHOOK_UUID = os.environ.get('WEBHOOK_UUID')
WEBHOOK_PATH = f"/webhook/{WEBHOOK_UUID}" if WEBHOOK_UUID else "/webhook/default"

if len(WEBHOOK_TOKEN) < 32:
    logger.warning("WEBHOOK_TOKEN is shorter than 32 characters, use a long random token")
if not WEBHOOK_UUID:
    logger.warning("WEBHOOK_UUID not provided, using default path")

# Fortigate connection settings have no defaults so a misconfiguration fails closed
FORTIGATE_HOST = require_env('FORTIGATE_HOST')
FORTIGATE_USER = require_env('FORTIGATE_USER')
FORTIGATE_KEY_FILE = get_env('FORTIGATE_KEY_FILE')  # Optional private key for key-based auth
FORTIGATE_PASSWORD = get_env('FORTIGATE_PASS') if FORTIGATE_KEY_FILE else require_env('FORTIGATE_PASS')
FORTIGATE_PORT = int(os.environ.get('FORTIGATE_PORT', 22))

# Host key pinning: a known_hosts file and/or a single "<key-type> <base64-key>" entry
FORTIGATE_KNOWN_HOSTS = get_env('FORTIGATE_KNOWN_HOSTS')
FORTIGATE_HOSTKEY = get_env('FORTIGATE_HOSTKEY')

if not FORTIGATE_KNOWN_HOSTS and not FORTIGATE_HOSTKEY:
    logger.error("FORTIGATE_KNOWN_HOSTS or FORTIGATE_HOSTKEY is required to verify the Fortigate host key")
    raise ValueError("FORTIGATE_KNOWN_HOSTS or FORTIGATE_HOSTKEY is required to verify the Fortigate host key")

def parse_host_key(value):
    """Parse "<key-type> <base64-key>" (optionally prefixed by a host like a known_hosts line)"""
    if not value:
        return None
    parts = value.split()
    for i in range(len(parts) - 1):
        try:
            return paramiko.PKey.from_type_string(parts[i], base64.b64decode(parts[i + 1]))
        except Exception:
            continue
    logger.error("FORTIGATE_HOSTKEY is not a valid '<key-type> <base64-key>' entry")
    raise ValueError("FORTIGATE_HOSTKEY is not a valid '<key-type> <base64-key>' entry")

FORTIGATE_PINNED_KEY = parse_host_key(FORTIGATE_HOSTKEY)

DISABLE_DELAY_SECONDS = 120  # 2 minutes

# Global variables
last_event_time = datetime.now()
disable_timer = None  # Pending delayed disable, replaced or cancelled on every event
disable_timer_lock = threading.Lock()

def configure_host_keys(ssh):
    """Pin the Fortigate host key and reject any unknown key"""
    if FORTIGATE_KNOWN_HOSTS:
        ssh.load_host_keys(FORTIGATE_KNOWN_HOSTS)
    if FORTIGATE_PINNED_KEY:
        host = FORTIGATE_HOST if FORTIGATE_PORT == 22 else f"[{FORTIGATE_HOST}]:{FORTIGATE_PORT}"
        ssh.get_host_keys().add(host, FORTIGATE_PINNED_KEY.get_name(), FORTIGATE_PINNED_KEY)
    ssh.set_missing_host_key_policy(paramiko.RejectPolicy())

def ssh_execute_command(command):
    try:
        ssh = paramiko.SSHClient()
        configure_host_keys(ssh)
        logger.info(f"Connecting to Fortigate at {FORTIGATE_HOST}:{FORTIGATE_PORT}")
        ssh.connect(
            FORTIGATE_HOST,
            port=FORTIGATE_PORT,
            username=FORTIGATE_USER,
            password=FORTIGATE_PASSWORD,
            key_filename=FORTIGATE_KEY_FILE,
            look_for_keys=False,
            allow_agent=False,
            timeout=10
        )
        logger.info(f"Executing command: {command.strip()}")
        stdin, stdout, stderr = ssh.exec_command(command)
        output = stdout.read().decode()
        error = stderr.read().decode()
        ssh.close()
        if output:
            logger.info(f"Command output: {output}")
        if error:
            logger.error(f"Command error: {error}")
        return {'success': True, 'output': output, 'error': error}
    except Exception as e:
        logger.error(f"SSH execution failed: {str(e)}")
        return {'success': False, 'error': str(e)}

def check_inactivity():
    global last_event_time
    while True:
        time.sleep(300)  # Check every 5 minutes
        current_time = datetime.now()
        elapsed_time = (current_time - last_event_time).total_seconds()
        logger.info(f"Inactivity check: {elapsed_time} seconds elapsed since last event")
        if elapsed_time >= 7200:  # 2 hours
            logger.info("No events for 2 hours, triggering playback_stop")
            command = """config firewall shaping-policy
edit 17
set status disable
next
edit 21
set status disable
next
edit 22
set status disable
next
end"""
            response = ssh_execute_command(command)
            if response.get('success'):
                logger.info("Inactivity timeout: shaping policy disabled")
            else:
                logger.error(f"Inactivity timeout failed: {response['error']}")
            last_event_time = datetime.now()

def cancel_delayed_disable():
    """Cancel the pending delayed disable, if any"""
    global disable_timer
    with disable_timer_lock:
        if disable_timer is not None:
            disable_timer.cancel()
            disable_timer = None
            logger.info("Pending delayed disable cancelled due to playback_resume")

def schedule_delayed_disable(event, wan_streams):
    """Start the single delayed disable timer, replacing any pending one"""
    global disable_timer
    with disable_timer_lock:
        if disable_timer is not None:
            disable_timer.cancel()
        logger.info(f"Starting 2-minute delay for {event} with wan_streams={wan_streams}")
        disable_timer = threading.Timer(DISABLE_DELAY_SECONDS, delayed_disable, args=(event, wan_streams))
        disable_timer.daemon = True
        disable_timer.start()

def delayed_disable(event, wan_streams):
    """Disable shaping once the delay expired without a resume"""
    global disable_timer
    with disable_timer_lock:
        if disable_timer is not threading.current_thread():
            return  # Cancelled or superseded by a newer timer
        disable_timer = None

    logger.info(f"Executing disable after 2-minute delay for {event}")
    command = """config firewall shaping-policy
edit 17
set status disable
next
edit 21
set status disable
next
edit 22
set status disable
next
end"""
    response = ssh_execute_command(command)
    if response.get('success'):
        logger.info(f"Delayed disable for {event} completed successfully")
    else:
        logger.error(f"Delayed disable for {event} failed: {response['error']}")

@app.route(WEBHOOK_PATH, methods=['POST'])
def webhook():
    global last_event_time

    auth_token = request.headers.get('X-Webhook-Token')
    if not auth_token or not hmac.compare_digest(auth_token.encode(), WEBHOOK_TOKEN.encode()):
        logger.warning(f"Unauthorized access attempt to {WEBHOOK_PATH}")
        return jsonify({'error': 'Unauthorized'}), 401

    request_data = request.get_json(silent=True)

    if not isinstance(request_data, dict) or not isinstance(request_data.get('event'), str):
        logger.warning("Invalid request received: missing event or invalid JSON")
        return jsonify({'error': 'Invalid request'}), 400

    event = request_data['event'].lower()
    logger.info(f"Incoming webhook event: {sanitize(event)}")
    wan_streams = request_data.get('wan_streams', 0)
    try:
        wan_streams = int(wan_streams)
    except (ValueError, TypeError):
        logger.warning(f"Invalid wan_streams value: {sanitize(wan_streams)}, treating as 0")
        wan_streams = 0

    last_event_time = datetime.now()

    if event in ['playback_start', 'playback_resume']:
        logger.info(f"Processing enable event: {event}")
        cancel_delayed_disable()
        command = """config firewall shaping-policy
edit 17
set status enable
next
edit 21
set status enable
next
edit 22
set status enable
next
end"""
        response = ssh_execute_command(command)
        if response.get('success'):
            return jsonify({'message': f'Event {event} executed successfully'}), 200
        else:
            return jsonify({'error': 'Failed to update Fortigate shaping policy'}), 500

    elif event == 'playback_pause':
        if wan_streams > 1:
            logger.info(f"Skipping disable for {event} - wan_streams active: {wan_streams}")
            return jsonify({'message': f'Event {event} ignored due to active wan_streams: {wan_streams}'}), 200
        else:
            logger.info(f"Triggering delayed disable for {event} with wan_streams={wan_streams}")
            schedule_delayed_disable(event, wan_streams)
            return jsonify({'message': f'Event {event} scheduled for disable after 2-minute delay'}), 200

    elif event == 'playback_stop':
        if wan_streams >= 1:
            logger.info(f"Skipping disable for {event} - wan_streams active: {wan_streams}")
            return jsonify({'message': f'Event {event} ignored due to active wan_streams: {wan_streams}'}), 200
        else:
            logger.info(f"Triggering delayed disable for {event} with wan_streams={wan_streams}")
            schedule_delayed_disable(event, wan_streams)
            return jsonify({'message': f'Event {event} scheduled for disable after 2-minute delay'}), 200

    else:
        logger.info(f"Ignoring unhandled event: {sanitize(event)}")
        return jsonify({'message': 'Event ignored'}), 200

# Start inactivity checker (unchanged)
inactivity_thread = threading.Thread(target=check_inactivity, daemon=True)
inactivity_thread.start()
logger.info("Inactivity checker thread started")

if __name__ == '__main__':
    logger.info("Starting webhook server...")
    logger.info(f"Webhook endpoint: {WEBHOOK_PATH}")
