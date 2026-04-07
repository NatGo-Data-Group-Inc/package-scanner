#!/usr/bin/env bash
set -euo pipefail

PORT="5004"
HOST="127.0.0.1"
REGION="us-east-1"
PROFILE=""
ALLOW_DEFAULT_PROFILE="false"
CATALOG_BUCKET=""
CATALOG_PREFIX="evidence"
APP_HOME="/tmp/package-scanner-webapp-home"
LOG_FILE="/tmp/package-scanner-webapp.log"
PID_FILE="/tmp/package-scanner-webapp.pid"

usage() {
  cat <<'EOF'
Usage: start-webapp.sh [options]

Options:
  --port <port>                 (default: 5004)
  --host <host>                 (default: 127.0.0.1)
  --region <region>             (default: us-east-1)
  --profile <profile>
  --allow-default-profile
  --catalog-bucket <bucket>     Required unless CATALOG_BUCKET is already set
  --catalog-prefix <prefix>     (default: evidence)
  --app-home <dir>              (default: /tmp/package-scanner-webapp-home)
  --log-file <path>             (default: /tmp/package-scanner-webapp.log)
  --pid-file <path>             (default: /tmp/package-scanner-webapp.pid)
  -h|--help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --allow-default-profile) ALLOW_DEFAULT_PROFILE="true"; shift 1 ;;
    --catalog-bucket) CATALOG_BUCKET="$2"; shift 2 ;;
    --catalog-prefix) CATALOG_PREFIX="$2"; shift 2 ;;
    --app-home) APP_HOME="$2"; shift 2 ;;
    --log-file) LOG_FILE="$2"; shift 2 ;;
    --pid-file) PID_FILE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if [[ "${ALLOW_DEFAULT_PROFILE}" != "true" && -z "${PROFILE}" ]]; then
  echo "Guardrail: --profile is required unless --allow-default-profile is explicitly set." >&2
  exit 1
fi

if [[ -z "${CATALOG_BUCKET}" ]]; then
  CATALOG_BUCKET="${CATALOG_BUCKET:-}"
fi
if [[ -z "${CATALOG_BUCKET}" ]]; then
  echo "--catalog-bucket is required." >&2
  exit 1
fi

if [[ ! -x ".venv/bin/flask" ]]; then
  echo "Missing .venv/bin/flask" >&2
  exit 1
fi

if [[ -f "${PID_FILE}" ]]; then
  existing_pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
  if [[ -n "${existing_pid}" ]] && kill -0 "${existing_pid}" 2>/dev/null; then
    echo "Webapp already running with PID ${existing_pid}. Stop it first." >&2
    exit 1
  fi
  rm -f "${PID_FILE}"
fi

if ! python - "${HOST}" "${PORT}" <<'PY'
import socket
import sys
host = sys.argv[1]
port = int(sys.argv[2])
sock = socket.socket()
try:
    sock.bind((host, port))
except OSError:
    raise SystemExit(1)
finally:
    sock.close()
PY
then
  echo "Port ${PORT} is already in use. Stop the existing listener first." >&2
  exit 1
fi

mkdir -p "${APP_HOME}" "$(dirname "${LOG_FILE}")" "$(dirname "${PID_FILE}")"
mkdir -p "${APP_HOME}/.aws"
if [[ -d "${HOME}/.aws" ]]; then
  cp -a "${HOME}/.aws/." "${APP_HOME}/.aws/"
fi

export HOME="${APP_HOME}"
export PYTHONPATH="$(pwd)"
export AWS_REGION="${REGION}"
export CATALOG_BUCKET
export CATALOG_PREFIX
export FLASK_APP="webapp/app.py"
export FLASK_DEBUG="0"
export WEBAPP_LOG_PATH="${LOG_FILE}"
if [[ -n "${PROFILE}" ]]; then
  export AWS_PROFILE="${PROFILE}"
fi

app_pid="$(
python - "${HOST}" "${PORT}" "${LOG_FILE}" <<'PY'
import os
import subprocess
import sys

host, port, log_file = sys.argv[1:4]
env = os.environ.copy()
with open(log_file, "ab", buffering=0) as log:
    proc = subprocess.Popen(
        [
            ".venv/bin/flask",
            "run",
            "--host",
            host,
            "--port",
            port,
            "--no-debugger",
            "--no-reload",
        ],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
        close_fds=True,
        start_new_session=True,
    )
print(proc.pid)
PY
)"
echo "${app_pid}" > "${PID_FILE}"

for _ in $(seq 1 30); do
  if ! kill -0 "${app_pid}" 2>/dev/null; then
    echo "Webapp failed to start. See ${LOG_FILE}" >&2
    exit 1
  fi
  if python - "${HOST}" "${PORT}" <<'PY'
import http.client
import sys

host = sys.argv[1]
port = int(sys.argv[2])
conn = http.client.HTTPConnection(host, port, timeout=10)
try:
    conn.request("GET", "/")
    resp = conn.getresponse()
    raise SystemExit(0 if 100 <= resp.status < 600 else 1)
except Exception:
    raise SystemExit(1)
finally:
    try:
        conn.close()
    except Exception:
        pass
PY
  then
    echo "Webapp running at http://${HOST}:${PORT}"
    echo "PID ${app_pid}"
    exit 0
  fi
  sleep 1
done

echo "Webapp start timed out. See ${LOG_FILE}" >&2
exit 1
