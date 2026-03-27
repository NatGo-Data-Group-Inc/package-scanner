from __future__ import annotations

import json
import subprocess
import tempfile
from typing import Any


def _base_cmd(region: str, profile: str | None) -> list[str]:
    cmd = ["aws", "--region", region]
    if profile:
        cmd.extend(["--profile", profile])
    return cmd


def aws_json(args: list[str], *, region: str, profile: str | None) -> Any:
    cmd = _base_cmd(region, profile) + args
    out = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return json.loads(out.stdout)


def s3_get_json(bucket: str, key: str, *, region: str, profile: str | None) -> dict[str, Any]:
    cmd = _base_cmd(region, profile) + ["s3", "cp", f"s3://{bucket}/{key}", "-"]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return json.loads(out.stdout)


def s3_put_json(bucket: str, key: str, payload: dict[str, Any], *, region: str, profile: str | None) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir="/tmp", suffix=".json") as fh:
        fh.write(json.dumps(payload, indent=2))
        temp_path = fh.name
    cmd = _base_cmd(region, profile) + ["s3api", "put-object", "--bucket", bucket, "--key", key, "--content-type", "application/json", "--body", temp_path]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    finally:
        subprocess.run(["rm", "-f", temp_path], check=False)


def s3_list_keys(bucket: str, prefix: str, *, region: str, profile: str | None) -> list[str]:
    paginator_args = [
        "s3api",
        "list-objects-v2",
        "--bucket",
        bucket,
        "--prefix",
        prefix,
    ]
    data = aws_json(paginator_args, region=region, profile=profile)
    return [obj["Key"] for obj in data.get("Contents", [])]
