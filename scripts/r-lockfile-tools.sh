#!/usr/bin/env bash
set -euo pipefail

COMMAND=""
LOCKFILE="renv.lock"
PROJECT_DIR="."
CACHE_DIR=""
OUTPUT_DIR="."
ARCHIVE_NAME=""
PLATFORM=""
TIMESTAMP=""
REQUESTED_OUT_FILE=""

usage() {
  cat <<'USAGE'
Usage: r-lockfile-tools.sh <command> [options]

Commands:
  snapshot            Run renv::snapshot for the given project/lockfile.
  bundle              Archive an existing renv cache for offline transfer.
  export-requested    Convert renv.lock into an unpinned requested-package manifest.

Snapshot options:
  --lockfile <path>   Lockfile path (default: renv.lock)
  --project-dir <dir> Project directory containing renv project (default: .)

Bundle options:
  --cache-dir <dir>   Source cache directory (required)
  --output-dir <dir>  Destination directory for bundle + checksum (default: .)
  --archive-name <n>  Override archive filename
  --platform <name>   Platform tag to embed in default names
  --timestamp <ts>    Timestamp to embed (default: current UTC)

Export-requested options:
  --lockfile <path>         Source renv.lock (default: renv.lock)
  --requested-out-file <p>  Destination requested-package manifest (required)
USAGE
}

require_arg() {
  local name="$1"
  local value="$2"
  if [[ -z "$value" ]]; then
    echo "Missing required argument: $name" >&2
    usage >&2
    exit 1
  fi
}

if [[ $# -lt 1 ]]; then
  usage
  exit 1
fi

COMMAND="$1"
shift

while [[ $# -gt 0 ]]; do
  case "$1" in
    --lockfile) LOCKFILE="$2"; shift 2 ;;
    --project-dir) PROJECT_DIR="$2"; shift 2 ;;
    --cache-dir) CACHE_DIR="$2"; shift 2 ;;
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    --archive-name) ARCHIVE_NAME="$2"; shift 2 ;;
    --platform) PLATFORM="$2"; shift 2 ;;
    --timestamp) TIMESTAMP="$2"; shift 2 ;;
    --requested-out-file) REQUESTED_OUT_FILE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
fi

case "$COMMAND" in
  snapshot)
    if [[ ! -d "$PROJECT_DIR" ]]; then
      echo "Project directory not found: $PROJECT_DIR" >&2
      exit 1
    fi
    Rscript -e "if (!requireNamespace('renv', quietly = TRUE)) stop('renv must be installed'); renv::snapshot(project = '$PROJECT_DIR', lockfile = '$LOCKFILE', prompt = FALSE)"
    ;;
  bundle)
    require_arg "--cache-dir" "$CACHE_DIR"
    mkdir -p "$OUTPUT_DIR"
    if [[ -z "$TIMESTAMP" ]]; then
      TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
    fi
    if [[ -z "$ARCHIVE_NAME" ]]; then
      if [[ -z "$PLATFORM" ]]; then
        ARCHIVE_NAME="renv-cache-${TIMESTAMP}.tar.gz"
      else
        ARCHIVE_NAME="renv-cache-${PLATFORM}-${TIMESTAMP}.tar.gz"
      fi
    fi
    ARCHIVE_PATH="${OUTPUT_DIR%/}/${ARCHIVE_NAME}"
    tar -C "$CACHE_DIR" -czf "$ARCHIVE_PATH" .
    (cd "$OUTPUT_DIR" && sha256sum "$ARCHIVE_NAME" > "${ARCHIVE_NAME}.sha256")
    echo "Created $ARCHIVE_PATH and checksum ${ARCHIVE_PATH}.sha256"
    ;;
  export-requested)
    require_arg "--requested-out-file" "$REQUESTED_OUT_FILE"
    python3 "$(dirname "$0")/convert-r-lockfile-to-requested.py" \
      --lock-file "$LOCKFILE" \
      --out-file "$REQUESTED_OUT_FILE"
    echo "Created requested-package manifest $REQUESTED_OUT_FILE"
    ;;
  *)
    echo "Unknown command: $COMMAND" >&2
    usage >&2
    exit 1
    ;;
 esac
