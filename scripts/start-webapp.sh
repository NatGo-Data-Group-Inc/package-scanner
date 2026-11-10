#!/usr/bin/env bash
set -euo pipefail

PORT="5004"
HOST="127.0.0.1"
REGION="us-east-1"
PROFILE=""
ALLOW_DEFAULT_PROFILE="false"
CATALOG_BUCKET=""
CATALOG_PREFIX="evidence"
EPHEMERAL_BUCKET=""
EPHEMERAL_PREFIX="deploy/tmp/r"
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
  --ephemeral-bucket <bucket>   Optional, but required for triage/checkpoint views
  --ephemeral-prefix <prefix>   (default: deploy/tmp/r)
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
    --ephemeral-bucket) EPHEMERAL_BUCKET="$2"; shift 2 ;;
    --ephemeral-prefix) EPHEMERAL_PREFIX="$2"; shift 2 ;;
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

GUNICORN_CMD=""
if [[ -x ".venv/bin/python" ]]; then
  GUNICORN_CMD=".venv/bin/python -m gunicorn"
elif [[ -x ".venv/Scripts/python.exe" ]]; then
  GUNICORN_CMD=".venv/Scripts/python.exe -m gunicorn"
fi

if [[ -z "${GUNICORN_CMD}" ]]; then
  echo "Missing gunicorn runtime. Expected .venv/bin/python or .venv/Scripts/python.exe with gunicorn installed." >&2
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
export EPHEMERAL_BUCKET
export EPHEMERAL_PREFIX
export R_STATE_MACHINE_ARNS="${R_STATE_MACHINE_ARNS:-arn:aws:states:us-east-1:807497180525:stateMachine:package-scanner-dev-r-ecs-scan-orchestrator,arn:aws:states:us-east-1:807497180525:stateMachine:package-scanner-dev-r-ecs-linux-scan-orchestrator,arn:aws:states:us-east-1:807497180525:stateMachine:package-scanner-dev-r-ecs-windows-scan-orchestrator}"
export WEBAPP_LOG_PATH="${LOG_FILE}"
if [[ -n "${PROFILE}" ]]; then
  export AWS_PROFILE="${PROFILE}"
fi
export GUNICORN_WORKERS="${GUNICORN_WORKERS:-4}"
export GUNICORN_THREADS="${GUNICORN_THREADS:-8}"
export GUNICORN_TIMEOUT="${GUNICORN_TIMEOUT:-120}"

app_pid="$(
python - "${HOST}" "${PORT}" "${LOG_FILE}" "${GUNICORN_CMD}" <<'PY'
import os
import shlex
import subprocess
import sys

host, port, log_file, gunicorn_cmd = sys.argv[1:5]
env = os.environ.copy()
with open(log_file, "ab", buffering=0) as log:
    proc = subprocess.Popen(
        shlex.split(gunicorn_cmd) + [
            "--bind",
            f"{host}:{port}",
            "--workers",
            env.get("GUNICORN_WORKERS", "4"),
            "--threads",
            env.get("GUNICORN_THREADS", "8"),
            "--timeout",
            env.get("GUNICORN_TIMEOUT", "120"),
            "webapp.app:app",
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
  if python - "${HOST}" "${PORT}" <<'PY'
import http.client
import sys

host = sys.argv[1]
port = int(sys.argv[2])
conn = http.client.HTTPConnection(host, port, timeout=10)
try:
    conn.request("GET", "/healthz")
    resp = conn.getresponse()
    raise SystemExit(0 if resp.status == 200 else 1)
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
  if ! kill -0 "${app_pid}" 2>/dev/null; then
    echo "Webapp failed to start. See ${LOG_FILE}" >&2
    exit 1
  fi
  sleep 1
done

echo "Webapp start timed out. See ${LOG_FILE}" >&2
exit 1
