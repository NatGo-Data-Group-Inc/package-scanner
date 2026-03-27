from __future__ import annotations

import os

from flask import Flask, abort, redirect, render_template, request

from package_scanner.catalog import catalog_pointer_key, catalog_run_key
from package_scanner.catalog_awscli import s3_get_json, s3_list_keys, s3_presign


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["CATALOG_BUCKET"] = os.environ.get("CATALOG_BUCKET", "")
    app.config["CATALOG_PREFIX"] = os.environ.get("CATALOG_PREFIX", "evidence")
    app.config["AWS_REGION"] = os.environ.get("AWS_REGION", "us-east-1")
    app.config["AWS_PROFILE"] = os.environ.get("AWS_PROFILE")

    def list_runs(ecosystem: str) -> list[dict]:
        prefix = f"{app.config['CATALOG_PREFIX'].rstrip('/')}/catalog/{ecosystem}/runs/"
        rows = []
        for key in s3_list_keys(app.config["CATALOG_BUCKET"], prefix, region=app.config["AWS_REGION"], profile=app.config["AWS_PROFILE"]):
            if key.endswith(".json"):
                rows.append(s3_get_json(app.config["CATALOG_BUCKET"], key, region=app.config["AWS_REGION"], profile=app.config["AWS_PROFILE"]))
        rows.sort(key=lambda row: row.get("scan_timestamp", ""), reverse=True)
        return rows

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

    @app.route("/")
    def index():
        return render_template(
            "index.html",
            latest_r=load_pointer("r", "latest-successful"),
            latest_python=load_pointer("python", "latest-successful"),
            approved_r=load_pointer("r", "current-approved"),
            approved_python=load_pointer("python", "current-approved"),
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
        return render_template("run_detail.html", ecosystem=ecosystem, record=record)

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
