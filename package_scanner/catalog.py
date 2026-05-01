from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def s3_uri(bucket: str, key: str) -> str:
    return f"s3://{bucket}/{key}"


def catalog_run_key(prefix: str, ecosystem: str, execution_id: str) -> str:
    return f"{prefix.rstrip('/')}/catalog/{ecosystem}/runs/{execution_id}.json"


def catalog_pointer_key(prefix: str, ecosystem: str, name: str) -> str:
    return f"{prefix.rstrip('/')}/catalog/{ecosystem}/pointers/{name}.json"


def ecosystem_paths(
    prefix: str,
    ecosystem: str,
    platform: str,
    timestamp: str,
    execution_id: str | None = None,
) -> dict[str, str]:
    root = prefix.rstrip("/")
    run_segment = f"{timestamp}/{execution_id}" if execution_id else timestamp
    return {
        "requirements_prefix": f"{root}/requirements/{ecosystem}/{platform}/{run_segment}/",
        "env_artifacts_prefix": f"{root}/env-artifacts/{ecosystem}/{platform}/{run_segment}/",
        "model_results_prefix": f"{root}/model-results/{ecosystem}/{platform}/{run_segment}/",
        "governance_prefix": f"{root}/governance/{ecosystem}/{platform}/{run_segment}/",
        "traceability_prefix": f"{root}/traceability/{ecosystem}/{platform}/{run_segment}/",
        "offline_bundle_prefix": f"{root}/packages/offline/{ecosystem}/{platform}/{run_segment}/",
    }


def build_catalog_record(
    *,
    ecosystem: str,
    bucket: str,
    prefix: str,
    input_bucket: str | None,
    input_object_key: str | None,
    summary_key: str,
    summary: dict[str, Any],
) -> dict[str, Any]:
    timestamp = summary["scan_timestamp"]
    execution_id = summary["scan_execution_id"]
    started_at = summary.get("started_at") or timestamp
    completed_at = summary.get("completed_at")
    duration_seconds = summary.get("duration_seconds")
    platforms: list[dict[str, Any]] = []
    for item in summary.get("platforms", []):
        platform = item["platform"]
        paths = ecosystem_paths(
            prefix,
            ecosystem,
            platform,
            timestamp,
            execution_id if ecosystem == "python" else None,
        )
        platforms.append(
            {
                "platform": platform,
                "status": item.get("status"),
                "validated": item.get("validated", False),
                "task_arn": item.get("task_arn"),
                "cluster_arn": item.get("cluster_arn"),
                "build_id": item.get("build_id"),
                "project_name": item.get("project_name"),
                "bundle_keys": item.get("bundle_keys", []),
                "missing": item.get("missing", []),
                "error": item.get("error"),
                "cause": item.get("cause"),
                "paths": {
                    **paths,
                    "materialization_summary_key": f"{paths['traceability_prefix']}materialization-summary.json",
                    "governance_summary_key": f"{paths['traceability_prefix']}governance-summary.json",
                    "run_metadata_key": f"{paths['traceability_prefix']}run-metadata.json",
                    "preflight_native_deps_key": f"{paths['traceability_prefix']}preflight-native-deps.json" if ecosystem == "r" else None,
                    "vulnerability_findings_key": f"{paths['governance_prefix']}vulnerability-findings.csv",
                    "remediation_required_key": f"{paths['governance_prefix']}remediation-required.csv",
                    "remediation_exceptions_key": f"{paths['governance_prefix']}remediation-exceptions.csv",
                    "remediation_spreadsheet_key": f"{paths['governance_prefix']}remediation-spreadsheet.csv",
                    "trivy_report_key": f"{paths['model_results_prefix']}trivy-sbom-report.json",
                    "safety_report_key": f"{paths['model_results_prefix']}safety-report.json" if ecosystem == "python" else None,
                    "osv_report_key": f"{paths['model_results_prefix']}osv-report.json" if ecosystem == "r" else None,
                },
            }
        )
    return {
        "schema_version": 1,
        "ecosystem": ecosystem,
        "execution_id": execution_id,
        "scan_timestamp": timestamp,
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_seconds": duration_seconds,
        "status": summary.get("overall_status"),
        "summary_key": summary_key,
        "summary_uri": s3_uri(bucket, summary_key),
        "evidence_bucket": bucket,
        "evidence_prefix": prefix.rstrip("/"),
        "input_bucket": input_bucket,
        "input_object_key": input_object_key,
        "input_uri": s3_uri(input_bucket, input_object_key) if input_bucket and input_object_key else None,
        "platforms": platforms,
        "platform_count": len(platforms),
        "validated_platforms": len([p for p in platforms if p.get("validated")]),
        "created_at": utc_now_iso(),
        "approved": False,
        "approved_at": None,
        "approved_by": None,
    }


def load_s3_json(s3_client: Any, bucket: str, key: str) -> dict[str, Any]:
    body = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()
    return json.loads(body)


def put_s3_json(s3_client: Any, bucket: str, key: str, payload: dict[str, Any]) -> None:
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(payload, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
