#!/usr/bin/env bash
set -euo pipefail

PORT="5004"
PID_FILE="/tmp/package-scanner-webapp.pid"

usage() {
  cat <<'EOF'
Usage: stop-webapp.sh [options]

Options:
  --port <port>         (default: 5004)
  --pid-file <path>     (default: /tmp/package-scanner-webapp.pid)
  -h|--help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --pid-file) PID_FILE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

stopped="false"
if [[ -f "${PID_FILE}" ]]; then
  pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
  if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
    kill -- -"${pid}" 2>/dev/null || kill "${pid}" 2>/dev/null || true
    for _ in $(seq 1 10); do
      if ! kill -0 "${pid}" 2>/dev/null; then
        stopped="true"
        break
      fi
      sleep 1
    done
    if [[ "${stopped}" != "true" ]]; then
      kill -9 -- -"${pid}" 2>/dev/null || kill -9 "${pid}" 2>/dev/null || true
      stopped="true"
    fi
  fi
  rm -f "${PID_FILE}"
fi

if command -v lsof >/dev/null 2>&1; then
  lingering_pids="$(lsof -tiTCP:"${PORT}" -sTCP:LISTEN || true)"
  if [[ -n "${lingering_pids}" ]]; then
    kill ${lingering_pids} 2>/dev/null || true
    sleep 1
    lingering_pids="$(lsof -tiTCP:"${PORT}" -sTCP:LISTEN || true)"
    if [[ -n "${lingering_pids}" ]]; then
      kill -9 ${lingering_pids} 2>/dev/null || true
    fi
    stopped="true"
  fi
fi

if command -v netstat >/dev/null 2>&1 && command -v taskkill >/dev/null 2>&1; then
  lingering_pids="$(
    netstat -ano -p tcp 2>/dev/null |
      awk -v port=":${PORT}" '$2 ~ port "$" && $4 == "LISTENING" {print $5}' |
      sort -u
  )"
  if [[ -n "${lingering_pids}" ]]; then
    for pid in ${lingering_pids}; do
      taskkill //PID "${pid}" //T //F >/dev/null 2>&1 || true
    done
    stopped="true"
  fi
fi

if [[ "${stopped}" == "true" ]]; then
  echo "Stopped webapp on port ${PORT}"
else
  echo "No webapp process found for port ${PORT}"
fi
