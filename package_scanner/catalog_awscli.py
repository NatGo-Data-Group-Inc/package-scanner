from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

import boto3
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    CredentialRetrievalError,
    NoAuthTokenError,
    NoCredentialsError,
    PartialCredentialsError,
    SSOError,
    SSOTokenLoadError,
    TokenRetrievalError,
    UnauthorizedSSOTokenError,
)
from botocore.config import Config


class AwsAuthExpiredError(RuntimeError):
    pass


logger = logging.getLogger(__name__)

AWS_BOTO3_CONNECT_TIMEOUT_SECONDS = 5
AWS_BOTO3_READ_TIMEOUT_SECONDS = 30

AUTH_EXCEPTIONS = (
    CredentialRetrievalError,
    NoAuthTokenError,
    NoCredentialsError,
    PartialCredentialsError,
    SSOError,
    SSOTokenLoadError,
    TokenRetrievalError,
    UnauthorizedSSOTokenError,
)

NOT_FOUND_ERROR_CODES = {
    "NoSuchKey",
    "ExecutionDoesNotExist",
}


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _normalize_response(value: Any) -> Any:
    return json.loads(json.dumps(value, default=_json_default))


def _is_auth_error_message(message: str) -> bool:
    lowered = message.lower()
    return (
        "error when retrieving token from sso" in lowered
        or "token has expired" in lowered
        or "sso session" in lowered
        or "unauthorizedssotokenerror" in lowered
        or "the sso session associated with this profile has expired" in lowered
        or "unable to locate credentials" in lowered
        or "no credentials" in lowered
    )


def _handle_aws_exception(exc: Exception) -> None:
    if isinstance(exc, AUTH_EXCEPTIONS):
        raise AwsAuthExpiredError("AWS credentials expired or unavailable. Refresh authentication and retry.") from exc
    if isinstance(exc, ClientError):
        message = str(exc)
        if _is_auth_error_message(message):
            raise AwsAuthExpiredError("AWS SSO session expired. Reauthenticate and refresh the page.") from exc
    if isinstance(exc, BotoCoreError):
        message = str(exc)
        if _is_auth_error_message(message):
            raise AwsAuthExpiredError("AWS SSO session expired. Reauthenticate and refresh the page.") from exc


def _client_error_code(exc: Exception) -> str:
    if not isinstance(exc, ClientError):
        return ""
    error = exc.response.get("Error") or {}
    return str(error.get("Code") or "").strip()


def _is_not_found_error(exc: Exception) -> bool:
    return _client_error_code(exc) in NOT_FOUND_ERROR_CODES


@lru_cache(maxsize=16)
def _session(region: str, profile: str | None):
    kwargs: dict[str, Any] = {"region_name": region}
    if profile:
        kwargs["profile_name"] = profile
    return boto3.session.Session(**kwargs)


@lru_cache(maxsize=64)
def _client(service_name: str, region: str, profile: str | None):
    session = _session(region, profile)
    return session.client(
        service_name,
        config=Config(
            connect_timeout=AWS_BOTO3_CONNECT_TIMEOUT_SECONDS,
            read_timeout=AWS_BOTO3_READ_TIMEOUT_SECONDS,
            retries={"max_attempts": 5, "mode": "standard"},
        ),
    )


def _require_flag_value(args: list[str], flag: str) -> str:
    try:
        index = args.index(flag)
    except ValueError as exc:
        raise ValueError(f"Missing required argument: {flag}") from exc
    try:
        return args[index + 1]
    except IndexError as exc:
        raise ValueError(f"Missing value for argument: {flag}") from exc


def _optional_flag_value(args: list[str], flag: str) -> str | None:
    try:
        index = args.index(flag)
    except ValueError:
        return None
    if index + 1 >= len(args):
        return None
    return args[index + 1]


def _cloudformation_describe_stacks(args: list[str], *, region: str, profile: str | None) -> dict[str, Any]:
    client = _client("cloudformation", region, profile)
    return client.describe_stacks(StackName=_require_flag_value(args, "--stack-name"))


def _ecs_describe_tasks(args: list[str], *, region: str, profile: str | None) -> dict[str, Any]:
    client = _client("ecs", region, profile)
    cluster = _require_flag_value(args, "--cluster")
    tasks_index = args.index("--tasks")
    tasks = args[tasks_index + 1 :]
    if not tasks:
        raise ValueError("Missing task ARN(s) for --tasks")
    return client.describe_tasks(cluster=cluster, tasks=tasks)


def _s3api_list_objects_v2(args: list[str], *, region: str, profile: str | None) -> dict[str, Any]:
    client = _client("s3", region, profile)
    params: dict[str, Any] = {
        "Bucket": _require_flag_value(args, "--bucket"),
        "Prefix": _require_flag_value(args, "--prefix"),
    }
    max_keys = _optional_flag_value(args, "--max-keys")
    if max_keys:
        params["MaxKeys"] = int(max_keys)
    continuation_token = _optional_flag_value(args, "--continuation-token")
    if continuation_token:
        params["ContinuationToken"] = continuation_token
    return client.list_objects_v2(**params)


def _s3api_head_object(args: list[str], *, region: str, profile: str | None) -> dict[str, Any]:
    client = _client("s3", region, profile)
    return client.head_object(
        Bucket=_require_flag_value(args, "--bucket"),
        Key=_require_flag_value(args, "--key"),
    )


def _stepfunctions_list_executions(args: list[str], *, region: str, profile: str | None) -> dict[str, Any]:
    client = _client("stepfunctions", region, profile)
    params: dict[str, Any] = {
        "stateMachineArn": _require_flag_value(args, "--state-machine-arn"),
        "maxResults": int(_optional_flag_value(args, "--max-results") or "10"),
    }
    status_filter = _optional_flag_value(args, "--status-filter")
    if status_filter:
        params["statusFilter"] = status_filter
    return client.list_executions(**params)


def _stepfunctions_describe_execution(args: list[str], *, region: str, profile: str | None) -> dict[str, Any]:
    client = _client("stepfunctions", region, profile)
    return client.describe_execution(executionArn=_require_flag_value(args, "--execution-arn"))


def _stepfunctions_get_execution_history(args: list[str], *, region: str, profile: str | None) -> dict[str, Any]:
    client = _client("stepfunctions", region, profile)
    params: dict[str, Any] = {
        "executionArn": _require_flag_value(args, "--execution-arn"),
        "maxResults": int(_optional_flag_value(args, "--max-results") or "50"),
    }
    if "--reverse-order" in args:
        params["reverseOrder"] = True
    return client.get_execution_history(**params)


def _stepfunctions_start_execution(args: list[str], *, region: str, profile: str | None) -> dict[str, Any]:
    client = _client("stepfunctions", region, profile)
    return client.start_execution(
        stateMachineArn=_require_flag_value(args, "--state-machine-arn"),
        name=_require_flag_value(args, "--name"),
        input=_require_flag_value(args, "--input"),
    )


def _stepfunctions_stop_execution(args: list[str], *, region: str, profile: str | None) -> dict[str, Any]:
    client = _client("stepfunctions", region, profile)
    params: dict[str, Any] = {"executionArn": _require_flag_value(args, "--execution-arn")}
    error = _optional_flag_value(args, "--error")
    cause = _optional_flag_value(args, "--cause")
    if error:
        params["error"] = error
    if cause:
        params["cause"] = cause
    return client.stop_execution(**params)


def aws_json(args: list[str], *, region: str, profile: str | None) -> Any:
    if len(args) < 2:
        raise ValueError("aws_json requires at least service and operation arguments")
    service, operation = args[0], args[1]
    try:
        if service == "cloudformation" and operation == "describe-stacks":
            return _normalize_response(_cloudformation_describe_stacks(args[2:], region=region, profile=profile))
        if service == "ecs" and operation == "describe-tasks":
            return _normalize_response(_ecs_describe_tasks(args[2:], region=region, profile=profile))
        if service == "s3api" and operation == "list-objects-v2":
            return _normalize_response(_s3api_list_objects_v2(args[2:], region=region, profile=profile))
        if service == "s3api" and operation == "head-object":
            return _normalize_response(_s3api_head_object(args[2:], region=region, profile=profile))
        if service == "stepfunctions" and operation == "list-executions":
            return _normalize_response(_stepfunctions_list_executions(args[2:], region=region, profile=profile))
        if service == "stepfunctions" and operation == "describe-execution":
            return _normalize_response(_stepfunctions_describe_execution(args[2:], region=region, profile=profile))
        if service == "stepfunctions" and operation == "get-execution-history":
            return _normalize_response(_stepfunctions_get_execution_history(args[2:], region=region, profile=profile))
        if service == "stepfunctions" and operation == "start-execution":
            return _normalize_response(_stepfunctions_start_execution(args[2:], region=region, profile=profile))
        if service == "stepfunctions" and operation == "stop-execution":
            return _normalize_response(_stepfunctions_stop_execution(args[2:], region=region, profile=profile))
    except Exception as exc:
        logger.error("AWS boto3 request failed service=%s operation=%s error=%s", service, operation, exc)
        _handle_aws_exception(exc)
        raise
    raise NotImplementedError(f"Unsupported aws_json operation: {service} {operation}")


def s3_get_json(bucket: str, key: str, *, region: str, profile: str | None) -> dict[str, Any]:
    try:
        body = _client("s3", region, profile).get_object(Bucket=bucket, Key=key)["Body"].read()
        return json.loads(body.decode("utf-8"))
    except Exception as exc:
        if _is_not_found_error(exc):
            logger.info("S3 get json not found bucket=%s key=%s", bucket, key)
        else:
            logger.error("S3 get json failed bucket=%s key=%s error=%s", bucket, key, exc)
        _handle_aws_exception(exc)
        raise


def s3_get_bytes(
    bucket: str,
    key: str,
    *,
    region: str,
    profile: str | None,
    timeout: int = AWS_BOTO3_READ_TIMEOUT_SECONDS,
) -> bytes:
    del timeout
    try:
        return _client("s3", region, profile).get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception as exc:
        if _is_not_found_error(exc):
            logger.info("S3 get bytes not found bucket=%s key=%s", bucket, key)
        else:
            logger.error("S3 get bytes failed bucket=%s key=%s error=%s", bucket, key, exc)
        _handle_aws_exception(exc)
        raise


def s3_put_json(bucket: str, key: str, payload: dict[str, Any], *, region: str, profile: str | None) -> None:
    try:
        _client("s3", region, profile).put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(payload, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as exc:
        logger.error("S3 put json failed bucket=%s key=%s error=%s", bucket, key, exc)
        _handle_aws_exception(exc)
        raise


def s3_put_bytes(
    bucket: str,
    key: str,
    payload: bytes,
    *,
    region: str,
    profile: str | None,
    content_type: str = "application/octet-stream",
) -> None:
    try:
        _client("s3", region, profile).put_object(
            Bucket=bucket,
            Key=key,
            Body=payload,
            ContentType=content_type,
        )
    except Exception as exc:
        logger.error("S3 put bytes failed bucket=%s key=%s error=%s", bucket, key, exc)
        _handle_aws_exception(exc)
        raise


def s3_list_keys(bucket: str, prefix: str, *, region: str, profile: str | None) -> list[str]:
    client = _client("s3", region, profile)
    try:
        paginator = client.get_paginator("list_objects_v2")
        keys: list[str] = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
        return keys
    except Exception as exc:
        logger.error("S3 list keys failed bucket=%s prefix=%s error=%s", bucket, prefix, exc)
        _handle_aws_exception(exc)
        raise


def s3_list_page(
    bucket: str,
    prefix: str,
    *,
    region: str,
    profile: str | None,
    max_keys: int,
    continuation_token: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": max_keys}
    if continuation_token:
        params["ContinuationToken"] = continuation_token
    try:
        data = _client("s3", region, profile).list_objects_v2(**params)
    except Exception as exc:
        logger.error("S3 list page failed bucket=%s prefix=%s error=%s", bucket, prefix, exc)
        _handle_aws_exception(exc)
        raise
    return {
        "keys": [obj["Key"] for obj in data.get("Contents", [])],
        "next_continuation_token": data.get("NextContinuationToken"),
        "is_truncated": bool(data.get("IsTruncated")),
    }


def s3_head_object(bucket: str, key: str, *, region: str, profile: str | None) -> dict[str, Any]:
    try:
        return _normalize_response(_client("s3", region, profile).head_object(Bucket=bucket, Key=key))
    except Exception as exc:
        logger.error("S3 head object failed bucket=%s key=%s error=%s", bucket, key, exc)
        _handle_aws_exception(exc)
        raise


def s3_presign(bucket: str, key: str, *, region: str, profile: str | None, expires_in: int = 3600) -> str:
    try:
        return _client("s3", region, profile).generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires_in,
        )
    except Exception as exc:
        logger.error("S3 presign failed bucket=%s key=%s error=%s", bucket, key, exc)
        _handle_aws_exception(exc)
        raise


def stepfunctions_list_executions(
    state_machine_arn: str,
    *,
    region: str,
    profile: str | None,
    status_filter: str | None = None,
    max_results: int = 10,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "stateMachineArn": state_machine_arn,
        "maxResults": max_results,
    }
    if status_filter:
        params["statusFilter"] = status_filter
    try:
        response = _client("stepfunctions", region, profile).list_executions(**params)
    except Exception as exc:
        logger.error("Step Functions list executions failed arn=%s error=%s", state_machine_arn, exc)
        _handle_aws_exception(exc)
        raise
    return _normalize_response(response).get("executions", [])


def stepfunctions_describe_execution(
    execution_arn: str,
    *,
    region: str,
    profile: str | None,
) -> dict[str, Any]:
    try:
        return _normalize_response(
            _client("stepfunctions", region, profile).describe_execution(executionArn=execution_arn)
        )
    except Exception as exc:
        if _is_not_found_error(exc):
            logger.info("Step Functions execution not found arn=%s", execution_arn)
        else:
            logger.error("Step Functions describe execution failed arn=%s error=%s", execution_arn, exc)
        _handle_aws_exception(exc)
        raise
