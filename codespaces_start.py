import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

root = Path(__file__).resolve().parent
origin = f"https://{os.environ['CODESPACE_NAME']}-8765.{os.environ['GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN']}"
data = root / '.clipbot-data'
data.mkdir(mode=0o700, exist_ok=True)

def listening():
    try:
        with socket.create_connection(('127.0.0.1', 8765), timeout=1):
            return True
    except OSError:
        return False

if not listening():
    env = dict(os.environ, CLIPBOT_PUBLIC_ORIGIN=origin, CLIPBOT_HOME=str(data), PYTHONDONTWRITEBYTECODE='1')
    with (data / 'server.log').open('ab') as log:
        process = subprocess.Popen([sys.executable, 'clipbot.py', 'serve'], cwd=root, env=env,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    for _ in range(30):
        if listening():
            break
        if process.poll() is not None:
            raise SystemExit('ClipBot exited. See .clipbot-data/server.log')
        time.sleep(0.2)
    else:
        raise SystemExit('ClipBot did not start. See .clipbot-data/server.log')
print(origin)
