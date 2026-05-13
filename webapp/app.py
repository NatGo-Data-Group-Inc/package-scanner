from __future__ import annotations

import csv
import io
import json
import logging
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import uuid
import zipfile
from collections.abc import Iterator
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

from flask import Flask, abort, g, jsonify, redirect, render_template, request, send_file

from package_scanner.catalog import catalog_pointer_key, catalog_run_key
from package_scanner.catalog_awscli import (
    AwsAuthExpiredError,
    aws_json,
    s3_get_bytes,
    s3_get_json,
    s3_list_page,
    s3_head_object,
    s3_put_bytes,
    s3_put_json,
    s3_presign,
    stepfunctions_describe_execution,
    stepfunctions_list_executions,
)


def architecture_label(platform: str) -> str:
    if platform.startswith("windows"):
        return "windows"
    return platform


def artifact_bundle_entries(platform: dict) -> list[tuple[str, str]]:
    paths = platform.get("paths", {})
    entries = [
        ("materialization-summary.json", paths.get("materialization_summary_key")),
        ("governance-summary.json", paths.get("governance_summary_key")),
        ("run-metadata.json", paths.get("run_metadata_key")),
        (
            "approval-candidate-packages.csv",
            f"{paths.get('requirements_prefix', '')}approval-candidate-packages.csv" if paths.get("requirements_prefix") else None,
        ),
        ("vulnerability-findings.csv", paths.get("vulnerability_findings_key")),
        ("remediation-required.csv", paths.get("remediation_required_key")),
        ("remediation-exceptions.csv", paths.get("remediation_exceptions_key")),
        ("remediation-spreadsheet.csv", paths.get("remediation_spreadsheet_key")),
        ("trivy-sbom-report.json", paths.get("trivy_report_key")),
        ("safety-report.json", paths.get("safety_report_key")),
        ("osv-report.json", paths.get("osv_report_key")),
    ]
    return [(filename, key) for filename, key in entries if key]


def posit_handoff_bundle_entries(platform: dict, scan_timestamp: str) -> list[tuple[str, str]]:
    paths = platform.get("paths", {})
    entries = [
        ("renv.lock", f"{paths.get('requirements_prefix', '')}renv.lock" if paths.get("requirements_prefix") else None),
        (
            "installed-packages.csv",
            f"{paths.get('requirements_prefix', '')}installed-packages.csv" if paths.get("requirements_prefix") else None,
        ),
        ("materialization-summary.json", paths.get("materialization_summary_key")),
        ("run-metadata.json", paths.get("run_metadata_key")),
        (
            f"renv-library-{platform.get('platform')}-{scan_timestamp}.tar.gz",
            f"{paths.get('env_artifacts_prefix', '')}renv-library-{platform.get('platform')}-{scan_timestamp}.tar.gz"
            if paths.get("env_artifacts_prefix") and platform.get("platform") and scan_timestamp
            else None,
        ),
        (
            f"renv-library-{platform.get('platform')}-{scan_timestamp}.tar.gz.sha256",
            f"{paths.get('env_artifacts_prefix', '')}renv-library-{platform.get('platform')}-{scan_timestamp}.tar.gz.sha256"
            if paths.get("env_artifacts_prefix") and platform.get("platform") and scan_timestamp
            else None,
        ),
    ]
    bundle_keys = platform.get("bundle_keys") or []
    for key in bundle_keys:
        filename = str(key).rsplit("/", 1)[-1]
        entries.append((filename, key))
    return [(filename, key) for filename, key in entries if key]


def python_handoff_bundle_entries(platform: dict, scan_timestamp: str) -> list[tuple[str, str]]:
    paths = platform.get("paths", {})
    platform_name = platform.get("platform")
    entries = [
        ("environment.yml", f"{paths.get('requirements_prefix', '')}environment.yml" if paths.get("requirements_prefix") else None),
        (
            "requirements.lock.txt",
            f"{paths.get('requirements_prefix', '')}requirements.lock.txt" if paths.get("requirements_prefix") else None,
        ),
        (
            "conda-list.json",
            f"{paths.get('env_artifacts_prefix', '')}conda-list.json" if paths.get("env_artifacts_prefix") else None,
        ),
        ("materialization-summary.json", paths.get("materialization_summary_key")),
        ("run-metadata.json", paths.get("run_metadata_key")),
        (
            f"python-env-{platform_name}-{scan_timestamp}.tar.gz",
            f"{paths.get('env_artifacts_prefix', '')}python-env-{platform_name}-{scan_timestamp}.tar.gz"
            if paths.get("env_artifacts_prefix") and platform_name and scan_timestamp
            else None,
        ),
        (
            f"python-env-{platform_name}-{scan_timestamp}.tar.gz.sha256",
            f"{paths.get('env_artifacts_prefix', '')}python-env-{platform_name}-{scan_timestamp}.tar.gz.sha256"
            if paths.get("env_artifacts_prefix") and platform_name and scan_timestamp
            else None,
        ),
    ]
    return [(filename, key) for filename, key in entries if key]


def local_bundle_script_entries(ecosystem: str) -> list[tuple[str, Path]]:
    if ecosystem != "r":
        return []
    repo_root = Path(__file__).resolve().parents[1]
    entries = [
        ("scripts/verify-posit-handoff.sh", repo_root / "scripts" / "verify-posit-handoff.sh"),
        ("scripts/verify-posit-handoff-inside.sh", repo_root / "scripts" / "verify-posit-handoff-inside.sh"),
    ]
    return [(name, path) for name, path in entries if path.exists()]


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def local_verify_paths(execution_id: str, platform_name: str) -> dict[str, Path]:
    safe_execution = execution_id.replace("/", "_")
    safe_platform = platform_name.replace("/", "_")
    base = Path("/tmp/package-scanner-local-verify") / safe_execution / safe_platform
    return {
        "base": base,
        "log": base / "verify.log",
        "pid": base / "verify.pid",
        "exit": base / "verify.exitcode",
        "meta": base / "verify-meta.json",
    }


def pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_local_verify_status(execution_id: str, platform_name: str) -> dict | None:
    paths = local_verify_paths(execution_id, platform_name)
    if not paths["base"].exists():
        return None

    pid = None
    exit_code = None
    metadata = {}
    if paths["pid"].exists():
        try:
            pid = int(paths["pid"].read_text(encoding="utf-8").strip())
        except Exception:
            pid = None
    if paths["exit"].exists():
        try:
            exit_code = int(paths["exit"].read_text(encoding="utf-8").strip())
        except Exception:
            exit_code = None
    if paths["meta"].exists():
        try:
            metadata = json.loads(paths["meta"].read_text(encoding="utf-8"))
        except Exception:
            metadata = {}

    running = bool(pid and pid_is_running(pid))
    if running:
        status = "RUNNING"
    elif exit_code == 0:
        status = "SUCCEEDED"
    elif exit_code is not None:
        status = "FAILED"
    else:
        status = "UNKNOWN"

    log_tail = None
    if paths["log"].exists():
        try:
            lines = paths["log"].read_text(encoding="utf-8", errors="ignore").splitlines()
            log_tail = "\n".join(lines[-80:])
        except Exception:
            log_tail = None

    return {
        "status": status,
        "pid": pid,
        "running": running,
        "exit_code": exit_code,
        "log_path": str(paths["log"]),
        "log_tail": log_tail,
        "work_dir": metadata.get("work_dir"),
        "app_name": metadata.get("app_name"),
        "timestamp": metadata.get("timestamp"),
    }


def parse_csv_bytes(payload: bytes) -> list[dict[str, str]]:
    text = payload.decode("utf-8-sig", errors="ignore")
    return list(csv.DictReader(io.StringIO(text)))


def first_cve(aliases: list[str]) -> str:
    return next((alias for alias in aliases if alias.upper().startswith("CVE-")), "")


def osv_advisory_url(vulnerability_id: str) -> str:
    if vulnerability_id.upper().startswith(("RSEC-", "GHSA-", "MAL-")):
        return f"https://osv.dev/vulnerability/{vulnerability_id}"
    return ""


def cran_package_url(package_name: str) -> str:
    return f"https://cran.r-project.org/package={quote_plus(package_name)}"


def bioconductor_package_url(package_name: str) -> str:
    return f"https://bioconductor.org/packages/{quote_plus(package_name)}"


def utc_compact_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_datetime(value) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 1_000_000_000_000:
            timestamp /= 1000.0
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    for parser in (
        lambda item: datetime.strptime(item, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc),
        lambda item: datetime.fromisoformat(item.replace("Z", "+00:00")),
        lambda item: parsedate_to_datetime(item),
    ):
        try:
            parsed = parser(text)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        except (TypeError, IndexError):
            continue
    return None


def format_display_datetime(value) -> str:
    parsed = parse_datetime(value)
    if not parsed:
        return str(value or "")
    return parsed.astimezone().strftime("%Y-%m-%d %I:%M:%S %p")


def format_duration(start, stop=None) -> str | None:
    started_at = parse_datetime(start)
    stopped_at = parse_datetime(stop) or datetime.now(timezone.utc)
    if not started_at or not stopped_at:
        return None
    seconds = max(0, int((stopped_at - started_at).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def format_duration_seconds(duration_seconds: int | float | None) -> str | None:
    if duration_seconds is None:
        return None
    seconds = max(0, int(duration_seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def parse_state_machine_arns(configured: str | None, legacy: str | None, defaults: list[str]) -> list[str]:
    values = [item.strip() for item in str(configured or "").split(",") if item.strip()]
    if values:
        return values
    if legacy and legacy.strip():
        return [legacy.strip()]
    return defaults


ACTIVE_RUN_STAGE_ORDER = {
    "r": ["preflight", "restore", "restored", "analysis", "governance", "publish", "completed"],
    "python": ["materialize", "restored", "analysis", "governance", "publish", "completed"],
}


def stage_display_name(stage: str | None) -> str:
    normalized = str(stage or "").strip().replace("-", " ").replace("_", " ")
    if not normalized:
        return "n/a"
    special = {
        "queued": "Queued",
        "starting": "Starting",
        "retrying": "Retrying",
        "failed": "Failed",
    }
    if normalized.lower() in special:
        return special[normalized.lower()]
    return normalized.title()


def stage_position(ecosystem: str, stage: str | None) -> int | None:
    order = ACTIVE_RUN_STAGE_ORDER.get(ecosystem, [])
    normalized = str(stage or "").strip().lower()
    if not normalized or normalized not in order:
        return None
    return order.index(normalized)


def stage_at_or_beyond(ecosystem: str, current_stage: str | None, threshold_stage: str) -> bool:
    current_pos = stage_position(ecosystem, current_stage)
    threshold_pos = stage_position(ecosystem, threshold_stage)
    if current_pos is None or threshold_pos is None:
        return False
    return current_pos >= threshold_pos


def stage_steps_for_run(ecosystem: str, current_stage: str | None, status: str) -> list[dict[str, str]]:
    order = ACTIVE_RUN_STAGE_ORDER.get(ecosystem, [])
    normalized_stage = str(current_stage or "").strip().lower()
    normalized_status = str(status or "").strip().upper()
    failed = normalized_status in {"FAILED", "TIMED_OUT", "ABORTED"}
    steps: list[dict[str, str]] = []
    for stage in order:
        if failed and stage == normalized_stage:
            step_status = "failed"
        elif normalized_stage == "completed":
            step_status = "done"
        elif normalized_stage == stage:
            step_status = "current"
        elif normalized_stage in order and order.index(stage) < order.index(normalized_stage):
            step_status = "done"
        else:
            step_status = "pending"
        steps.append({"name": stage_display_name(stage), "status": step_status})
    if normalized_status == "RUNNING" and normalized_stage in {"queued", "starting", "retrying"} and steps:
        steps[0]["status"] = "current"
    return steps


def status_class(status: str) -> str:
    normalized = str(status or "").upper()
    if normalized in {"SUCCEEDED", "RUNNING"}:
        return "ok"
    if normalized in {"FAILED", "TIMED_OUT", "ABORTED"}:
        return "bad"
    return "muted"


def ecosystem_platform_badge(ecosystem: str, platform: str) -> str:
    return f"{ecosystem}-{architecture_label(platform or 'unknown')}"


LISTING_CACHE_TTL_SECONDS = 120
RECORD_CACHE_TTL_SECONDS = 300
CURSOR_CACHE_TTL_SECONDS = 1800


def create_app() -> Flask:
    app = Flask(__name__)
    listing_cache: dict[str, dict] = {}
    record_cache: dict[str, dict] = {}
    cursor_cache: dict[str, dict] = {}
    head_cache: dict[str, dict] = {}
    execution_history_cache: dict[str, dict] = {}
    ecs_task_cache: dict[str, dict] = {}
    execution_input_cache: dict[str, dict] = {}
    log_path = os.environ.get("WEBAPP_LOG_PATH", "/tmp/package-scanner-webapp.log")
    if not app.logger.handlers:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        app.logger.addHandler(stream_handler)
    if not any(isinstance(handler, logging.FileHandler) and getattr(handler, "baseFilename", "") == log_path for handler in app.logger.handlers):
        file_handler = logging.FileHandler(log_path)
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        app.logger.addHandler(file_handler)
    app.logger.setLevel(logging.INFO)
    app.logger.propagate = False
    app.config["CATALOG_BUCKET"] = os.environ.get("CATALOG_BUCKET", "")
    app.config["CATALOG_PREFIX"] = os.environ.get("CATALOG_PREFIX", "evidence")
    app.config["AWS_REGION"] = os.environ.get("AWS_REGION", "us-east-1")
    app.config["AWS_PROFILE"] = os.environ.get("AWS_PROFILE")
    app.config["R_STACK_NAME"] = os.environ.get("R_STACK_NAME", "cyber-scanner-dev-r-ecs")
    app.config["PYTHON_STACK_NAME"] = os.environ.get("PYTHON_STACK_NAME", "cyber-scanner-dev-python-ecs")
    app.config["EPHEMERAL_BUCKET"] = os.environ.get("EPHEMERAL_BUCKET", "")
    app.config["PYTHON_STATE_MACHINE_ARN"] = os.environ.get(
        "PYTHON_STATE_MACHINE_ARN",
        "arn:aws:states:us-east-1:807497180525:stateMachine:package-scanner-dev-python-ecs-scan-orchestrator",
    )
    app.config["R_STATE_MACHINE_ARN"] = os.environ.get(
        "R_STATE_MACHINE_ARN",
        "arn:aws:states:us-east-1:807497180525:stateMachine:package-scanner-dev-r-ecs-linux-scan-orchestrator",
    )
    app.config["PYTHON_STATE_MACHINE_ARNS"] = parse_state_machine_arns(
        os.environ.get("PYTHON_STATE_MACHINE_ARNS"),
        app.config["PYTHON_STATE_MACHINE_ARN"],
        [
            "arn:aws:states:us-east-1:807497180525:stateMachine:package-scanner-dev-python-ecs-scan-orchestrator",
            "arn:aws:states:us-east-1:807497180525:stateMachine:package-scanner-dev-python-ecs-linux-scan-orchestrator",
        ],
    )
    app.config["R_STATE_MACHINE_ARNS"] = parse_state_machine_arns(
        os.environ.get("R_STATE_MACHINE_ARNS"),
        app.config["R_STATE_MACHINE_ARN"],
        [
            "arn:aws:states:us-east-1:807497180525:stateMachine:package-scanner-dev-r-ecs-scan-orchestrator",
            "arn:aws:states:us-east-1:807497180525:stateMachine:package-scanner-dev-r-ecs-linux-scan-orchestrator",
            "arn:aws:states:us-east-1:807497180525:stateMachine:package-scanner-dev-r-ecs-windows-scan-orchestrator",
        ],
    )
    stack_output_cache: dict[str, dict] = {}

    @app.before_request
    def log_request_start() -> None:
        g.request_started_at = time.monotonic()
        app.logger.info("request start %s %s from=%s", request.method, request.full_path, request.remote_addr)

    @app.after_request
    def log_request_end(response):
        started_at = getattr(g, "request_started_at", None)
        elapsed_ms = int((time.monotonic() - started_at) * 1000) if started_at is not None else -1
        app.logger.info(
            "request end %s %s status=%s elapsed_ms=%s",
            request.method,
            request.full_path,
            response.status_code,
            elapsed_ms,
        )
        return response

    @app.teardown_request
    def log_request_exception(exc: BaseException | None) -> None:
        if exc is not None:
            app.logger.exception("request exception %s %s", request.method, request.full_path, exc_info=exc)

    def auth_message() -> str | None:
        return None

    def package_count_for_platform(record: dict, platform: dict, ecosystem: str) -> int | None:
        try:
            summary = s3_get_json(
                app.config["CATALOG_BUCKET"],
                platform["paths"]["materialization_summary_key"],
                region=app.config["AWS_REGION"],
                profile=app.config["AWS_PROFILE"],
            )
        except Exception:
            return None
        counts = summary.get("counts", {})
        if ecosystem == "r":
            return counts.get("restored_packages")
        if ecosystem == "python":
            pip_count = counts.get("pip_package_count")
            conda_count = counts.get("conda_package_count")
            if isinstance(pip_count, int) and isinstance(conda_count, int):
                return pip_count + conda_count
            return pip_count if isinstance(pip_count, int) else conda_count
        return None

    def s3_get_rows(bucket: str, key: str) -> list[dict[str, str]]:
        return parse_csv_bytes(
            s3_get_bytes(
                bucket,
                key,
                region=app.config["AWS_REGION"],
                profile=app.config["AWS_PROFILE"],
            )
        )

    def s3_get_json_optional(bucket: str, key: str) -> dict | None:
        try:
            return s3_get_json(
                bucket,
                key,
                region=app.config["AWS_REGION"],
                profile=app.config["AWS_PROFILE"],
            )
        except Exception:
            return None

    def get_listing_keys(ecosystem: str) -> list[str]:
        now = time.time()
        cache_key = ecosystem
        cached = listing_cache.get(cache_key)
        if cached and cached["expires_at"] > now:
            return cached["keys"]

        prefix = f"{app.config['CATALOG_PREFIX'].rstrip('/')}/catalog/{ecosystem}/runs/"
        continuation_token = None
        keys: list[str] = []
        while True:
            page = s3_list_page(
                app.config["CATALOG_BUCKET"],
                prefix,
                region=app.config["AWS_REGION"],
                profile=app.config["AWS_PROFILE"],
                max_keys=200,
                continuation_token=continuation_token,
            )
            keys.extend(key for key in page["keys"] if key.endswith(".json"))
            if not page["is_truncated"]:
                break
            continuation_token = page["next_continuation_token"]

        keys.sort(reverse=True)
        listing_cache[cache_key] = {
            "expires_at": now + LISTING_CACHE_TTL_SECONDS,
            "keys": keys,
        }
        return keys

    def list_runs(ecosystem: str) -> list[dict]:
        prefix = f"{app.config['CATALOG_PREFIX'].rstrip('/')}/catalog/{ecosystem}/runs/"
        rows = []
        for key in get_listing_keys(ecosystem):
            try:
                rows.append(load_record(ecosystem, key.rsplit("/", 1)[-1].replace(".json", "")))
            except Exception:
                continue
        rows.sort(key=lambda row: row.get("scan_timestamp", ""), reverse=True)
        return rows

    def parse_execution_input(execution_arn: str) -> dict:
        cache_key = str(execution_arn or "").strip()
        if not cache_key:
            return {}
        now = time.time()
        cached = execution_input_cache.get(cache_key)
        if cached and cached["expires_at"] > now:
            return cached["input"]
        try:
            detail = stepfunctions_describe_execution(
                cache_key,
                region=app.config["AWS_REGION"],
                profile=app.config["AWS_PROFILE"],
            )
        except Exception:
            return {}
        raw_input = detail.get("input")
        if isinstance(raw_input, str) and raw_input.strip():
            try:
                parsed = json.loads(raw_input)
                result = parsed if isinstance(parsed, dict) else {}
                execution_input_cache[cache_key] = {
                    "expires_at": now + RECORD_CACHE_TTL_SECONDS,
                    "input": result,
                }
                return result
            except json.JSONDecodeError:
                return {}
        result = raw_input if isinstance(raw_input, dict) else {}
        execution_input_cache[cache_key] = {
            "expires_at": now + RECORD_CACHE_TTL_SECONDS,
            "input": result,
        }
        return result

    def execution_arn_candidates(ecosystem: str, execution_id: str) -> list[str]:
        arns = configured_state_machine_arns(ecosystem)
        candidates: list[str] = []
        for state_machine_arn in arns:
            parts = str(state_machine_arn).split(":")
            if len(parts) < 7:
                continue
            partition = parts[1]
            service = parts[2]
            region = parts[3]
            account = parts[4]
            resource = ":".join(parts[5:])
            if not resource.startswith("stateMachine:"):
                continue
            state_machine_name = resource.split(":", 1)[1]
            candidates.append(f"arn:{partition}:{service}:{region}:{account}:execution:{state_machine_name}:{execution_id}")
        return candidates

    def execution_input_for_run(ecosystem: str, execution_id: str, execution_arn: str | None = None) -> dict:
        if execution_arn:
            parsed = parse_execution_input(execution_arn)
            if parsed:
                return parsed
        for candidate_arn in execution_arn_candidates(ecosystem, execution_id):
            parsed = parse_execution_input(candidate_arn)
            if parsed:
                return parsed
        return {}

    def execution_detail_for_run(ecosystem: str, execution_id: str, execution_arn: str | None = None) -> dict | None:
        candidate_arns: list[str] = []
        if execution_arn:
            candidate_arns.append(execution_arn)
        candidate_arns.extend(
            arn for arn in execution_arn_candidates(ecosystem, execution_id) if arn not in candidate_arns
        )
        for candidate_arn in candidate_arns:
            try:
                detail = stepfunctions_describe_execution(
                    candidate_arn,
                    region=app.config["AWS_REGION"],
                    profile=app.config["AWS_PROFILE"],
                )
            except Exception:
                continue
            if isinstance(detail, dict) and detail.get("executionArn"):
                return detail
        return None

    def stack_output_value_cached(stack_name: str, output_key: str) -> str:
        cache_key = f"{stack_name}|{output_key}"
        now = time.time()
        cached = stack_output_cache.get(cache_key)
        if cached and cached["expires_at"] > now:
            return str(cached["value"] or "")
        try:
            stack = describe_stack(stack_name)
        except Exception:
            return ""
        value = stack_output_value(stack, output_key)
        stack_output_cache[cache_key] = {
            "expires_at": now + 60,
            "value": value,
        }
        return str(value or "")

    def configured_state_machine_arns(ecosystem: str) -> list[str]:
        base = (
            list(app.config["PYTHON_STATE_MACHINE_ARNS"])
            if ecosystem == "python"
            else list(app.config["R_STATE_MACHINE_ARNS"])
        )
        discovered: list[str] = []
        if ecosystem == "python":
            stack_name = app.config["PYTHON_STACK_NAME"]
            for output_key in ("PythonScanOrchestrationStateMachineArn", "PythonLinuxScanOrchestrationStateMachineArn"):
                value = stack_output_value_cached(stack_name, output_key)
                if value and value != "None":
                    discovered.append(value)
        else:
            stack_name = app.config["R_STACK_NAME"]
            for output_key in (
                "RScanOrchestrationStateMachineArn",
                "RLinuxScanOrchestrationStateMachineArn",
                "RWindowsScanOrchestrationStateMachineArn",
            ):
                value = stack_output_value_cached(stack_name, output_key)
                if value and value != "None":
                    discovered.append(value)
        merged: list[str] = []
        seen: set[str] = set()
        for arn in [*discovered, *base]:
            arn = str(arn or "").strip()
            if not arn or arn in seen:
                continue
            seen.add(arn)
            merged.append(arn)
        return merged

    def execution_platforms(ecosystem: str, execution_input: dict) -> list[str]:
        selected_platforms = execution_input.get("platforms")
        if isinstance(selected_platforms, list):
            platforms = [str(item or "").strip() for item in selected_platforms if str(item or "").strip()]
            if platforms:
                return sorted(set(platforms))
        input_key = str(execution_input.get("input_object_key") or "").strip()
        platform_set = str(execution_input.get("platform_set") or "").strip()
        if "/windows-amd64/" in input_key or platform_set == "windows-only":
            return ["windows-amd64"]
        if "/linux-arm64/" in input_key:
            return ["linux-arm64"]
        if "/linux-amd64/" in input_key or platform_set in {"linux-only", "all"}:
            return ["linux-amd64"]
        return ["linux-amd64"] if ecosystem == "r" else ["unknown"]

    def stepfunctions_execution_history(execution_arn: str, *, max_results: int = 50) -> list[dict]:
        now = time.time()
        cached = execution_history_cache.get(execution_arn)
        if cached and cached["expires_at"] > now:
            return cached["events"]
        data = aws_json(
            [
                "stepfunctions",
                "get-execution-history",
                "--execution-arn",
                execution_arn,
                "--max-results",
                str(max_results),
                "--reverse-order",
            ],
            region=app.config["AWS_REGION"],
            profile=app.config["AWS_PROFILE"],
        )
        events = data.get("events") or []
        execution_history_cache[execution_arn] = {
            "expires_at": now + 15,
            "events": events,
        }
        return events

    def ecs_describe_task(cluster: str, task_arn: str) -> dict | None:
        cache_key = f"{cluster}|{task_arn}"
        now = time.time()
        cached = ecs_task_cache.get(cache_key)
        if cached and cached["expires_at"] > now:
            return cached["task"]
        data = aws_json(
            [
                "ecs",
                "describe-tasks",
                "--cluster",
                cluster,
                "--tasks",
                task_arn,
            ],
            region=app.config["AWS_REGION"],
            profile=app.config["AWS_PROFILE"],
        )
        tasks = data.get("tasks") or []
        task = tasks[0] if tasks else None
        ecs_task_cache[cache_key] = {
            "expires_at": now + 15,
            "task": task,
        }
        return task

    def stepfunctions_execution_task_context(execution_arn: str) -> dict:
        try:
            events = stepfunctions_execution_history(execution_arn)
        except Exception:
            return {}

        context: dict[str, str | bool | None] = {
            "task_arn": None,
            "cluster": None,
            "task_last_status": None,
            "container_last_status": None,
            "worker_attempt_failed": False,
            "task_started": False,
            "task_submitted": False,
        }
        for event in events:
            event_type = str(event.get("type") or "")
            if event_type == "TaskScheduled":
                details = event.get("taskScheduledEventDetails") or {}
                if str(details.get("resource") or "") != "runTask.sync":
                    continue
                parameters_raw = details.get("parameters")
                if isinstance(parameters_raw, str) and parameters_raw.strip():
                    try:
                        parameters = json.loads(parameters_raw)
                    except json.JSONDecodeError:
                        parameters = {}
                    cluster = str(parameters.get("Cluster") or "").strip()
                    if cluster:
                        context["cluster"] = cluster.rsplit("/", 1)[-1]
            elif event_type == "TaskStarted":
                details = event.get("taskStartedEventDetails") or {}
                if str(details.get("resource") or "") == "runTask.sync":
                    context["task_started"] = True
            elif event_type == "TaskSubmitted":
                details = event.get("taskSubmittedEventDetails") or {}
                if str(details.get("resource") or "") != "runTask.sync":
                    continue
                context["task_submitted"] = True
                output_raw = details.get("output")
                if isinstance(output_raw, str) and output_raw.strip():
                    try:
                        output = json.loads(output_raw)
                    except json.JSONDecodeError:
                        output = {}
                    tasks = output.get("Tasks") or []
                    if tasks:
                        first = tasks[0] or {}
                        task_arn = str(first.get("TaskArn") or "").strip()
                        cluster = str(first.get("ClusterArn") or "").strip()
                        if task_arn:
                            context["task_arn"] = task_arn
                        if cluster:
                            context["cluster"] = cluster.rsplit("/", 1)[-1]
            elif event_type in {"TaskFailed", "TaskTimedOut"}:
                details = event.get("taskFailedEventDetails") or event.get("taskTimedOutEventDetails") or {}
                if str(details.get("resource") or "") == "runTask.sync":
                    context["worker_attempt_failed"] = True

        cluster = str(context.get("cluster") or "").strip()
        task_arn = str(context.get("task_arn") or "").strip()
        if cluster and task_arn:
            try:
                task = ecs_describe_task(cluster, task_arn)
            except Exception:
                task = None
            if isinstance(task, dict):
                context["task_last_status"] = str(task.get("lastStatus") or "").upper() or None
                containers = task.get("containers") or []
                if containers:
                    context["container_last_status"] = str((containers[0] or {}).get("lastStatus") or "").upper() or None
        return context

    def derive_live_execution_state(
        ecosystem: str,
        execution: dict,
        execution_input: dict,
    ) -> dict:
        lifecycle_status = str(execution.get("status") or "UNKNOWN").upper()
        execution_id = str(execution.get("name") or "")
        checkpoint_bucket = str(execution_input.get("ephemeral_bucket") or "").strip() or None
        platforms = execution_platforms(ecosystem, execution_input)
        primary_platform = platforms[0] if platforms else ("linux-amd64" if ecosystem == "r" else "unknown")
        stage_state = checkpoint_stage_state(
            ecosystem,
            execution_id,
            primary_platform,
            bucket_override=checkpoint_bucket,
        ) or {}
        checkpoint_phase = str(stage_state.get("phase") or "").strip().lower() or None
        progress_current = stage_state.get("progress_current")
        progress_total = stage_state.get("progress_total")
        progress_count_display = None
        if isinstance(progress_current, int) and isinstance(progress_total, int) and progress_total > 0:
            progress_count_display = f"{progress_current}/{progress_total}"

        phase = checkpoint_phase
        phase_detail = None
        task_context = stepfunctions_execution_task_context(str(execution.get("executionArn") or "")) if execution.get("executionArn") else {}

        if lifecycle_status == "RUNNING":
            task_last_status = str(task_context.get("task_last_status") or "").upper()
            container_last_status = str(task_context.get("container_last_status") or "").upper()
            worker_attempt_failed = bool(task_context.get("worker_attempt_failed"))
            if checkpoint_phase == "failed":
                checkpoint_phase = None
            if task_last_status == "RUNNING" or container_last_status == "RUNNING":
                phase = checkpoint_phase or "starting"
            elif task_last_status == "PENDING" or container_last_status == "PENDING":
                phase = "starting"
                phase_detail = "Task placed; container is starting."
            elif task_last_status == "STOPPED":
                phase = "retrying"
                phase_detail = "Latest worker attempt stopped; Step Functions is still retrying."
            elif bool(task_context.get("task_submitted")) or bool(task_context.get("task_started")):
                phase = checkpoint_phase or "starting"
            elif worker_attempt_failed:
                phase = "retrying"
                phase_detail = "Worker attempt failed; orchestration is still active."
            else:
                phase = checkpoint_phase or "queued"
                phase_detail = "Execution started; waiting for the active worker checkpoint."
        elif lifecycle_status == "SUCCEEDED":
            phase = "completed"
            progress_count_display = None
        elif lifecycle_status in {"FAILED", "TIMED_OUT", "ABORTED"}:
            phase = checkpoint_phase or "failed"
            progress_count_display = None

        return {
            "lifecycle_status": lifecycle_status,
            "phase": phase,
            "phase_display": stage_display_name(phase),
            "phase_detail": phase_detail,
            "progress_count_display": progress_count_display,
            "stage_steps": stage_steps_for_run(ecosystem, phase, lifecycle_status),
        }

    def live_execution_row(ecosystem: str, execution: dict) -> dict:
        execution_arn = str(execution.get("executionArn") or "")
        execution_input = parse_execution_input(execution_arn) if execution_arn else {}
        state = derive_live_execution_state(ecosystem, execution, execution_input)
        status = state["lifecycle_status"]
        row = {
            "execution_id": str(execution.get("name") or ""),
            "execution_arn": execution_arn,
            "status": status,
            "source": "step-functions",
            "scan_timestamp": execution_input.get("scan_timestamp"),
            "started_at": execution.get("startDate"),
            "completed_at": execution.get("stopDate"),
            "duration_seconds": None,
            "evidence_bucket": execution_input.get("evidence_bucket") or app.config["CATALOG_BUCKET"],
            "evidence_prefix": execution_input.get("evidence_prefix") or app.config["CATALOG_PREFIX"],
            "input_bucket": execution_input.get("input_bucket"),
            "input_object_key": execution_input.get("input_object_key"),
            "current_phase": state["phase"],
            "current_phase_display": state["phase_display"],
            "phase_detail": state["phase_detail"],
            "progress_count_display": state["progress_count_display"],
            "stage_steps": state["stage_steps"],
            "platforms": [
                {
                    "platform": platform,
                    "status": status,
                    "validated": False,
                    "paths": {},
                }
                for platform in execution_platforms(ecosystem, execution_input)
            ],
        }
        return attach_input_context(ecosystem, row)

    def live_running_execution_map(ecosystem: str) -> dict[str, dict]:
        live_map: dict[str, dict] = {}
        arns = configured_state_machine_arns(ecosystem)
        for arn in arns:
            try:
                executions = stepfunctions_list_executions(
                    arn,
                    region=app.config["AWS_REGION"],
                    profile=app.config["AWS_PROFILE"],
                    status_filter="RUNNING",
                    max_results=25,
                )
            except Exception:
                continue
            for execution in executions:
                execution_id = str(execution.get("name") or "").strip()
                if execution_id:
                    live_map[execution_id] = execution
        return live_map

    def live_filtered_rows(
        ecosystem: str,
        *,
        selected_status: str,
        selected_platform: str,
        selected_validated: str,
        exclude_execution_ids: set[str],
    ) -> list[dict]:
        if selected_status not in {"RUNNING", "ANY"}:
            return []
        rows = []
        for execution_id, execution in live_running_execution_map(ecosystem).items():
            if execution_id in exclude_execution_ids:
                continue
            row = select_run_row(
                ecosystem,
                live_execution_row(ecosystem, execution),
                selected_status=selected_status,
                selected_platform=selected_platform,
                selected_validated=selected_validated,
            )
            if row is not None:
                row["source"] = "step-functions"
                rows.append(row)
        rows.sort(key=lambda item: str(item.get("started_at") or ""), reverse=True)
        return rows

    def overlay_live_status(row: dict, live_execution: dict | None) -> dict:
        if not live_execution:
            return row
        execution_arn = str(live_execution.get("executionArn") or "")
        execution_input = parse_execution_input(execution_arn) if execution_arn else {}
        state = derive_live_execution_state(row.get("ecosystem", ""), live_execution, execution_input)
        updated = dict(row)
        updated["status"] = state["lifecycle_status"]
        updated["completed_at"] = None
        updated["duration_seconds"] = None
        started = live_execution.get("startDate")
        if started:
            updated["started_at"] = started
        updated["current_phase"] = state["phase"]
        updated["current_phase_display"] = state["phase_display"]
        updated["phase_detail"] = state["phase_detail"]
        updated["progress_count_display"] = state["progress_count_display"]
        updated["stage_steps"] = state["stage_steps"]
        platforms = []
        for platform in row.get("platforms", []):
            platform_row = dict(platform)
            platform_row["status"] = state["lifecycle_status"]
            platform_row["validated"] = False
            platform_row.pop("validation_error", None)
            platform_row.pop("error", None)
            platform_row.pop("cause", None)
            platforms.append(platform_row)
        updated["platforms"] = platforms
        return updated

    def load_record_by_key(key: str) -> dict:
        now = time.time()
        cached = record_cache.get(key)
        if cached and cached["expires_at"] > now:
            return cached["record"]
        record = s3_get_json(
            app.config["CATALOG_BUCKET"],
            key,
            region=app.config["AWS_REGION"],
            profile=app.config["AWS_PROFILE"],
        )
        record_cache[key] = {
            "expires_at": now + RECORD_CACHE_TTL_SECONDS,
            "record": record,
        }
        return record

    def head_object(bucket: str, key: str) -> dict:
        cache_key = f"{bucket}/{key}"
        now = time.time()
        cached = head_cache.get(cache_key)
        if cached and cached["expires_at"] > now:
            return cached["head"]
        head = s3_head_object(
            bucket,
            key,
            region=app.config["AWS_REGION"],
            profile=app.config["AWS_PROFILE"],
        )
        head_cache[cache_key] = {
            "expires_at": now + RECORD_CACHE_TTL_SECONDS,
            "head": head,
        }
        return head

    def completed_at_for_row(row: dict) -> str | None:
        completed_at = row.get("completed_at")
        if completed_at:
            return str(completed_at)
        summary_key = str(row.get("summary_key") or "").strip()
        bucket = str(row.get("evidence_bucket") or "").strip()
        if not summary_key or not bucket:
            return None
        try:
            head = head_object(bucket, summary_key)
        except Exception:
            return None
        return str(head.get("LastModified") or "")

    def duration_for_row(row: dict) -> str | None:
        duration_seconds = row.get("duration_seconds")
        if isinstance(duration_seconds, (int, float)):
            return format_duration_seconds(duration_seconds)
        completed_at = completed_at_for_row(row)
        if not completed_at:
            return None
        return format_duration(row.get("started_at") or row.get("scan_timestamp"), completed_at)

    def platform_matches(platform_name: str, selected_platform: str) -> bool:
        return selected_platform == "all" or architecture_label(platform_name) == selected_platform

    def normalize_filters(ecosystem: str) -> tuple[str, str, str, list[str]]:
        platform_options = ["all", "linux-amd64", "windows"]
        if ecosystem == "python":
            platform_options.insert(2, "linux-arm64")
        selected_status = request.args.get("status", "SUCCEEDED").upper()
        if selected_status not in {"SUCCEEDED", "FAILED", "RUNNING", "TIMED_OUT", "ABORTED", "ANY"}:
            selected_status = "SUCCEEDED"
        selected_platform = request.args.get("platform", "linux-amd64")
        if selected_platform not in platform_options:
            selected_platform = "linux-amd64"
        default_validated = "yes" if selected_status == "SUCCEEDED" else "any"
        selected_validated = request.args.get("validated", default_validated).lower()
        if selected_validated not in {"yes", "no", "any"}:
            selected_validated = default_validated
        return selected_status, selected_platform, selected_validated, platform_options

    def select_run_row(
        ecosystem: str,
        row: dict,
        *,
        selected_status: str,
        selected_platform: str,
        selected_validated: str,
    ) -> dict | None:
        matching_platforms = [
            platform
            for platform in row.get("platforms", [])
            if platform_matches(platform.get("platform", ""), selected_platform)
        ]
        if not matching_platforms:
            return None
        matching_statuses = {platform.get("status") for platform in matching_platforms}
        if selected_status != "ANY" and selected_status not in matching_statuses:
            return None
        if selected_validated != "any":
            required = selected_validated == "yes"
            if not any(bool(platform.get("validated")) is required for platform in matching_platforms):
                return None
        preferred_platform = next(
            (
                platform
                for platform in matching_platforms
                if (selected_status == "ANY" or platform.get("status") == selected_status)
                and (
                    selected_validated == "any"
                    or bool(platform.get("validated")) is (selected_validated == "yes")
                )
            ),
            matching_platforms[0],
        )
        row = dict(row)
        row["selected_platform"] = preferred_platform
        row["selected_platform_label"] = architecture_label(preferred_platform.get("platform", ""))
        row["selected_platform_status"] = preferred_platform.get("status")
        row["selected_platform_validated"] = bool(preferred_platform.get("validated"))
        row["scan_timestamp_display"] = format_display_datetime(row.get("started_at") or row.get("scan_timestamp"))
        row["duration_display"] = duration_for_row(row)
        row["selected_platform_status_class"] = status_class(str(row.get("selected_platform_status") or ""))
        row["ecosystem_platform_badge"] = ecosystem_platform_badge(ecosystem, row.get("selected_platform_label", ""))
        row["current_phase_display"] = row.get("current_phase_display") or (
            stage_display_name("completed")
            if str(row.get("selected_platform_status") or "").upper() == "SUCCEEDED"
            else stage_display_name(row.get("current_phase"))
        )
        row["phase_detail"] = row.get("phase_detail")
        return attach_input_context(ecosystem, row)

    def normalize_cursor_state(ecosystem: str, cursor_id: str | None, filters: tuple[str, str, str, int]) -> tuple[int, str | None]:
        now = time.time()
        expired = [key for key, value in cursor_cache.items() if value["expires_at"] <= now]
        for key in expired:
            cursor_cache.pop(key, None)
        if not cursor_id:
            return 0, None
        state = cursor_cache.get(cursor_id)
        if not state:
            return 0, None
        if state["ecosystem"] != ecosystem or state["filters"] != filters:
            return 0, None
        return int(state["offset"]), state.get("prev_cursor")

    def save_cursor_state(
        ecosystem: str,
        filters: tuple[str, str, str, int],
        *,
        offset: int,
        prev_cursor: str | None,
    ) -> str:
        cursor_id = uuid.uuid4().hex
        cursor_cache[cursor_id] = {
            "ecosystem": ecosystem,
            "filters": filters,
            "offset": offset,
            "prev_cursor": prev_cursor,
            "expires_at": time.time() + CURSOR_CACHE_TTL_SECONDS,
        }
        return cursor_id

    def iter_filtered_rows(
        ecosystem: str,
        *,
        selected_status: str,
        selected_platform: str,
        selected_validated: str,
        offset: int,
    ) -> Iterator[tuple[int, dict]]:
        keys = get_listing_keys(ecosystem)
        live_map = live_running_execution_map(ecosystem)
        for index, key in enumerate(keys[offset:], start=offset):
            execution_id = key.rsplit("/", 1)[-1].replace(".json", "")
            live_execution = live_map.get(execution_id)
            if live_execution is None and execution_id:
                detail = execution_detail_for_run(ecosystem, execution_id)
                if isinstance(detail, dict) and str(detail.get("status") or "").upper() == "RUNNING":
                    live_execution = detail
            row = select_run_row(
                ecosystem,
                overlay_live_status(load_record_by_key(key), live_execution),
                selected_status=selected_status,
                selected_platform=selected_platform,
                selected_validated=selected_validated,
            )
            if row is not None:
                yield index, row

    def enrich_record(record: dict, ecosystem: str) -> dict:
        record = attach_input_context(ecosystem, record)
        for platform in record.get("platforms", []):
            paths = platform.setdefault("paths", {})
            governance_prefix = paths.get("governance_prefix")
            model_results_prefix = paths.get("model_results_prefix")
            if governance_prefix:
                paths.setdefault("vulnerability_findings_key", f"{governance_prefix}vulnerability-findings.csv")
                paths.setdefault("remediation_required_key", f"{governance_prefix}remediation-required.csv")
                paths.setdefault("remediation_exceptions_key", f"{governance_prefix}remediation-exceptions.csv")
                paths.setdefault("remediation_spreadsheet_key", f"{governance_prefix}remediation-spreadsheet.csv")
            if model_results_prefix:
                paths.setdefault("trivy_report_key", f"{model_results_prefix}trivy-sbom-report.json")
                if ecosystem == "python":
                    paths.setdefault("safety_report_key", f"{model_results_prefix}safety-report.json")
                if ecosystem == "r":
                    paths.setdefault("osv_report_key", f"{model_results_prefix}osv-report.json")
            try:
                platform["unknown_findings_count"] = len(unknown_findings_for_platform(record, platform, ecosystem))
            except Exception:
                platform["unknown_findings_count"] = None
        return record

    def unknown_findings_for_platform(record: dict, platform: dict, ecosystem: str) -> list[dict]:
        findings_rows = s3_get_rows(record["evidence_bucket"], platform["paths"]["vulnerability_findings_key"])
        unknown_rows = [row for row in findings_rows if str(row.get("severity") or "").strip().upper() == "UNKNOWN"]

        installed_index: dict[tuple[str, str], dict[str, str]] = {}
        requirements_key = (
            f"{platform['paths']['requirements_prefix']}installed-packages.csv"
            if ecosystem == "r"
            else f"{platform['paths']['requirements_prefix']}requirements.lock.txt"
        )
        if ecosystem == "r":
            for row in s3_get_rows(record["evidence_bucket"], requirements_key):
                name = str(row.get("package_name") or row.get("Package") or "").strip()
                version = str(row.get("package_version") or row.get("Version") or "").strip()
                if name and version:
                    installed_index[(name, version)] = row

        osv_findings_index: dict[tuple[str, str, str], dict] = {}
        queried_packages_index: dict[str, dict] = {}
        if ecosystem == "r" and platform["paths"].get("osv_report_key"):
            osv_report = s3_get_json_optional(record["evidence_bucket"], platform["paths"]["osv_report_key"])
            if isinstance(osv_report, dict):
                for item in osv_report.get("findings", []) or []:
                    if not isinstance(item, dict):
                        continue
                    key = (
                        str(item.get("package_name") or "").strip(),
                        str(item.get("package_version") or "").strip(),
                        str(item.get("vulnerability_id") or "").strip(),
                    )
                    osv_findings_index[key] = item
                for item in osv_report.get("queried_packages", []) or []:
                    if isinstance(item, dict):
                        queried_packages_index[str(item.get("package_name") or "").strip()] = item

        enriched: list[dict] = []
        for row in unknown_rows:
            package_name = str(row.get("package_name") or "").strip()
            package_version = str(row.get("package_version") or "").strip()
            vulnerability_id = str(row.get("vulnerability_id") or "").strip()
            osv_match = osv_findings_index.get((package_name, package_version, vulnerability_id), {})
            queried_package = queried_packages_index.get(package_name, {})
            aliases = [
                alias.strip()
                for alias in str(row.get("aliases") or osv_match.get("aliases") or "").split(";")
                if alias.strip()
            ]
            cve_id = first_cve(aliases)
            reference_url = str(row.get("reference_url") or osv_match.get("reference_url") or "").strip()
            nvd_url = str(row.get("nvd_url") or osv_match.get("nvd_url") or "").strip()
            if not nvd_url and cve_id:
                nvd_url = f"https://nvd.nist.gov/vuln/detail/{cve_id}"
            package_repo = str(
                queried_package.get("repository")
                or installed_index.get((package_name, package_version), {}).get("repository")
                or ""
            ).strip()
            ecosystem_name = str(queried_package.get("ecosystem") or "").strip()
            package_home = ""
            if package_repo.upper() == "CRAN" or ecosystem_name == "CRAN":
                package_home = cran_package_url(package_name)
            elif ecosystem_name == "Bioconductor":
                package_home = bioconductor_package_url(package_name)

            fixed_versions = [
                version.strip()
                for version in str(row.get("fixed_versions") or osv_match.get("fixed_versions") or "").split(";")
                if version.strip()
            ]
            fixed_available = str(row.get("fixed_available") or osv_match.get("fixed_available") or "").strip().lower() == "yes"
            materialized = (package_name, package_version) in installed_index if ecosystem == "r" else True
            analysis_steps = [
                {
                    "name": "Confirm package is materialized",
                    "status": "complete" if materialized else "needs-review",
                    "evidence": (
                        f"Installed package inventory contains {package_name} {package_version}."
                        if materialized
                        else "Package/version not confirmed in installed inventory."
                    ),
                    "analyst_action": "Verify the package is present in the delivered environment and not only declared in the lockfile.",
                },
                {
                    "name": "Confirm advisory identity",
                    "status": "complete" if vulnerability_id else "needs-review",
                    "evidence": f"Vulnerability ID: {vulnerability_id or 'missing'}. Aliases: {', '.join(aliases) or 'none'}",
                    "analyst_action": "Validate the advisory maps to this package/version and record any CVE aliases.",
                },
                {
                    "name": "Enrich severity from external sources",
                    "status": "ready" if reference_url or nvd_url or osv_advisory_url(vulnerability_id) else "needs-review",
                    "evidence": "Reference links were prepared for NVD/OSV/vendor sources where available.",
                    "analyst_action": "Review NVD, OSV, vendor advisories, and upstream issue trackers to determine exploitability and severity.",
                },
                {
                    "name": "Assess remediation path",
                    "status": "complete" if fixed_available else "needs-review",
                    "evidence": (
                        f"Fixed versions identified: {', '.join(fixed_versions)}."
                        if fixed_available
                        else "No fixed version was identified by the scanner outputs."
                    ),
                    "analyst_action": "Decide whether upgrade is available now or whether an exception/monitor path is required.",
                },
                {
                    "name": "Assess deployment relevance",
                    "status": "needs-review",
                    "evidence": "Scanner artifacts do not encode runtime reachability or enclave exposure context.",
                    "analyst_action": "Use system context to determine whether the vulnerable code path is reachable in the target deployment.",
                },
                {
                    "name": "Assign Cyber disposition",
                    "status": "needs-review",
                    "evidence": (
                        "Recommended initial path: upgrade."
                        if fixed_available
                        else "Recommended initial path: exception analysis."
                    ),
                    "analyst_action": "Record internal severity, rationale, compensating controls, and final disposition.",
                },
            ]
            enriched.append(
                {
                    "package_name": package_name,
                    "package_version": package_version,
                    "vulnerability_id": vulnerability_id,
                    "severity": "UNKNOWN",
                    "scanner": str(row.get("scanner") or "").strip(),
                    "title": str(row.get("title") or osv_match.get("summary") or osv_match.get("details") or "").strip(),
                    "aliases": aliases,
                    "cve_id": cve_id,
                    "reference_url": reference_url,
                    "nvd_url": nvd_url,
                    "osv_url": osv_advisory_url(vulnerability_id),
                    "package_home_url": package_home,
                    "repository": package_repo,
                    "ecosystem_name": ecosystem_name,
                    "fixed_versions": fixed_versions,
                    "fixed_available": fixed_available,
                    "materialized": materialized,
                    "recommended_disposition": "upgrade" if fixed_available else "exception-review",
                    "analysis_steps": analysis_steps,
                }
            )

        enriched.sort(key=lambda item: (item["package_name"], item["vulnerability_id"]))
        return enriched

    def create_r_upgrade_candidate_batch(
        record: dict,
        platform: dict,
        findings: list[dict],
        *,
        requested_by: str,
        rationale: str,
    ) -> dict:
        stack = describe_stack(app.config["R_STACK_NAME"])
        input_bucket = stack_output_value(stack, "InputBucketName")
        evidence_bucket = stack_output_value(stack, "EvidenceBucketName") or record["evidence_bucket"]
        ephemeral_bucket = stack_output_value(stack, "EphemeralBucketName")
        lock_token = stack_tag_value(stack, "DeploymentLockToken")
        if not input_bucket or not evidence_bucket or not ephemeral_bucket or not lock_token:
            raise RuntimeError("Required R stack outputs/tags were not available for candidate creation.")
        if not findings:
            raise RuntimeError("At least one finding must be selected for a candidate upgrade.")

        scan_timestamp = utc_compact_timestamp()
        candidate_id = f"r-upgrade-batch-{scan_timestamp}-{uuid.uuid4().hex[:8]}"
        execution_id = f"r-scan-{scan_timestamp}-{uuid.uuid4().hex[:8]}"
        package_names = "-".join(sorted({item["package_name"] for item in findings}))[:120]
        base_prefix = (
            f"{record['evidence_prefix']}/remediation-candidates/r/"
            f"{record['execution_id']}/{platform['platform']}/{package_names}/{scan_timestamp}/"
        )
        original_lock_key = f"{platform['paths']['requirements_prefix']}renv.lock"
        original_lock_bytes = s3_get_bytes(
            record["evidence_bucket"],
            original_lock_key,
            region=app.config["AWS_REGION"],
            profile=app.config["AWS_PROFILE"],
        )
        original_lock = json.loads(original_lock_bytes.decode("utf-8-sig"))
        packages = original_lock.get("Packages") or {}
        changes = []
        for finding in findings:
            package_name = finding["package_name"]
            target_version = finding["target_version"]
            if package_name not in packages:
                raise RuntimeError(f"Package {package_name} was not present in the source renv.lock.")
            current_version = str(packages[package_name].get("Version") or "").strip()
            packages[package_name]["Version"] = target_version
            changes.append(
                {
                    "package_name": package_name,
                    "vulnerability_id": finding["vulnerability_id"],
                    "title": finding["title"],
                    "scanner": finding["scanner"],
                    "aliases": finding["aliases"],
                    "reference_url": finding["reference_url"],
                    "nvd_url": finding["nvd_url"],
                    "osv_url": finding["osv_url"],
                    "from_version": current_version,
                    "to_version": target_version,
                }
            )
        candidate_lock_bytes = (json.dumps(original_lock, indent=2) + "\n").encode("utf-8")

        candidate_input_key = (
            f"inputs/r/candidates/{record['execution_id']}/{platform['platform']}/"
            f"{scan_timestamp}/renv.lock"
        )
        s3_put_bytes(
            input_bucket,
            candidate_input_key,
            candidate_lock_bytes,
            region=app.config["AWS_REGION"],
            profile=app.config["AWS_PROFILE"],
            content_type="application/json",
        )
        s3_put_bytes(
            evidence_bucket,
            f"{base_prefix}original-renv.lock",
            original_lock_bytes,
            region=app.config["AWS_REGION"],
            profile=app.config["AWS_PROFILE"],
            content_type="application/json",
        )
        s3_put_bytes(
            evidence_bucket,
            f"{base_prefix}candidate-renv.lock",
            candidate_lock_bytes,
            region=app.config["AWS_REGION"],
            profile=app.config["AWS_PROFILE"],
            content_type="application/json",
        )

        governance_summary = s3_get_json_optional(
            record["evidence_bucket"],
            platform["paths"]["governance_summary_key"],
        ) or {}
        policy = governance_summary.get("policy") or {}
        if platform["platform"] == "linux-amd64":
            platform_set = "linux-only"
            state_machine_key = "RLinuxScanOrchestrationStateMachineArn"
        elif platform["platform"] == "windows-amd64":
            platform_set = "windows-only"
            state_machine_key = "RWindowsScanOrchestrationStateMachineArn"
        else:
            platform_set = "all"
            state_machine_key = "RScanOrchestrationStateMachineArn"
        state_machine_arn = stack_output_value(stack, state_machine_key)
        execution_input = {
            "scan_execution_id": execution_id,
            "scan_timestamp": scan_timestamp,
            "input_bucket": input_bucket,
            "input_object_key": candidate_input_key,
            "evidence_bucket": evidence_bucket,
            "evidence_prefix": record["evidence_prefix"],
            "ephemeral_bucket": ephemeral_bucket,
            "ephemeral_prefix": "deploy/tmp/r",
            "remediate_medium": str(policy.get("remediate_medium", True)).lower(),
            "fail_on_medium": str(policy.get("fail_on_medium", False)).lower(),
            "remediate_unknown": str(policy.get("remediate_unknown", True)).lower(),
            "fail_on_unknown": str(policy.get("fail_on_unknown", False)).lower(),
            "r_stage_package_count": "25",
            "platform_set": platform_set,
        }
        start_data = aws_json(
            [
                "stepfunctions",
                "start-execution",
                "--state-machine-arn",
                state_machine_arn,
                "--name",
                execution_id,
                "--input",
                json.dumps(execution_input),
            ],
            region=app.config["AWS_REGION"],
            profile=app.config["AWS_PROFILE"],
        )
        audit = {
            "candidate_id": candidate_id,
            "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "created_by": requested_by,
            "rationale": rationale,
            "ecosystem": "r",
            "platform": platform["platform"],
            "platform_set": platform_set,
            "source_run": {
                "execution_id": record["execution_id"],
                "scan_timestamp": record["scan_timestamp"],
                "evidence_bucket": record["evidence_bucket"],
                "summary_key": record["summary_key"],
            },
            "finding": {
                "count": len(changes),
            },
            "changes": [
                {
                    **change,
                    "action": "upgrade-candidate-created",
                    "why": rationale,
                    "who": requested_by,
                    "lockfile_field_updated": f"Packages.{change['package_name']}.Version",
                    "note": "Only the selected package version field was changed in renv.lock. Dependency compatibility must be validated by the candidate scan.",
                }
                for change in changes
            ],
            "artifacts": {
                "source_lock_key": original_lock_key,
                "candidate_input_uri": f"s3://{input_bucket}/{candidate_input_key}",
                "audit_prefix": f"s3://{evidence_bucket}/{base_prefix}",
                "original_lock_uri": f"s3://{evidence_bucket}/{base_prefix}original-renv.lock",
                "candidate_lock_uri": f"s3://{evidence_bucket}/{base_prefix}candidate-renv.lock",
            },
            "validation_scan": {
                "stack_name": app.config["R_STACK_NAME"],
                "deployment_lock_token": lock_token,
                "state_machine_arn": state_machine_arn,
                "execution_name": execution_id,
                "execution_arn": start_data.get("executionArn"),
                "summary_uri": f"s3://{evidence_bucket}/{record['evidence_prefix']}/orchestration/r/{execution_id}/orchestration-summary.json",
                "execution_input": execution_input,
            },
            "steps": [
                {
                    "step": "source lockfile downloaded from evidence bucket",
                    "status": "completed",
                },
                {
                    "step": "selected package versions updated in renv.lock",
                    "status": "completed",
                },
                {
                    "step": "candidate renv.lock uploaded to candidate input prefix",
                    "status": "completed",
                },
                {
                    "step": f"{platform_set} validation scan started against candidate lockfile",
                    "status": "completed",
                },
                {
                    "step": "candidate scan must complete and be reviewed before package set is approved for use",
                    "status": "pending",
                },
            ],
        }
        s3_put_json(
            evidence_bucket,
            f"{base_prefix}audit.json",
            audit,
            region=app.config["AWS_REGION"],
            profile=app.config["AWS_PROFILE"],
        )
        summary_text = "\n".join(
            [
                f"CandidateId: {candidate_id}",
                f"CreatedBy: {requested_by}",
                f"Rationale: {rationale}",
                f"SourceRun: {record['execution_id']}",
                f"Platform: {platform['platform']}",
                f"ChangeCount: {len(changes)}",
                "Changes:",
                *[
                    f"- {change['package_name']}: {change['from_version']} -> {change['to_version']} ({change['vulnerability_id']})"
                    for change in changes
                ],
                f"CandidateInput: s3://{input_bucket}/{candidate_input_key}",
                f"ValidationExecution: {execution_id}",
                f"ValidationExecutionArn: {start_data.get('executionArn', '')}",
                f"ValidationSummary: s3://{evidence_bucket}/{record['evidence_prefix']}/orchestration/r/{execution_id}/orchestration-summary.json",
            ]
        )
        s3_put_bytes(
            evidence_bucket,
            f"{base_prefix}audit-summary.txt",
            summary_text.encode("utf-8"),
            region=app.config["AWS_REGION"],
            profile=app.config["AWS_PROFILE"],
            content_type="text/plain",
        )
        return audit

    def create_r_upgrade_candidate(
        record: dict,
        platform: dict,
        finding: dict,
        *,
        target_version: str,
        requested_by: str,
        rationale: str,
    ) -> dict:
        return create_r_upgrade_candidate_batch(
            record,
            platform,
            [
                {
                    **finding,
                    "target_version": target_version,
                }
            ],
            requested_by=requested_by,
            rationale=rationale,
        )

    def load_pointer(ecosystem: str, name: str) -> dict | None:
        if name == "current-approved":
            return None
        try:
            return s3_get_json(
                app.config["CATALOG_BUCKET"],
                catalog_pointer_key(app.config["CATALOG_PREFIX"], ecosystem, name),
                region=app.config["AWS_REGION"],
                profile=app.config["AWS_PROFILE"],
            )
        except Exception:
            return None

    def describe_stack(stack_name: str) -> dict:
        data = aws_json(
            ["cloudformation", "describe-stacks", "--stack-name", stack_name],
            region=app.config["AWS_REGION"],
            profile=app.config["AWS_PROFILE"],
        )
        stacks = data.get("Stacks") or []
        if not stacks:
            raise RuntimeError(f"Stack not found: {stack_name}")
        return stacks[0]

    def stack_output_value(stack: dict, key: str) -> str:
        for item in stack.get("Outputs", []) or []:
            if item.get("OutputKey") == key:
                return str(item.get("OutputValue") or "")
        return ""

    def stack_tag_value(stack: dict, key: str) -> str:
        for item in stack.get("Tags", []) or []:
            if item.get("Key") == key:
                return str(item.get("Value") or "")
        return ""

    def candidate_files() -> list[dict]:
        candidates_dir = repo_root() / "candidates"
        if not candidates_dir.exists():
            return []
        rows = []
        for path in sorted([*candidates_dir.glob("*.yml"), *candidates_dir.glob("*.yaml")]):
            rows.append(
                {
                    "name": path.stem,
                    "filename": path.name,
                    "size": path.stat().st_size,
                    "ecosystem": "python",
                }
            )
        return rows

    def active_python_candidate_runs() -> dict[str, dict]:
        active_by_candidate: dict[str, dict] = {}
        for execution in live_running_execution_map("python").values():
            execution_arn = str(execution.get("executionArn") or "")
            execution_input = parse_execution_input(execution_arn) if execution_arn else {}
            input_key = str(execution_input.get("input_object_key") or "").strip()
            marker = "inputs/python/candidates/"
            if not input_key.startswith(marker):
                continue
            remainder = input_key[len(marker):]
            candidate_name = remainder.split("/", 1)[0]
            if not candidate_name:
                continue
            active_by_candidate[candidate_name] = {
                "execution_id": execution.get("name"),
                "execution_arn": execution_arn,
                "status": execution.get("status") or "RUNNING",
                "started_at": execution.get("startDate"),
                "started_display": format_display_datetime(execution.get("startDate")),
                "input_object_key": input_key,
            }
        return active_by_candidate

    def candidate_files_with_active_state() -> list[dict]:
        active_by_candidate = active_python_candidate_runs()
        rows = []
        for candidate in candidate_files():
            row = dict(candidate)
            active_run = active_by_candidate.get(row["name"])
            row["active_run"] = active_run
            row["start_disabled"] = active_run is not None
            rows.append(row)
        return rows

    def input_artifact_group(ecosystem: str, bucket: str | None, key: str | None) -> dict:
        artifact_key = str(key or "").strip()
        artifact_bucket = str(bucket or "").strip()
        group_id = f"{artifact_bucket}/{artifact_key}" if artifact_bucket or artifact_key else f"{ecosystem}/unknown-input"
        filename = artifact_key.rsplit("/", 1)[-1] if artifact_key else "unknown input"
        candidate_name = ""
        marker = "/candidates/"
        if marker in artifact_key:
            candidate_part = artifact_key.split(marker, 1)[1]
            candidate_name = candidate_part.split("/", 1)[0]
        elif artifact_key.startswith("inputs/python/candidates/"):
            candidate_name = artifact_key.split("/", 3)[3].split("/", 1)[0]
        elif artifact_key.startswith("inputs/r/candidates/"):
            candidate_name = artifact_key.split("/", 3)[3].split("/", 1)[0]
        if candidate_name:
            label = f"{candidate_name} ({filename})"
            artifact_type = f"{ecosystem.upper()} candidate"
        elif artifact_key:
            label = filename
            artifact_type = f"{ecosystem.upper()} input"
        else:
            label = "unknown input"
            artifact_type = f"{ecosystem.upper()} input"
        return {
            "id": group_id,
            "bucket": artifact_bucket,
            "key": artifact_key,
            "uri": f"s3://{artifact_bucket}/{artifact_key}" if artifact_bucket and artifact_key else "",
            "label": label,
            "candidate_name": candidate_name,
            "artifact_type": artifact_type,
        }

    def attach_input_context(ecosystem: str, row: dict | None) -> dict | None:
        if row is None:
            return None
        updated = dict(row)
        input_bucket = updated.get("input_bucket")
        input_object_key = updated.get("input_object_key")
        if not input_bucket or not input_object_key:
            execution_id = str(updated.get("execution_id") or "").strip()
            execution_arn = str(updated.get("execution_arn") or "").strip()
            execution_input = execution_input_for_run(ecosystem, execution_id, execution_arn)
            if execution_input:
                input_bucket = input_bucket or execution_input.get("input_bucket")
                input_object_key = input_object_key or execution_input.get("input_object_key")
                updated["input_bucket"] = input_bucket
                updated["input_object_key"] = input_object_key
                if input_bucket and input_object_key and not updated.get("input_uri"):
                    updated["input_uri"] = f"s3://{input_bucket}/{input_object_key}"
        group = input_artifact_group(ecosystem, input_bucket, input_object_key)
        updated["input_group"] = group
        updated["input_label"] = str(group.get("label") or "unknown input")
        updated["input_candidate_name"] = str(group.get("candidate_name") or "")
        updated["input_uri"] = str(updated.get("input_uri") or group.get("uri") or "")
        updated["input_artifact_type"] = str(group.get("artifact_type") or f"{ecosystem.upper()} input")
        updated["input_key_display"] = str(group.get("key") or "")
        return updated

    def platform_names_from_record(row: dict) -> list[str]:
        platforms = []
        for platform in row.get("platforms", []) or []:
            platform_name = str(platform.get("platform") or "").strip()
            if platform_name:
                platforms.append(architecture_label(platform_name))
        return sorted(set(platforms))

    def catalog_run_actions(ecosystem: str, row: dict) -> list[dict[str, str]]:
        execution_id = str(row.get("execution_id") or "").strip()
        if not execution_id:
            return []
        actions = [
            {
                "label": "Details",
                "href": f"/runs/{ecosystem}/{execution_id}",
                "kind": "primary",
            }
        ]
        if any(str(platform.get("status") or "").upper() == "FAILED" for platform in row.get("platforms", []) or []):
            actions.append(
                {
                    "label": "Triage failure",
                    "href": f"/runs/{ecosystem}/{execution_id}/triage",
                    "kind": "triage",
                }
            )
        if ecosystem == "r":
            for platform in row.get("platforms", []) or []:
                platform_name = str(platform.get("platform") or "").strip()
                if not platform_name:
                    continue
                actions.append(
                    {
                        "label": f"Review/remediate UNKNOWN ({architecture_label(platform_name)})",
                        "href": f"/runs/{ecosystem}/{execution_id}/{platform_name}/unknown-findings",
                        "kind": "remediate",
                    }
                )
        return actions

    def platform_names_from_execution_input(ecosystem: str, execution_input: dict) -> list[str]:
        selected_platforms = execution_input.get("platforms")
        if isinstance(selected_platforms, list):
            platforms = [architecture_label(str(item or "").strip()) for item in selected_platforms if str(item or "").strip()]
            if platforms:
                return sorted(set(platforms))
        input_key = str(execution_input.get("input_object_key") or "").strip()
        platform_set = str(execution_input.get("platform_set") or "").strip()
        if "/windows-amd64/" in input_key or platform_set == "windows-only":
            return ["windows"]
        if "/linux-arm64/" in input_key:
            return ["linux-arm64"]
        if "/linux-amd64/" in input_key or platform_set in {"linux-only", "all"}:
            return ["linux-amd64"]
        return ["linux-amd64"] if ecosystem == "r" else ["unknown"]

    def recent_execution_history(ecosystem: str) -> list[dict]:
        arns = configured_state_machine_arns(ecosystem)
        rows: list[dict] = []
        seen: set[str] = set()
        for arn in arns:
            for status_filter in ("RUNNING", "FAILED", "TIMED_OUT", "ABORTED", "SUCCEEDED"):
                try:
                    executions = stepfunctions_list_executions(
                        arn,
                        region=app.config["AWS_REGION"],
                        profile=app.config["AWS_PROFILE"],
                        status_filter=status_filter,
                        max_results=10,
                    )
                except Exception as exc:
                    app.logger.warning(
                        "candidate history execution lookup failed ecosystem=%s state_machine=%s status=%s error=%s",
                        ecosystem,
                        arn,
                        status_filter,
                        exc,
                    )
                    continue
                for execution in executions:
                    execution_arn = str(execution.get("executionArn") or "")
                    if not execution_arn or execution_arn in seen:
                        continue
                    seen.add(execution_arn)
                    execution_input = {}
                    try:
                        detail = stepfunctions_describe_execution(
                            execution_arn,
                            region=app.config["AWS_REGION"],
                            profile=app.config["AWS_PROFILE"],
                        )
                        raw_input = detail.get("input")
                        if isinstance(raw_input, str) and raw_input.strip():
                            execution_input = json.loads(raw_input)
                        elif isinstance(raw_input, dict):
                            execution_input = raw_input
                    except Exception as exc:
                        app.logger.warning(
                            "candidate history execution detail lookup failed ecosystem=%s execution=%s error=%s",
                            ecosystem,
                            execution_arn,
                            exc,
                        )
                    if not isinstance(execution_input, dict):
                        execution_input = {}
                    rows.append(
                        {
                            "ecosystem": ecosystem,
                            "execution_id": execution.get("name"),
                            "execution_arn": execution_arn,
                            "status": execution.get("status") or status_filter,
                            "status_class": status_class(str(execution.get("status") or status_filter)),
                            "source": "step-functions",
                            "catalog_link": False,
                            "input_bucket": execution_input.get("input_bucket"),
                            "input_object_key": execution_input.get("input_object_key"),
                            "platforms": platform_names_from_execution_input(ecosystem, execution_input),
                            "started_at": execution.get("startDate"),
                            "completed_at": execution.get("stopDate"),
                            "started_display": format_display_datetime(execution.get("startDate")),
                            "completed_display": format_display_datetime(execution.get("stopDate")) if execution.get("stopDate") else "",
                            "duration_display": format_duration(execution.get("startDate"), execution.get("stopDate")),
                        }
                    )
        return rows

    def candidate_history() -> list[dict]:
        groups: dict[str, dict] = {}

        def ensure_group(ecosystem: str, bucket: str | None, key: str | None) -> dict:
            group = input_artifact_group(ecosystem, bucket, key)
            existing = groups.get(group["id"])
            if existing:
                return existing
            group["runs"] = []
            group["latest_sort"] = ""
            groups[group["id"]] = group
            return group

        for candidate in candidate_files():
            key = f"inputs/python/candidates/{candidate['name']}/environment.yml"
            group = ensure_group("python", "", key)
            group["local_filename"] = candidate["filename"]

        seen_catalog_runs: set[tuple[str, str]] = set()
        for ecosystem in ("python", "r"):
            for row in list_runs(ecosystem):
                execution_id = str(row.get("execution_id") or "")
                if execution_id:
                    seen_catalog_runs.add((ecosystem, execution_id))
                group = ensure_group(ecosystem, row.get("input_bucket"), row.get("input_object_key"))
                started_at = row.get("started_at") or row.get("scan_timestamp")
                completed_at = row.get("completed_at")
                run = {
                    "ecosystem": ecosystem,
                    "execution_id": execution_id,
                    "status": row.get("status") or "UNKNOWN",
                    "status_class": status_class(str(row.get("status") or "")),
                    "source": "catalog",
                    "catalog_link": bool(execution_id),
                    "actions": catalog_run_actions(ecosystem, row),
                    "platforms": platform_names_from_record(row),
                    "started_at": started_at,
                    "completed_at": completed_at,
                    "started_display": format_display_datetime(started_at),
                    "completed_display": format_display_datetime(completed_at) if completed_at else "",
                    "duration_display": (
                        format_duration_seconds(row.get("duration_seconds"))
                        if isinstance(row.get("duration_seconds"), (int, float))
                        else format_duration(started_at, completed_at) if completed_at else None
                    ),
                }
                group["runs"].append(run)
                group["latest_sort"] = max(str(group.get("latest_sort") or ""), str(started_at or ""))

        for ecosystem in ("python", "r"):
            for run in recent_execution_history(ecosystem):
                execution_id = str(run.get("execution_id") or "")
                if execution_id and (ecosystem, execution_id) in seen_catalog_runs:
                    continue
                group = ensure_group(ecosystem, run.get("input_bucket"), run.get("input_object_key"))
                group["runs"].append(run)
                group["latest_sort"] = max(str(group.get("latest_sort") or ""), str(run.get("started_at") or ""))

        history = list(groups.values())
        for group in history:
            group["runs"].sort(key=lambda run: str(run.get("started_at") or ""), reverse=True)
            group["run_count"] = len(group["runs"])
        history.sort(key=lambda group: (str(group.get("latest_sort") or ""), group.get("key") or ""), reverse=True)
        return history

    def checkpoint_stage_state(
        ecosystem: str,
        execution_id: str,
        platform: str,
        *,
        bucket_override: str | None = None,
    ) -> dict | None:
        bucket = str(bucket_override or app.config.get("EPHEMERAL_BUCKET") or "").strip()
        if not bucket or not execution_id or not platform:
            return None
        key = f"deploy/tmp/{ecosystem}/checkpoints/{ecosystem}/{execution_id}/{platform}/latest/stage-state.json"
        return s3_get_json_optional(bucket, key)

    def should_surface_restore_failure(
        ecosystem: str,
        execution_id: str,
        platform: str,
    ) -> bool:
        stage_state = checkpoint_stage_state(ecosystem, execution_id, platform) or {}
        current_stage = str(stage_state.get("phase") or "").strip().lower()
        return not stage_at_or_beyond(ecosystem, current_stage, "restored")

    def active_runs() -> list[dict]:
        configs = [
            ("python", configured_state_machine_arns("python")),
            ("r", configured_state_machine_arns("r")),
        ]
        active: list[dict] = []
        for ecosystem, arns in configs:
            for arn in arns:
                try:
                    executions = stepfunctions_list_executions(
                        arn,
                        region=app.config["AWS_REGION"],
                        profile=app.config["AWS_PROFILE"],
                        status_filter="RUNNING",
                        max_results=10,
                    )
                except Exception as exc:
                    app.logger.warning("active run lookup failed ecosystem=%s state_machine=%s error=%s", ecosystem, arn, exc)
                    continue
                for execution in executions:
                    row = live_execution_row(ecosystem, execution)
                    platforms = row.get("platforms") or []
                    platform = str((platforms[0] or {}).get("platform") or "") if platforms else ""
                    active.append(
                        {
                            "ecosystem": ecosystem,
                            "name": execution.get("name"),
                            "executionArn": execution.get("executionArn"),
                            "startDate": execution.get("startDate"),
                            "status": row.get("status"),
                            "platform_label": architecture_label(platform),
                            "ecosystem_platform_badge": ecosystem_platform_badge(ecosystem, platform),
                            "started_display": format_display_datetime(execution.get("startDate")),
                            "duration_display": format_duration(execution.get("startDate")),
                            "status_class": status_class(str(row.get("status") or "")),
                            "current_stage": row.get("current_phase"),
                            "current_stage_display": row.get("current_phase_display"),
                            "phase_detail": row.get("phase_detail"),
                            "progress_count_display": row.get("progress_count_display"),
                            "stage_steps": row.get("stage_steps") or [],
                            "input_label": row.get("input_label"),
                            "input_key_display": row.get("input_key_display"),
                            "stoppable": str(row.get("status") or "").upper() == "RUNNING" and bool(execution.get("executionArn")),
                        }
                    )
        active.sort(key=lambda row: str(row.get("startDate", "")), reverse=True)
        return active

    @app.post("/runs/stop")
    def stop_run():
        execution_arn = str(request.form.get("execution_arn") or "").strip()
        execution_name = str(request.form.get("execution_name") or "").strip()
        if not execution_arn or ":execution:" not in execution_arn:
            abort(400, "Valid execution ARN is required.")
        task_context = stepfunctions_execution_task_context(execution_arn)
        try:
            aws_json(
                [
                    "stepfunctions",
                    "stop-execution",
                    "--execution-arn",
                    execution_arn,
                    "--cause",
                    "Stopped from web dashboard",
                ],
                region=app.config["AWS_REGION"],
                profile=app.config["AWS_PROFILE"],
            )
            cluster = str(task_context.get("cluster") or "").strip()
            task_arn = str(task_context.get("task_arn") or "").strip()
            task_last_status = str(task_context.get("task_last_status") or "").upper()
            container_last_status = str(task_context.get("container_last_status") or "").upper()
            if cluster and task_arn and (
                task_last_status in {"RUNNING", "PENDING"} or container_last_status in {"RUNNING", "PENDING"}
            ):
                aws_json(
                    [
                        "ecs",
                        "stop-task",
                        "--cluster",
                        cluster,
                        "--task",
                        task_arn,
                        "--reason",
                        "Execution stopped from web dashboard",
                    ],
                    region=app.config["AWS_REGION"],
                    profile=app.config["AWS_PROFILE"],
                )
        except AwsAuthExpiredError:
            raise
        except Exception as exc:
            app.logger.warning("dashboard stop failed execution_arn=%s error=%s", execution_arn, exc)
            return redirect(f"/?stop_status=error&stop_execution={quote_plus(execution_name or execution_arn)}", code=302)
        return redirect(f"/?stop_status=ok&stop_execution={quote_plus(execution_name or execution_arn)}", code=302)

    def load_record(ecosystem: str, execution_id: str) -> dict:
        return s3_get_json(
            app.config["CATALOG_BUCKET"],
            catalog_run_key(app.config["CATALOG_PREFIX"], ecosystem, execution_id),
            region=app.config["AWS_REGION"],
            profile=app.config["AWS_PROFILE"],
        )

    def latest_root_cause(ecosystem: str, row: dict) -> str | None:
        platforms = row.get("platforms", [])
        failed_platform = next((p for p in platforms if p.get("status") == "FAILED"), platforms[0] if platforms else None)
        if not failed_platform:
            return None
        platform_name = str(failed_platform.get("platform") or "").strip()
        execution_id = str(row.get("execution_id") or "").strip()
        timestamp = str(row.get("scan_timestamp") or "").strip()
        if not platform_name or not timestamp:
            return None
        if execution_id and not should_surface_restore_failure(ecosystem, execution_id, platform_name):
            return None
        candidates = [
            f"{app.config['CATALOG_PREFIX'].rstrip('/')}/traceability/{ecosystem}/{platform_name}/{timestamp}/restore-root-cause.txt",
            f"deploy/tmp/r/{platform_name}/{timestamp}/restore-root-cause.txt",
        ]
        for key in candidates:
            try:
                if key.startswith("deploy/tmp/"):
                    payload = s3_get_bytes(
                        app.config["EPHEMERAL_BUCKET"],
                        key,
                        region=app.config["AWS_REGION"],
                        profile=app.config["AWS_PROFILE"],
                    )
                else:
                    payload = s3_get_bytes(
                        app.config["CATALOG_BUCKET"],
                        key,
                        region=app.config["AWS_REGION"],
                        profile=app.config["AWS_PROFILE"],
                    )
                text = payload.decode("utf-8", errors="ignore").strip()
                if text:
                    return text.splitlines()[0]
            except Exception:
                continue
        return None

    def failed_cleanup_rows() -> list[dict]:
        rows: list[dict] = []
        failure_statuses = {"FAILED", "TIMED_OUT", "ABORTED"}
        for ecosystem in ("r", "python"):
            for row in list_runs(ecosystem):
                platforms = row.get("platforms", []) or []
                platform_statuses = {
                    str(platform.get("status") or "").upper()
                    for platform in platforms
                    if platform.get("status")
                }
                row_status = str(row.get("status") or "").upper()
                if row_status not in failure_statuses and not platform_statuses.intersection(failure_statuses):
                    continue
                failed_platforms = [
                    platform
                    for platform in platforms
                    if str(platform.get("status") or "").upper() in failure_statuses
                ]
                cleanup_row = dict(row)
                cleanup_row["ecosystem"] = ecosystem
                cleanup_row["status"] = row_status or "UNKNOWN"
                cleanup_row["status_class"] = status_class(row_status)
                cleanup_row["failed_platforms"] = [
                    architecture_label(str(platform.get("platform") or "unknown")) for platform in failed_platforms
                ]
                cleanup_row["started_display"] = format_display_datetime(row.get("started_at") or row.get("scan_timestamp"))
                cleanup_row["duration_display"] = (
                    format_duration_seconds(row.get("duration_seconds"))
                    if isinstance(row.get("duration_seconds"), (int, float))
                    else duration_for_row(row)
                )
                rows.append(cleanup_row)
        rows.sort(key=lambda item: str(item.get("started_at") or item.get("scan_timestamp") or ""), reverse=True)
        return rows

    def cleanup_failed_command(ecosystem: str, execution_id: str, *, write: bool) -> list[str]:
        if ecosystem not in {"r", "python"}:
            abort(404)
        if not app.config["CATALOG_BUCKET"]:
            abort(500, "CATALOG_BUCKET is required for failed artifact cleanup.")
        if not app.config["EPHEMERAL_BUCKET"]:
            abort(500, "EPHEMERAL_BUCKET is required for failed artifact cleanup.")
        script_path = repo_root() / "scripts" / "cleanup-failed-s3-artifacts.py"
        cmd = [
            sys.executable,
            str(script_path),
            "--ecosystem",
            ecosystem,
            "--execution-id",
            execution_id,
            "--evidence-bucket",
            app.config["CATALOG_BUCKET"],
            "--ephemeral-bucket",
            app.config["EPHEMERAL_BUCKET"],
            "--evidence-prefix",
            app.config["CATALOG_PREFIX"],
            "--region",
            app.config["AWS_REGION"],
        ]
        if app.config["AWS_PROFILE"]:
            cmd.extend(["--profile", app.config["AWS_PROFILE"]])
        if write:
            cmd.append("--write")
        return cmd

    def run_failed_cleanup(ecosystem: str, execution_id: str, *, write: bool) -> dict:
        cmd = cleanup_failed_command(ecosystem, execution_id, write=write)
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(repo_root()),
                text=True,
                capture_output=True,
                timeout=900,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Failed artifact cleanup timed out after 15 minutes.") from exc
        output = "\n".join(part for part in [proc.stdout.strip(), proc.stderr.strip()] if part)
        if proc.returncode == 2 and "authentication" in output.lower():
            raise AwsAuthExpiredError(output)
        if proc.returncode != 0:
            raise RuntimeError(output or f"Cleanup command failed with exit code {proc.returncode}.")
        return {
            "command": " ".join(shlex.quote(part) for part in cmd),
            "output": output,
            "write": write,
            "execution_id": execution_id,
            "ecosystem": ecosystem,
        }

    @app.route("/")
    def index():
        auth_error = None
        stop_status = str(request.args.get("stop_status") or "").strip().lower()
        stop_execution = str(request.args.get("stop_execution") or "").strip()
        try:
            latest_r = attach_input_context("r", load_pointer("r", "latest-successful"))
            latest_python = attach_input_context("python", load_pointer("python", "latest-successful"))
            approved_r = attach_input_context("r", load_pointer("r", "current-approved"))
            approved_python = attach_input_context("python", load_pointer("python", "current-approved"))
            active = active_runs()
            def latest_failed(ecosystem: str, *, max_checks: int = 10) -> dict | None:
                """
                Find the most recent failed run without walking the entire catalog.
                We stream listing keys (already sorted newest-first) and stop early
                after the first failure or after max_checks records.
                """
                for idx, key in enumerate(get_listing_keys(ecosystem)):
                    if idx >= max_checks:
                        break
                    try:
                        row = load_record_by_key(key)
                    except Exception:
                        continue
                    if any(p.get("status") == "FAILED" for p in row.get("platforms", [])):
                        return row
                return None
            failed_r = latest_failed("r")
            failed_python = latest_failed("python")
            if failed_r:
                failed_r = attach_input_context("r", dict(failed_r))
                failed_r["root_cause"] = latest_root_cause("r", failed_r)
            if failed_python:
                failed_python = attach_input_context("python", dict(failed_python))
                failed_python["root_cause"] = latest_root_cause("python", failed_python)
        except AwsAuthExpiredError as exc:
            auth_error = str(exc)
            latest_r = None
            latest_python = None
            approved_r = None
            approved_python = None
            active = []
            failed_r = None
            failed_python = None
        return render_template(
            "index.html",
            latest_r=latest_r,
            latest_python=latest_python,
            approved_r=approved_r,
            approved_python=approved_python,
            failed_r=failed_r,
            failed_python=failed_python,
            active_runs=active,
            auth_error=auth_error,
            stop_status=stop_status,
            stop_execution=stop_execution,
        )

    @app.route("/healthz")
    def healthz():
        return jsonify({"status": "ok"}), 200

    def normalize_paging() -> int:
        per_page = request.args.get("per_page", "10")
        if per_page not in {"10", "50", "100"}:
            per_page = "10"
        return int(per_page)

    @app.route("/runs/<ecosystem>")
    def runs(ecosystem: str):
        if ecosystem not in {"r", "python"}:
            abort(404)
        auth_error = None
        selected_status, selected_platform, selected_validated, platform_options = normalize_filters(ecosystem)
        per_page = normalize_paging()
        cursor_id = request.args.get("cursor")
        filters = (selected_status, selected_platform, selected_validated, per_page)
        try:
            offset, prev_cursor = normalize_cursor_state(ecosystem, cursor_id, filters)
            rows = []
            next_offset = None
            live_execution_ids = set(live_running_execution_map(ecosystem))
            if offset == 0:
                rows.extend(
                    live_filtered_rows(
                        ecosystem,
                        selected_status=selected_status,
                        selected_platform=selected_platform,
                        selected_validated=selected_validated,
                        exclude_execution_ids=set(),
                    )
                )
            for index, row in iter_filtered_rows(
                ecosystem,
                selected_status=selected_status,
                selected_platform=selected_platform,
                selected_validated=selected_validated,
                offset=offset,
            ):
                if len(rows) == per_page:
                    next_offset = index
                    break
                if str(row.get("execution_id") or "") in live_execution_ids:
                    continue
                rows.append(row)
            for row in rows:
                row["package_count"] = package_count_for_platform(row, row["selected_platform"], ecosystem)
            next_cursor = (
                save_cursor_state(ecosystem, filters, offset=next_offset, prev_cursor=cursor_id)
                if next_offset is not None
                else None
            )
        except AwsAuthExpiredError as exc:
            auth_error = str(exc)
            rows = []
            per_page = 10
            prev_cursor = None
            next_cursor = None
        return render_template(
            "runs.html",
            ecosystem=ecosystem,
            runs=rows,
            auth_error=auth_error,
            selected_status=selected_status,
            selected_platform=selected_platform,
            selected_validated=selected_validated,
            platform_options=platform_options,
            per_page=per_page,
            current_cursor=cursor_id,
            prev_cursor=prev_cursor,
            next_cursor=next_cursor,
        )

    @app.route("/candidates")
    def candidates():
        auth_error = None
        history = []
        try:
            history = candidate_history()
        except AwsAuthExpiredError as exc:
            auth_error = str(exc)
        except Exception as exc:
            app.logger.warning("candidate history lookup failed error=%s", exc)
        return render_template(
            "candidates.html",
            candidates=candidate_files_with_active_state(),
            history=history,
            auth_error=auth_error,
        )

    @app.route("/candidates/python/<candidate_name>/start", methods=["POST"])
    def start_python_candidate(candidate_name: str):
        safe_name = candidate_name.strip()
        if not safe_name or "/" in safe_name or "\\" in safe_name:
            abort(400, "Invalid candidate name")

        candidate_path = repo_root() / "candidates" / f"{safe_name}.yml"
        if not candidate_path.exists():
            candidate_path = repo_root() / "candidates" / f"{safe_name}.yaml"
        if not candidate_path.exists():
            abort(404, "Candidate YAML not found")

        platform = request.form.get("platform", "linux-amd64")
        if platform not in {"linux-amd64", "linux-arm64", "windows-amd64"}:
            abort(400, "Unsupported Python ECS platform")

        stack = describe_stack(app.config["PYTHON_STACK_NAME"])
        input_bucket = stack_output_value(stack, "InputBucketName")
        evidence_bucket = stack_output_value(stack, "EvidenceBucketName")
        ephemeral_bucket = stack_output_value(stack, "EphemeralBucketName")
        state_machine_arn = stack_output_value(stack, "PythonScanOrchestrationStateMachineArn") or stack_output_value(stack, "PythonLinuxScanOrchestrationStateMachineArn")
        if not all([input_bucket, evidence_bucket, ephemeral_bucket, state_machine_arn]):
            abort(500, f"Python ECS stack {app.config['PYTHON_STACK_NAME']} is missing required outputs")

        timestamp = utc_compact_timestamp()
        execution_name = f"python-scan-{timestamp}-{uuid.uuid4().hex[:8]}"
        input_object_key = f"inputs/python/candidates/{safe_name}/environment.yml"
        active_run = active_python_candidate_runs().get(safe_name)
        if active_run:
            return render_template(
                "candidate_started.html",
                candidate_name=safe_name,
                platform=platform,
                execution_name=active_run.get("execution_id"),
                execution_arn=active_run.get("execution_arn"),
                input_uri=f"s3://{input_bucket}/{input_object_key}",
                summary_uri="",
                auth_error=None,
                already_running=True,
            ), 409
        s3_put_bytes(
            input_bucket,
            input_object_key,
            candidate_path.read_bytes(),
            region=app.config["AWS_REGION"],
            profile=app.config["AWS_PROFILE"],
            content_type="application/x-yaml",
        )
        execution_input = {
            "input_bucket": input_bucket,
            "input_object_key": input_object_key,
            "evidence_bucket": evidence_bucket,
            "evidence_prefix": app.config["CATALOG_PREFIX"],
            "ephemeral_bucket": ephemeral_bucket,
            "ephemeral_prefix": "deploy/tmp/python",
            "remediate_medium": request.form.get("remediate_medium", "true"),
            "fail_on_medium": request.form.get("fail_on_medium", "false"),
            "safety_api_key": "",
            "scan_timestamp": timestamp,
            "scan_execution_id": execution_name,
            "platforms": [platform],
        }
        start_data = aws_json(
            [
                "stepfunctions",
                "start-execution",
                "--state-machine-arn",
                state_machine_arn,
                "--name",
                execution_name,
                "--input",
                json.dumps(execution_input),
            ],
            region=app.config["AWS_REGION"],
            profile=app.config["AWS_PROFILE"],
        )
        return render_template(
            "candidate_started.html",
            candidate_name=safe_name,
            platform=platform,
            execution_name=execution_name,
            execution_arn=start_data.get("executionArn"),
            input_uri=f"s3://{input_bucket}/{input_object_key}",
            summary_uri=f"s3://{evidence_bucket}/{app.config['CATALOG_PREFIX']}/orchestration/python/{execution_name}/orchestration-summary.json",
            auth_error=None,
            already_running=False,
        )

    @app.route("/runs/<ecosystem>/<execution_id>")
    def run_detail(ecosystem: str, execution_id: str):
        if ecosystem not in {"r", "python"}:
            abort(404)
        auth_error = None
        verify_status_by_platform = {}
        try:
            record = load_record(ecosystem, execution_id)
        except AwsAuthExpiredError as exc:
            auth_error = str(exc)
            record = None
        except Exception:
            abort(404)
        enriched = enrich_record(record, ecosystem) if record else None
        if enriched and ecosystem == "r":
            for platform in enriched.get("platforms", []):
                platform_name = str(platform.get("platform") or "")
                if platform_name:
                    verify_status_by_platform[platform_name] = read_local_verify_status(execution_id, platform_name)
        return render_template(
            "run_detail.html",
            ecosystem=ecosystem,
            record=enriched,
            auth_error=auth_error,
            verify_status_by_platform=verify_status_by_platform,
        )

    @app.route("/verify-local-host/<ecosystem>/<execution_id>/<platform_name>", methods=["POST"])
    def verify_local_host(ecosystem: str, execution_id: str, platform_name: str):
        if ecosystem != "r":
            abort(404)
        try:
            record = enrich_record(load_record(ecosystem, execution_id), ecosystem)
        except AwsAuthExpiredError as exc:
            return render_template("download_error.html", auth_error=str(exc), key=execution_id), 401
        except Exception:
            abort(404)

        platform = next((item for item in record.get("platforms", []) if item.get("platform") == platform_name), None)
        if not platform or platform.get("status") != "SUCCEEDED":
            abort(400)

        timestamp = str(record.get("scan_timestamp") or "").strip()
        evidence_bucket = str(record.get("evidence_bucket") or "").strip()
        if not timestamp or not evidence_bucket:
            abort(400)

        existing = read_local_verify_status(execution_id, platform_name)
        if existing and existing.get("running"):
            return redirect(f"/runs/{ecosystem}/{execution_id}")

        paths = local_verify_paths(execution_id, platform_name)
        paths["base"].mkdir(parents=True, exist_ok=True)
        for key in ("log", "pid", "exit"):
            try:
                paths[key].unlink()
            except FileNotFoundError:
                pass

        work_dir = str(repo_root() / "artifacts" / "posit-restore-verification" / platform_name / timestamp)
        app_name = f"{execution_id}-{platform_name}".replace("/", "-")
        paths["meta"].write_text(
            json.dumps(
                {
                    "execution_id": execution_id,
                    "platform": platform_name,
                    "timestamp": timestamp,
                    "work_dir": work_dir,
                    "app_name": app_name,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        cmd = [
            str(repo_root() / "scripts" / "verify-posit-handoff.sh"),
            "--local-host",
            "--evidence-bucket",
            evidence_bucket,
            "--timestamp",
            timestamp,
            "--platform",
            platform_name,
            "--region",
            app.config["AWS_REGION"],
            "--app-name",
            app_name,
            "--work-dir",
            work_dir,
        ]
        if app.config.get("AWS_PROFILE"):
            cmd.extend(["--profile", app.config["AWS_PROFILE"]])

        command_line = " ".join(shlex.quote(part) for part in cmd)
        shell_command = (
            f"printf '%s\\n' {shlex.quote(command_line)} >> {shlex.quote(str(paths['log']))}; "
            f"{command_line} >> {shlex.quote(str(paths['log']))} 2>&1; "
            f"rc=$?; printf '%s\\n' \"$rc\" > {shlex.quote(str(paths['exit']))}"
        )
        process = subprocess.Popen(
            ["/bin/bash", "-lc", shell_command],
            cwd=str(repo_root()),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        paths["pid"].write_text(str(process.pid), encoding="utf-8")
        return redirect(f"/runs/{ecosystem}/{execution_id}")

    def triage_checkpoint_prefix(ecosystem: str, execution_id: str, platform: str) -> tuple[str | None, str | None]:
        bucket = resolve_triage_ephemeral_bucket(ecosystem)
        if not bucket:
            return None, None
        prefix = f"deploy/tmp/{ecosystem}/checkpoints/{ecosystem}/{execution_id}/{platform}/"
        return bucket, prefix

    def resolve_triage_ephemeral_bucket(ecosystem: str) -> str | None:
        bucket = str(app.config.get("EPHEMERAL_BUCKET") or "").strip()
        if bucket:
            return bucket
        stack_name = app.config["PYTHON_STACK_NAME"] if ecosystem == "python" else app.config["R_STACK_NAME"]
        try:
            stack = describe_stack(stack_name)
        except Exception:
            return None
        bucket = str(stack_output_value(stack, "EphemeralBucketName") or "").strip()
        return bucket or None

    def triage_platform_failure_message(platform_summary: dict | None) -> str | None:
        if not isinstance(platform_summary, dict):
            return None
        cause = str(platform_summary.get("cause") or "").strip()
        error = str(platform_summary.get("error") or "").strip()
        task_arn = platform_summary.get("task_arn")
        if task_arn:
            return None
        if "RESOURCE:MEMORY" in cause:
            return "ECS never launched the worker. This failed at placement because no container instance had enough free memory for the requested task size."
        if "RESOURCE:" in cause:
            return "ECS never launched the worker. This failed at placement before the container started."
        if error == "ECS.AmazonECSException":
            return "ECS did not start the worker task, so no checkpoint or restore logs were created."
        return None

    def list_checkpoint_keys(bucket: str, prefix: str, limit: int = 50) -> list[str]:
        keys: list[str] = []
        token = None
        while len(keys) < limit:
            page = s3_list_page(
                bucket,
                prefix,
                region=app.config["AWS_REGION"],
                profile=app.config["AWS_PROFILE"],
                max_keys=100,
                continuation_token=token,
            )
            keys.extend(page["keys"])
            if not page["is_truncated"] or len(keys) >= limit:
                break
            token = page["next_continuation_token"]
        return keys[:limit]

    @app.route("/runs/<ecosystem>/<execution_id>/triage")
    def triage_view(ecosystem: str, execution_id: str):
        if ecosystem not in {"r", "python"}:
            abort(404)
        auth_error = None
        record = None
        summary = None
        triage_platform = None
        checkpoint_keys = []
        restore_log_tail = None
        restore_log_key = None
        checkpoint_bucket = None
        placement_failure = None
        try:
            record = load_record(ecosystem, execution_id)
            summary = s3_get_json(
                record["evidence_bucket"],
                record["summary_key"],
                region=app.config["AWS_REGION"],
                profile=app.config["AWS_PROFILE"],
            )
            platforms = record.get("platforms", [])
            triage_platform = next((p for p in platforms if p.get("status") == "FAILED"), platforms[0] if platforms else None)
            summary_platforms = summary.get("platforms") if isinstance(summary, dict) else []
            summary_platform = None
            if triage_platform and isinstance(summary_platforms, list):
                summary_platform = next(
                    (p for p in summary_platforms if str(p.get("platform") or "") == str(triage_platform.get("platform") or "")),
                    summary_platforms[0] if summary_platforms else None,
                )
            placement_failure = triage_platform_failure_message(summary_platform)
            checkpoint_bucket, prefix = (triage_checkpoint_prefix(ecosystem, execution_id, triage_platform.get("platform", "")) if triage_platform else (None, None))
            if checkpoint_bucket and prefix:
                checkpoint_keys = list_checkpoint_keys(checkpoint_bucket, prefix)
                can_surface_failure = should_surface_restore_failure(ecosystem, execution_id, triage_platform.get("platform", ""))
                candidates = ["latest/restore.log"] if not can_surface_failure else ["failures/restore.log", "latest/restore.log"]
                for candidate in candidates:
                    key = f"{prefix}{candidate}"
                    try:
                        payload = s3_get_bytes(
                            checkpoint_bucket,
                            key,
                            region=app.config["AWS_REGION"],
                            profile=app.config["AWS_PROFILE"],
                        ).decode("utf-8", errors="ignore")
                        lines = payload.strip().splitlines()
                        restore_log_tail = "\n".join(lines[-120:])
                        restore_log_key = key
                        break
                    except Exception:
                        continue
        except AwsAuthExpiredError as exc:
            auth_error = str(exc)
        return render_template(
            "triage.html",
            ecosystem=ecosystem,
            execution_id=execution_id,
            record=record,
            summary=summary,
            triage_platform=triage_platform,
            checkpoint_keys=checkpoint_keys,
            checkpoint_bucket=checkpoint_bucket,
            restore_log_tail=restore_log_tail,
            restore_log_key=restore_log_key,
            placement_failure=placement_failure,
            auth_error=auth_error,
            architecture_label=architecture_label,
        )

    @app.route("/download-triage/<ecosystem>/<execution_id>/<platform_name>")
    def download_triage_bundle(ecosystem: str, execution_id: str, platform_name: str):
        if ecosystem not in {"r", "python"}:
            abort(404)
        try:
            record = load_record(ecosystem, execution_id)
            summary = s3_get_json(
                record["evidence_bucket"],
                record["summary_key"],
                region=app.config["AWS_REGION"],
                profile=app.config["AWS_PROFILE"],
            )
        except AwsAuthExpiredError as exc:
            return render_template("download_error.html", auth_error=str(exc), key=execution_id), 401
        except Exception:
            abort(404)

        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("orchestration-summary.json", json.dumps(summary, indent=2))
            checkpoint_bucket, checkpoint_prefix = triage_checkpoint_prefix(ecosystem, execution_id, platform_name)
            if checkpoint_bucket and checkpoint_prefix:
                for name in ["failures/restore.log", "failures/stage-state.json", "latest/stage-state.json"]:
                    key = f"{checkpoint_prefix}{name}"
                    try:
                        payload = s3_get_bytes(
                            checkpoint_bucket,
                            key,
                            region=app.config["AWS_REGION"],
                            profile=app.config["AWS_PROFILE"],
                        )
                        bundle.writestr(name.split("/")[-1], payload)
                    except Exception:
                        continue
        archive.seek(0)
        return send_file(
            archive,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"{execution_id}-{platform_name}-triage.zip",
        )

    @app.route("/download-bundle/<ecosystem>/<execution_id>/<platform_name>")
    def download_bundle(ecosystem: str, execution_id: str, platform_name: str):
        if ecosystem not in {"r", "python"}:
            abort(404)
        try:
            record = enrich_record(load_record(ecosystem, execution_id), ecosystem)
        except AwsAuthExpiredError as exc:
            return render_template("download_error.html", auth_error=str(exc), key=execution_id), 401
        except Exception:
            abort(404)

        platform = next((item for item in record.get("platforms", []) if item.get("platform") == platform_name), None)
        if not platform:
            abort(404)

        archive = io.BytesIO()
        try:
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                for filename, key in artifact_bundle_entries(platform):
                    try:
                        payload = s3_get_bytes(
                            record["evidence_bucket"],
                            key,
                            region=app.config["AWS_REGION"],
                            profile=app.config["AWS_PROFILE"],
                        )
                    except AwsAuthExpiredError:
                        raise
                    except Exception:
                        continue
                    bundle.writestr(filename, payload)
                for filename, path in local_bundle_script_entries(ecosystem):
                    try:
                        bundle.writestr(filename, path.read_bytes())
                    except Exception:
                        continue
        except AwsAuthExpiredError as exc:
            return render_template("download_error.html", auth_error=str(exc), key=execution_id), 401

        archive.seek(0)
        download_name = f"{execution_id}-{platform_name}-review-bundle.zip"
        return send_file(archive, as_attachment=True, download_name=download_name, mimetype="application/zip")

    @app.route("/download-posit-handoff-bundle/<execution_id>/<platform_name>")
    def download_posit_handoff_bundle(execution_id: str, platform_name: str):
        try:
            record = enrich_record(load_record("r", execution_id), "r")
        except AwsAuthExpiredError as exc:
            return render_template("download_error.html", auth_error=str(exc), key=execution_id), 401
        except Exception:
            abort(404)

        platform = next((item for item in record.get("platforms", []) if item.get("platform") == platform_name), None)
        if not platform or platform.get("status") != "SUCCEEDED":
            abort(404)

        archive = io.BytesIO()
        try:
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                for filename, key in posit_handoff_bundle_entries(platform, str(record.get("scan_timestamp") or "")):
                    try:
                        payload = s3_get_bytes(
                            record["evidence_bucket"],
                            key,
                            region=app.config["AWS_REGION"],
                            profile=app.config["AWS_PROFILE"],
                        )
                    except AwsAuthExpiredError:
                        raise
                    except Exception:
                        continue
                    bundle.writestr(filename, payload)
                for filename, path in local_bundle_script_entries("r"):
                    try:
                        bundle.writestr(filename, path.read_bytes())
                    except Exception:
                        continue
        except AwsAuthExpiredError as exc:
            return render_template("download_error.html", auth_error=str(exc), key=execution_id), 401

        archive.seek(0)
        download_name = f"{execution_id}-{platform_name}-posit-handoff-bundle.zip"
        return send_file(archive, as_attachment=True, download_name=download_name, mimetype="application/zip")

    @app.route("/download-python-handoff-bundle/<execution_id>/<platform_name>")
    def download_python_handoff_bundle(execution_id: str, platform_name: str):
        try:
            record = enrich_record(load_record("python", execution_id), "python")
        except AwsAuthExpiredError as exc:
            return render_template("download_error.html", auth_error=str(exc), key=execution_id), 401
        except Exception:
            abort(404)

        platform = next((item for item in record.get("platforms", []) if item.get("platform") == platform_name), None)
        if not platform or platform.get("status") != "SUCCEEDED":
            abort(404)

        archive = io.BytesIO()
        try:
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                for filename, key in python_handoff_bundle_entries(platform, str(record.get("scan_timestamp") or "")):
                    try:
                        payload = s3_get_bytes(
                            record["evidence_bucket"],
                            key,
                            region=app.config["AWS_REGION"],
                            profile=app.config["AWS_PROFILE"],
                        )
                    except AwsAuthExpiredError:
                        raise
                    except Exception:
                        continue
                    bundle.writestr(filename, payload)
        except AwsAuthExpiredError as exc:
            return render_template("download_error.html", auth_error=str(exc), key=execution_id), 401

        archive.seek(0)
        download_name = f"{execution_id}-{platform_name}-python-handoff-bundle.zip"
        return send_file(archive, as_attachment=True, download_name=download_name, mimetype="application/zip")

    @app.route("/runs/<ecosystem>/<execution_id>/<platform_name>/unknown-findings")
    def unknown_findings(ecosystem: str, execution_id: str, platform_name: str):
        if ecosystem not in {"r", "python"}:
            abort(404)
        auth_error = None
        form_error = request.args.get("error")
        try:
            record = enrich_record(load_record(ecosystem, execution_id), ecosystem)
            platform = next((item for item in record.get("platforms", []) if item.get("platform") == platform_name), None)
            if not platform:
                abort(404)
            findings = unknown_findings_for_platform(record, platform, ecosystem)
        except AwsAuthExpiredError as exc:
            auth_error = str(exc)
            record = None
            platform = None
            findings = []
        except Exception:
            abort(404)
        return render_template(
            "unknown_findings.html",
            ecosystem=ecosystem,
            record=record,
            platform=platform,
            findings=findings,
            auth_error=auth_error,
            form_error=form_error,
        )

    @app.route("/download-unknown-bundle/<ecosystem>/<execution_id>/<platform_name>")
    def download_unknown_bundle(ecosystem: str, execution_id: str, platform_name: str):
        if ecosystem not in {"r", "python"}:
            abort(404)
        try:
            record = enrich_record(load_record(ecosystem, execution_id), ecosystem)
        except AwsAuthExpiredError as exc:
            return render_template("download_error.html", auth_error=str(exc), key=execution_id), 401
        except Exception:
            abort(404)

        platform = next((item for item in record.get("platforms", []) if item.get("platform") == platform_name), None)
        if not platform:
            abort(404)
        findings = unknown_findings_for_platform(record, platform, ecosystem)

        archive = io.BytesIO()
        try:
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                bundle.writestr("unknown-findings-enriched.json", json.dumps(findings, indent=2))
                csv_buffer = io.StringIO()
                fieldnames = [
                    "package_name",
                    "package_version",
                    "vulnerability_id",
                    "scanner",
                    "repository",
                    "ecosystem_name",
                    "fixed_available",
                    "fixed_versions",
                    "recommended_disposition",
                    "reference_url",
                    "nvd_url",
                    "osv_url",
                    "package_home_url",
                ]
                writer = csv.DictWriter(csv_buffer, fieldnames=fieldnames)
                writer.writeheader()
                for finding in findings:
                    writer.writerow(
                        {
                            **{key: finding.get(key, "") for key in fieldnames},
                            "fixed_versions": ";".join(finding.get("fixed_versions", [])),
                            "fixed_available": "yes" if finding.get("fixed_available") else "no",
                        }
                    )
                bundle.writestr("unknown-findings-enriched.csv", csv_buffer.getvalue())

                source_keys = [
                    platform["paths"]["vulnerability_findings_key"],
                    platform["paths"]["materialization_summary_key"],
                    platform["paths"]["governance_summary_key"],
                    platform["paths"]["run_metadata_key"],
                ]
                if ecosystem == "r":
                    source_keys.extend(
                        [
                            f"{platform['paths']['requirements_prefix']}installed-packages.csv",
                            platform["paths"].get("osv_report_key"),
                        ]
                    )
                for key in [item for item in source_keys if item]:
                    try:
                        payload = s3_get_bytes(
                            record["evidence_bucket"],
                            key,
                            region=app.config["AWS_REGION"],
                            profile=app.config["AWS_PROFILE"],
                        )
                    except AwsAuthExpiredError:
                        raise
                    except Exception:
                        continue
                    bundle.writestr(f"source/{key.split('/')[-1]}", payload)
        except AwsAuthExpiredError as exc:
            return render_template("download_error.html", auth_error=str(exc), key=execution_id), 401

        archive.seek(0)
        download_name = f"{execution_id}-{platform_name}-unknown-findings-bundle.zip"
        return send_file(archive, as_attachment=True, download_name=download_name, mimetype="application/zip")

    @app.post("/upgrade-candidate/<ecosystem>/<execution_id>/<platform_name>")
    def create_upgrade_candidate(ecosystem: str, execution_id: str, platform_name: str):
        if ecosystem != "r":
            abort(404)
        requested_by = str(request.form.get("requested_by") or "").strip()
        rationale = str(request.form.get("rationale") or "").strip()
        package_name = str(request.form.get("package_name") or "").strip()
        vulnerability_id = str(request.form.get("vulnerability_id") or "").strip()
        target_version = str(request.form.get("target_version") or "").strip()
        if not requested_by or not rationale or not package_name or not vulnerability_id or not target_version:
            abort(400)

        try:
            record = enrich_record(load_record(ecosystem, execution_id), ecosystem)
            platform = next((item for item in record.get("platforms", []) if item.get("platform") == platform_name), None)
            if not platform:
                abort(404)
            finding = next(
                (
                    item
                    for item in unknown_findings_for_platform(record, platform, ecosystem)
                    if item["package_name"] == package_name and item["vulnerability_id"] == vulnerability_id
                ),
                None,
            )
            if not finding:
                abort(404)
            audit = create_r_upgrade_candidate(
                record,
                platform,
                finding,
                target_version=target_version,
                requested_by=requested_by,
                rationale=rationale,
            )
        except AwsAuthExpiredError as exc:
            return render_template("download_error.html", auth_error=str(exc), key=execution_id), 401

        return render_template(
            "upgrade_candidate_result.html",
            ecosystem=ecosystem,
            record=record,
            platform=platform,
            findings=[finding],
            audit=audit,
        )

    @app.post("/upgrade-candidates/<ecosystem>/<execution_id>/<platform_name>")
    def create_upgrade_candidates(ecosystem: str, execution_id: str, platform_name: str):
        if ecosystem != "r":
            abort(404)
        requested_by = str(request.form.get("requested_by") or "").strip()
        rationale = str(request.form.get("rationale") or "").strip()
        selected_ids = [item.strip() for item in request.form.getlist("selected_ids") if item.strip()]
        if not requested_by or not rationale or not selected_ids:
            return redirect(
                f"/runs/{ecosystem}/{execution_id}/{platform_name}/unknown-findings"
                "?error=Select at least one finding and fill in Requested By and Why before submitting."
            )

        try:
            record = enrich_record(load_record(ecosystem, execution_id), ecosystem)
            platform = next((item for item in record.get("platforms", []) if item.get("platform") == platform_name), None)
            if not platform:
                abort(404)
            findings_index = {
                f"{item['package_name']}::{item['vulnerability_id']}": item
                for item in unknown_findings_for_platform(record, platform, ecosystem)
            }
            selected_findings = []
            for selected_id in selected_ids:
                finding = findings_index.get(selected_id)
                if not finding:
                    continue
                target_version = str(request.form.get(f"target_version__{selected_id}") or "").strip()
                if not target_version:
                    continue
                selected_findings.append(
                    {
                        **finding,
                        "target_version": target_version,
                    }
                )
            if not selected_findings:
                return redirect(
                    f"/runs/{ecosystem}/{execution_id}/{platform_name}/unknown-findings"
                    "?error=No valid target versions were submitted for the selected findings."
                )
            audit = create_r_upgrade_candidate_batch(
                record,
                platform,
                selected_findings,
                requested_by=requested_by,
                rationale=rationale,
            )
        except AwsAuthExpiredError as exc:
            return render_template("download_error.html", auth_error=str(exc), key=execution_id), 401

        return render_template(
            "upgrade_candidate_result.html",
            ecosystem=ecosystem,
            record=record,
            platform=platform,
            findings=selected_findings,
            audit=audit,
        )

    @app.get("/execution-status")
    def execution_status():
        execution_arn = str(request.args.get("execution_arn") or "").strip()
        if not execution_arn:
            abort(400)
        try:
            detail = stepfunctions_describe_execution(
                execution_arn,
                region=app.config["AWS_REGION"],
                profile=app.config["AWS_PROFILE"],
            )
        except AwsAuthExpiredError as exc:
            return jsonify({"auth_error": str(exc)}), 401
        except Exception:
            abort(404)

        payload = {
            "executionArn": detail.get("executionArn"),
            "name": detail.get("name"),
            "status": detail.get("status"),
            "startDate": detail.get("startDate"),
            "stopDate": detail.get("stopDate"),
            "stateMachineArn": detail.get("stateMachineArn"),
        }
        output = detail.get("output")
        if isinstance(output, str):
            try:
                payload["output"] = json.loads(output)
            except Exception:
                payload["output"] = output
        if detail.get("error"):
            payload["error"] = detail.get("error")
        if detail.get("cause"):
            payload["cause"] = detail.get("cause")
        return jsonify(payload)

    @app.route("/cleanup/failed")
    def cleanup_failed_view():
        auth_error = None
        cleanup_error = None
        rows = []
        try:
            rows = failed_cleanup_rows()
        except AwsAuthExpiredError as exc:
            auth_error = str(exc)
        except Exception as exc:
            cleanup_error = str(exc)
        return render_template(
            "cleanup_failed.html",
            rows=rows,
            auth_error=auth_error,
            cleanup_error=cleanup_error,
            catalog_bucket=app.config["CATALOG_BUCKET"],
            ephemeral_bucket=app.config["EPHEMERAL_BUCKET"],
        )

    @app.post("/cleanup/failed/<ecosystem>/<execution_id>")
    def cleanup_failed_run(ecosystem: str, execution_id: str):
        action = str(request.form.get("action") or "preview").strip().lower()
        write = action == "delete"
        if action not in {"preview", "delete"}:
            abort(400, "Unsupported cleanup action")
        confirmation = str(request.form.get("confirm_execution_id") or "").strip()
        if write and confirmation != execution_id:
            abort(400, "Type the exact execution id to delete failed scan artifacts.")
        try:
            result = run_failed_cleanup(ecosystem, execution_id, write=write)
        except AwsAuthExpiredError as exc:
            return render_template("cleanup_failed_result.html", auth_error=str(exc), result=None, cleanup_error=None), 401
        except Exception as exc:
            return render_template("cleanup_failed_result.html", auth_error=None, result=None, cleanup_error=str(exc)), 500
        return render_template("cleanup_failed_result.html", auth_error=None, result=result, cleanup_error=None)

    @app.route("/download")
    def download():
        bucket = request.args.get("bucket") or app.config["CATALOG_BUCKET"]
        key = request.args.get("key")
        download_name = str(request.args.get("name") or "").strip()
        if not key:
            abort(400)
        if download_name:
            try:
                payload = s3_get_bytes(
                    bucket,
                    key,
                    region=app.config["AWS_REGION"],
                    profile=app.config["AWS_PROFILE"],
                    timeout=600,
                )
            except AwsAuthExpiredError as exc:
                return render_template("download_error.html", auth_error=str(exc), key=key), 401
            except Exception:
                abort(500)
            return send_file(
                io.BytesIO(payload),
                as_attachment=True,
                download_name=download_name,
                mimetype="application/octet-stream",
            )
        try:
            url = s3_presign(
                bucket,
                key,
                region=app.config["AWS_REGION"],
                profile=app.config["AWS_PROFILE"],
            )
        except AwsAuthExpiredError as exc:
            return render_template("download_error.html", auth_error=str(exc), key=key), 401
        except Exception:
            abort(500)
        return redirect(url, code=302)

    return app


app = create_app()
