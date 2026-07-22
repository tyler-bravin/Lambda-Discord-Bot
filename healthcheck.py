"""Docker healthcheck: passes while main.py keeps refreshing the heartbeat file.

main.py touches the heartbeat file every 30 seconds while the bot is connected
to Discord. If the file is missing or stale, the container is reported unhealthy.
"""
import os
import sys
import tempfile
import time

MAX_AGE_SECONDS = 90

path = os.getenv("HEARTBEAT_FILE", os.path.join(tempfile.gettempdir(), "lambda-bot-heartbeat"))
try:
    age = time.time() - os.path.getmtime(path)
except OSError:
    sys.exit(1)
sys.exit(0 if age < MAX_AGE_SECONDS else 1)
