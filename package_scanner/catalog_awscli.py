from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from typing import Any


class AwsAuthExpiredError(RuntimeError):
    pass


logger = logging.getLogger(__name__)
AWS_CLI_TIMEOUT_SECONDS = int(os.environ.get("AWS_CLI_TIMEOUT_SECONDS", "30"))


def _base_cmd(region: str, profile: str | None) -> list[str]:
    cmd = ["aws", "--region", region]
    if profile:
        cmd.extend(["--profile", profile])
    return cmd


def _run_aws_cmd(
    cmd: list[str],
    *,
    text: bool,
    timeout: int = AWS_CLI_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=text,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        logger.error("AWS CLI timed out after %ss: %s", timeout, " ".join(cmd))
        raise RuntimeError(f"AWS CLI command timed out after {timeout} seconds.") from exc


def aws_json(args: list[str], *, region: str, profile: str | None) -> Any:
    cmd = _base_cmd(region, profile) + args
    try:
        out = _run_aws_cmd(cmd, text=True)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr or ""
        logger.error("AWS CLI failed: %s :: %s", " ".join(cmd), stderr.strip())
        if "Error when retrieving token from sso" in stderr or "Token has expired" in stderr:
            raise AwsAuthExpiredError("AWS SSO session expired. Reauthenticate and refresh the page.") from exc
        raise
    return json.loads(out.stdout)


def s3_get_json(bucket: str, key: str, *, region: str, profile: str | None) -> dict[str, Any]:
    cmd = _base_cmd(region, profile) + ["s3", "cp", f"s3://{bucket}/{key}", "-"]
    try:
        out = _run_aws_cmd(cmd, text=True)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr or ""
        logger.error("AWS CLI failed: %s :: %s", " ".join(cmd), stderr.strip())
        if "Error when retrieving token from sso" in stderr or "Token has expired" in stderr:
            raise AwsAuthExpiredError("AWS SSO session expired. Reauthenticate and refresh the page.") from exc
        raise
    return json.loads(out.stdout)


def s3_get_bytes(bucket: str, key: str, *, region: str, profile: str | None) -> bytes:
    cmd = _base_cmd(region, profile) + ["s3", "cp", f"s3://{bucket}/{key}", "-"]
    try:
        out = _run_aws_cmd(cmd, text=False)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode("utf-8", errors="ignore")
        logger.error("AWS CLI failed: %s :: %s", " ".join(cmd), stderr.strip())
        if "Error when retrieving token from sso" in stderr or "Token has expired" in stderr:
            raise AwsAuthExpiredError("AWS SSO session expired. Reauthenticate and refresh the page.") from exc
        raise
    return out.stdout


def s3_put_json(bucket: str, key: str, payload: dict[str, Any], *, region: str, profile: str | None) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir="/tmp", suffix=".json") as fh:
        fh.write(json.dumps(payload, indent=2))
        temp_path = fh.name
    cmd = _base_cmd(region, profile) + ["s3api", "put-object", "--bucket", bucket, "--key", key, "--content-type", "application/json", "--body", temp_path]
    try:
        _run_aws_cmd(cmd, text=True)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr or ""
        logger.error("AWS CLI failed: %s :: %s", " ".join(cmd), stderr.strip())
        if "Error when retrieving token from sso" in stderr or "Token has expired" in stderr:
            raise AwsAuthExpiredError("AWS SSO session expired. Reauthenticate and refresh the page.") from exc
        raise
    finally:
        subprocess.run(["rm", "-f", temp_path], check=False)


def s3_put_bytes(
    bucket: str,
    key: str,
    payload: bytes,
    *,
    region: str,
    profile: str | None,
    content_type: str = "application/octet-stream",
) -> None:
    with tempfile.NamedTemporaryFile("wb", delete=False, dir="/tmp") as fh:
        fh.write(payload)
        temp_path = fh.name
    cmd = _base_cmd(region, profile) + [
        "s3api",
        "put-object",
        "--bucket",
        bucket,
        "--key",
        key,
        "--content-type",
        content_type,
        "--body",
        temp_path,
    ]
    try:
        _run_aws_cmd(cmd, text=True)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr or ""
        logger.error("AWS CLI failed: %s :: %s", " ".join(cmd), stderr.strip())
        if "Error when retrieving token from sso" in stderr or "Token has expired" in stderr:
            raise AwsAuthExpiredError("AWS SSO session expired. Reauthenticate and refresh the page.") from exc
        raise
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


def s3_list_page(
    bucket: str,
    prefix: str,
    *,
    region: str,
    profile: str | None,
    max_keys: int,
    continuation_token: str | None = None,
) -> dict[str, Any]:
    args = [
        "s3api",
        "list-objects-v2",
        "--bucket",
        bucket,
        "--prefix",
        prefix,
        "--max-keys",
        str(max_keys),
    ]
    if continuation_token:
        args.extend(["--continuation-token", continuation_token])
    data = aws_json(args, region=region, profile=profile)
    return {
        "keys": [obj["Key"] for obj in data.get("Contents", [])],
        "next_continuation_token": data.get("NextContinuationToken"),
        "is_truncated": bool(data.get("IsTruncated")),
    }


def s3_head_object(bucket: str, key: str, *, region: str, profile: str | None) -> dict[str, Any]:
    return aws_json(
        ["s3api", "head-object", "--bucket", bucket, "--key", key],
        region=region,
        profile=profile,
    )


def s3_presign(bucket: str, key: str, *, region: str, profile: str | None, expires_in: int = 3600) -> str:
    cmd = _base_cmd(region, profile) + [
        "s3",
        "presign",
        f"s3://{bucket}/{key}",
        "--expires-in",
        str(expires_in),
    ]
    out = _run_aws_cmd(cmd, text=True)
    return out.stdout.strip()


def stepfunctions_list_executions(
    state_machine_arn: str,
    *,
    region: str,
    profile: str | None,
    status_filter: str | None = None,
    max_results: int = 10,
) -> list[dict[str, Any]]:
    args = [
        "stepfunctions",
        "list-executions",
        "--state-machine-arn",
        state_machine_arn,
        "--max-results",
        str(max_results),
    ]
    if status_filter:
        args.extend(["--status-filter", status_filter])
    data = aws_json(args, region=region, profile=profile)
    return data.get("executions", [])


def stepfunctions_describe_execution(
    execution_arn: str,
    *,
    region: str,
    profile: str | None,
) -> dict[str, Any]:
    return aws_json(
        ["stepfunctions", "describe-execution", "--execution-arn", execution_arn],
        region=region,
        profile=profile,
    )
