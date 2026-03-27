from __future__ import annotations

import os

from flask import Flask, abort, redirect, render_template, request

from package_scanner.catalog import catalog_pointer_key, catalog_run_key
from package_scanner.catalog_awscli import (
    s3_get_json,
    s3_list_keys,
    s3_presign,
    stepfunctions_describe_execution,
    stepfunctions_list_executions,
)


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["CATALOG_BUCKET"] = os.environ.get("CATALOG_BUCKET", "")
    app.config["CATALOG_PREFIX"] = os.environ.get("CATALOG_PREFIX", "evidence")
    app.config["AWS_REGION"] = os.environ.get("AWS_REGION", "us-east-1")
    app.config["AWS_PROFILE"] = os.environ.get("AWS_PROFILE")
    app.config["PYTHON_STATE_MACHINE_ARN"] = os.environ.get(
        "PYTHON_STATE_MACHINE_ARN",
        "arn:aws:states:us-east-1:807497180525:stateMachine:package-scanner-dev-python-ecs-linux-scan-orchestrator",
    )
    app.config["R_STATE_MACHINE_ARN"] = os.environ.get(
        "R_STATE_MACHINE_ARN",
        "arn:aws:states:us-east-1:807497180525:stateMachine:package-scanner-dev-r-ecs-linux-scan-orchestrator",
    )

    def list_runs(ecosystem: str) -> list[dict]:
        prefix = f"{app.config['CATALOG_PREFIX'].rstrip('/')}/catalog/{ecosystem}/runs/"
        rows = []
        for key in s3_list_keys(app.config["CATALOG_BUCKET"], prefix, region=app.config["AWS_REGION"], profile=app.config["AWS_PROFILE"]):
            if key.endswith(".json"):
                rows.append(s3_get_json(app.config["CATALOG_BUCKET"], key, region=app.config["AWS_REGION"], profile=app.config["AWS_PROFILE"]))
        rows.sort(key=lambda row: row.get("scan_timestamp", ""), reverse=True)
        return rows

    def enrich_record(record: dict, ecosystem: str) -> dict:
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
        return record

    def load_pointer(ecosystem: str, name: str) -> dict | None:
        try:
            return s3_get_json(
                app.config["CATALOG_BUCKET"],
                catalog_pointer_key(app.config["CATALOG_PREFIX"], ecosystem, name),
                region=app.config["AWS_REGION"],
                profile=app.config["AWS_PROFILE"],
            )
        except Exception:
            return None

    def active_runs() -> list[dict]:
        configs = [
            ("python", app.config["PYTHON_STATE_MACHINE_ARN"]),
            ("r", app.config["R_STATE_MACHINE_ARN"]),
        ]
        active: list[dict] = []
        for ecosystem, arn in configs:
            if not arn:
                continue
            try:
                executions = stepfunctions_list_executions(
                    arn,
                    region=app.config["AWS_REGION"],
                    profile=app.config["AWS_PROFILE"],
                    status_filter="RUNNING",
                    max_results=10,
                )
            except Exception:
                continue
            for execution in executions:
                item = {
                    "ecosystem": ecosystem,
                    "name": execution.get("name"),
                    "executionArn": execution.get("executionArn"),
                    "startDate": execution.get("startDate"),
                    "status": execution.get("status"),
                }
                try:
                    detail = stepfunctions_describe_execution(
                        execution["executionArn"],
                        region=app.config["AWS_REGION"],
                        profile=app.config["AWS_PROFILE"],
                    )
                    item["input"] = detail.get("input")
                except Exception:
                    item["input"] = None
                active.append(item)
        active.sort(key=lambda row: str(row.get("startDate", "")), reverse=True)
        return active

    @app.route("/")
    def index():
        return render_template(
            "index.html",
            latest_r=load_pointer("r", "latest-successful"),
            latest_python=load_pointer("python", "latest-successful"),
            approved_r=load_pointer("r", "current-approved"),
            approved_python=load_pointer("python", "current-approved"),
            active_runs=active_runs(),
        )

    @app.route("/runs/<ecosystem>")
    def runs(ecosystem: str):
        if ecosystem not in {"r", "python"}:
            abort(404)
        return render_template("runs.html", ecosystem=ecosystem, runs=list_runs(ecosystem))

    @app.route("/runs/<ecosystem>/<execution_id>")
    def run_detail(ecosystem: str, execution_id: str):
        if ecosystem not in {"r", "python"}:
            abort(404)
        try:
            record = s3_get_json(
                app.config["CATALOG_BUCKET"],
                catalog_run_key(app.config["CATALOG_PREFIX"], ecosystem, execution_id),
                region=app.config["AWS_REGION"],
                profile=app.config["AWS_PROFILE"],
            )
        except Exception:
            abort(404)
        return render_template("run_detail.html", ecosystem=ecosystem, record=enrich_record(record, ecosystem))

    @app.route("/download")
    def download():
        bucket = request.args.get("bucket") or app.config["CATALOG_BUCKET"]
        key = request.args.get("key")
        if not key:
            abort(400)
        try:
            url = s3_presign(
                bucket,
                key,
                region=app.config["AWS_REGION"],
                profile=app.config["AWS_PROFILE"],
            )
        except Exception:
            abort(500)
        return redirect(url, code=302)

    return app


app = create_app()
