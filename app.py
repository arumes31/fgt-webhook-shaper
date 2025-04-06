from flask import Flask, request, jsonify
import paramiko
import os
import logging
from datetime import datetime, timedelta
import threading
import time

app = Flask(__name__)

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

# Environment variables (unchanged)
WEBHOOK_TOKEN = os.environ.get('WEBHOOK_TOKEN')
WEBHOOK_UUID = os.environ.get('WEBHOOK_UUID')
WEBHOOK_PATH = f"/webhook/{WEBHOOK_UUID}" if WEBHOOK_UUID else "/webhook/default"

if not WEBHOOK_TOKEN:
    logger.error("WEBHOOK_TOKEN environment variable is required")
    raise ValueError("WEBHOOK_TOKEN environment variable is required")
if not WEBHOOK_UUID:
    logger.warning("WEBHOOK_UUID not provided, using default path")

FORTIGATE_HOST = os.environ.get('FORTIGATE_HOST', 'fortigate.example.com')
FORTIGATE_USER = os.environ.get('FORTIGATE_USER', 'admin')
FORTIGATE_PASSWORD = os.environ.get('FORTIGATE_PASS', 'password')
FORTIGATE_PORT = int(os.environ.get('FORTIGATE_PORT', 22))

# Global variables
last_event_time = datetime.now()
resume_event = threading.Event()  # To signal resume has occurred

def ssh_execute_command(command):
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        logger.info(f"Connecting to Fortigate at {FORTIGATE_HOST}:{FORTIGATE_PORT}")
        ssh.connect(
            FORTIGATE_HOST,
            port=FORTIGATE_PORT,
            username=FORTIGATE_USER,
            password=FORTIGATE_PASSWORD,
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

def delayed_disable(event, wan_streams):
    """Handle delayed disable with instant cancellation on resume"""
    logger.info(f"Starting 2-minute delay for {event} with wan_streams={wan_streams}")
    resume_event.clear()  # Reset the resume flag
    delay_seconds = 120  # 2 minutes
    elapsed = 0

    # Check every second to allow instant cancellation
    while elapsed < delay_seconds:
        if resume_event.is_set():
            logger.info(f"{event} cancelled due to playback_resume")
            return
        time.sleep(1)  # Wait 1 second at a time
        elapsed += 1

    # If we reach here, no resume occurred within 2 minutes
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
    if not auth_token or auth_token != WEBHOOK_TOKEN:
        logger.warning(f"Unauthorized access attempt to {WEBHOOK_PATH}")
        return jsonify({'error': 'Unauthorized'}), 401

    request_data = request.get_json(silent=True)
    x_forwarded_for = request.headers.get('X-Forwarded-For', 'Not provided')
    logger.info(f"Incoming webhook request JSON: {request_data}, X-Forwarded-For: {x_forwarded_for}")
    
    if not request_data or 'event' not in request_data:
        logger.warning("Invalid request received: missing event or invalid JSON")
        return jsonify({'error': 'Invalid request'}), 400

    event = request_data['event'].lower()
    wan_streams = request_data.get('wan_streams', 0)
    try:
        wan_streams = int(wan_streams)
    except (ValueError, TypeError):
        logger.warning(f"Invalid wan_streams value: {wan_streams}, treating as 0")
        wan_streams = 0

    last_event_time = datetime.now()

    if event in ['playback_start', 'playback_resume']:
        logger.info(f"Processing enable event: {event}")
        resume_event.set()  # Signal that a resume has occurred, cancelling any delayed disable
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
            return jsonify({'message': f'Event {event} executed successfully', 'output': response['output']}), 200
        else:
            return jsonify({'error': response['error']}), 500

    elif event == 'playback_pause':
        if wan_streams > 1:
            logger.info(f"Skipping disable for {event} - wan_streams active: {wan_streams}")
            return jsonify({'message': f'Event {event} ignored due to active wan_streams: {wan_streams}'}), 200
        else:
            logger.info(f"Triggering delayed disable for {event} with wan_streams={wan_streams}")
            threading.Thread(target=delayed_disable, args=(event, wan_streams), daemon=True).start()
            return jsonify({'message': f'Event {event} scheduled for disable after 2-minute delay'}), 200

    elif event == 'playback_stop':
        if wan_streams >= 1:
            logger.info(f"Skipping disable for {event} - wan_streams active: {wan_streams}")
            return jsonify({'message': f'Event {event} ignored due to active wan_streams: {wan_streams}'}), 200
        else:
            logger.info(f"Triggering delayed disable for {event} with wan_streams={wan_streams}")
            threading.Thread(target=delayed_disable, args=(event, wan_streams), daemon=True).start()
            return jsonify({'message': f'Event {event} scheduled for disable after 2-minute delay'}), 200

    else:
        logger.info(f"Ignoring unhandled event: {event}")
        return jsonify({'message': f'Event {event} ignored'}), 200

# Start inactivity checker (unchanged)
inactivity_thread = threading.Thread(target=check_inactivity, daemon=True)
inactivity_thread.start()
logger.info("Inactivity checker thread started")

if __name__ == '__main__':
    logger.info("Starting webhook server...")
    logger.info(f"Webhook endpoint: {WEBHOOK_PATH}")
    logger.info(f"Webhook token: {WEBHOOK_TOKEN}")