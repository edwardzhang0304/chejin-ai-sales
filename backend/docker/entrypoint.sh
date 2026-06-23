#!/usr/bin/env sh
set -eu

if [ "${WAIT_FOR_DB:-1}" = "1" ]; then
  python - <<'PY'
import os
import socket
import time
from urllib.parse import urlparse

url = os.environ.get("DATABASE_URL", "")
parsed = urlparse(url)
host = parsed.hostname
port = parsed.port or (5432 if parsed.scheme.startswith("postgres") else None)

if host and port:
    deadline = time.time() + int(os.environ.get("DB_WAIT_TIMEOUT", "60"))
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                print(f"database reachable: {host}:{port}")
                break
        except OSError:
            time.sleep(1)
    else:
        raise SystemExit(f"database not reachable within timeout: {host}:{port}")
PY
fi

exec "$@"

