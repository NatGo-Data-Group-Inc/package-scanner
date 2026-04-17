#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Iterable

from package_scanner.catalog import ecosystem_paths
from package_scanner.catalog_awscli import (
    AwsAuthExpiredError,
    s3_get_json,
    s3_list_page,
    stepfunctions_list_executions,
)


DEFAULT_FAILURE_STATUSES = ("ABORTED", "FAILED", "TIMED_OUT")


def aws_cmd(*, region: str, profile: str | None) -> list[str]:
    cmd = ["aws", "--region", region]
    if profile:
        cmd.extend(["--profile", profile])
    return cmd


def run_aws(args: list[str], *, region: str, profile: str | None, check: bool = True) -> subprocess.CompletedProcess[str]:
    cmd = aws_cmd(region=region, profile=profile) + args
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if check and proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        if "Error when retrieving token from sso" in stderr or "Token has expired" in stderr:
            raise AwsAuthExpiredError(stderr)
        raise RuntimeError(f"AWS CLI failed ({proc.returncode}): {' '.join(cmd)}\n{stderr}")
    return proc


def s3_uri(bucket: str, key: str) -> str:
    return f"s3://{bucket}/{key}"


def load_summary(
    *,
    evidence_bucket: str,
    evidence_prefix: str,
    ecosystem: str,
    execution_id: str,
    region: str,
    profile: str | None,
) -> dict | None:
    key = f"{evidence_prefix.rstrip('/')}/orchestration/{ecosystem}/{execution_id}/orchestration-summary.json"
    try:
      return s3_get_json(evidence_bucket, key, region=region, profile=profile)
    except subprocess.CalledProcessError:
      return None


def discover_run_metadata_matches(
    *,
    evidence_bucket: str,
    evidence_prefix: str,
    ecosystem: str,
    execution_id: str,
    region: str,
    profile: str | None,
) -> list[tuple[str, str]]:
    prefix = f"{evidence_prefix.rstrip('/')}/traceability/{ecosystem}/"
    continuation_token = None
    matches: list[tuple[str, str]] = []

    while True:
        page = s3_list_page(
            evidence_bucket,
            prefix,
            region=region,
            profile=profile,
            max_keys=1000,
            continuation_token=continuation_token,
        )
        for key in page["keys"]:
            if not key.endswith("/run-metadata.json"):
                continue
            try:
                payload = s3_get_json(evidence_bucket, key, region=region, profile=profile)
            except subprocess.CalledProcessError:
                continue
            if payload.get("scan_execution_id") != execution_id:
                continue
            platform = payload.get("platform")
            timestamp = payload.get("timestamp_utc")
            if platform and timestamp:
                matches.append((platform, timestamp))

        if not page["is_truncated"]:
            break
        continuation_token = page["next_continuation_token"]

    return matches


def unique_ordered(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def delete_targets(
    *,
    delete_keys: list[str],
    delete_prefixes: list[str],
    region: str,
    profile: str | None,
    write: bool,
) -> None:
    for uri in delete_keys:
        if write:
            print(f"DELETE {uri}")
            run_aws(["s3", "rm", uri], region=region, profile=profile)
        else:
            print(f"DRYRUN delete object {uri}")

    for uri in delete_prefixes:
        if write:
            print(f"DELETE {uri} (recursive)")
            run_aws(["s3", "rm", uri, "--recursive"], region=region, profile=profile)
        else:
            print(f"DRYRUN delete prefix {uri} (recursive)")


def execution_ids_from_state_machine(
    *,
    state_machine_arn: str,
    statuses: list[str],
    max_per_status: int,
    region: str,
    profile: str | None,
) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for status in statuses:
        for execution in stepfunctions_list_executions(
            state_machine_arn,
            region=region,
            profile=profile,
            status_filter=status,
            max_results=max_per_status,
        ):
            pairs.append((execution["name"], status))
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Delete failed scan artifacts from S3 for a specific ecosystem."
    )
    ap.add_argument("--ecosystem", required=True, choices=["python", "r"])
    ap.add_argument("--state-machine-arn", help="Source of failed execution ids when --execution-id is not provided.")
    ap.add_argument("--execution-id", action="append", default=[], help="Explicit execution id to clean. May be repeated.")
    ap.add_argument("--evidence-bucket", required=True)
    ap.add_argument("--ephemeral-bucket", required=True)
    ap.add_argument("--evidence-prefix", default="evidence")
    ap.add_argument("--ephemeral-prefix")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--profile")
    ap.add_argument("--status", action="append", dest="statuses", help="Execution status to purge. Default: ABORTED, FAILED, TIMED_OUT.")
    ap.add_argument("--max-executions-per-status", type=int, default=100)
    ap.add_argument("--write", action="store_true", help="Actually delete. Default is dry run.")
    args = ap.parse_args()

    ephemeral_prefix = args.ephemeral_prefix or f"deploy/tmp/{args.ecosystem}"
    statuses = args.statuses or list(DEFAULT_FAILURE_STATUSES)

    if not args.execution_id and not args.state_machine_arn:
        ap.error("Provide either --execution-id or --state-machine-arn.")

    execution_pairs: list[tuple[str, str]]
    if args.execution_id:
        execution_pairs = [(execution_id, "EXPLICIT") for execution_id in args.execution_id]
    else:
        execution_pairs = execution_ids_from_state_machine(
            state_machine_arn=args.state_machine_arn,
            statuses=statuses,
            max_per_status=args.max_executions_per_status,
            region=args.region,
            profile=args.profile,
        )

    if not execution_pairs:
        print("No matching executions found.")
        return 0

    total_delete_keys: list[str] = []
    total_delete_prefixes: list[str] = []

    for execution_id, source_status in execution_pairs:
        summary = load_summary(
            evidence_bucket=args.evidence_bucket,
            evidence_prefix=args.evidence_prefix,
            ecosystem=args.ecosystem,
            execution_id=execution_id,
            region=args.region,
            profile=args.profile,
        )
        if summary and summary.get("overall_status") == "SUCCEEDED":
            print(f"SKIP {execution_id}: orchestration summary says SUCCEEDED")
            continue

        print(f"PROCESS {execution_id} status={source_status}")

        total_delete_prefixes.append(
            s3_uri(
                args.ephemeral_bucket,
                f"{ephemeral_prefix.rstrip('/')}/checkpoints/{args.ecosystem}/{execution_id}/",
            )
        )
        total_delete_prefixes.append(
            s3_uri(
                args.evidence_bucket,
                f"{args.evidence_prefix.rstrip('/')}/orchestration/{args.ecosystem}/{execution_id}/",
            )
        )
        total_delete_keys.append(
            s3_uri(
                args.evidence_bucket,
                f"{args.evidence_prefix.rstrip('/')}/catalog/{args.ecosystem}/runs/{execution_id}.json",
            )
        )

        platform_timestamps: list[tuple[str, str]] = []
        if summary:
            timestamp = summary.get("scan_timestamp")
            for platform_entry in summary.get("platforms", []):
                platform = platform_entry.get("platform")
                if platform and timestamp:
                    platform_timestamps.append((platform, timestamp))
        else:
            platform_timestamps.extend(
                discover_run_metadata_matches(
                    evidence_bucket=args.evidence_bucket,
                    evidence_prefix=args.evidence_prefix,
                    ecosystem=args.ecosystem,
                    execution_id=execution_id,
                    region=args.region,
                    profile=args.profile,
                )
            )

        for platform, timestamp in platform_timestamps:
            paths = ecosystem_paths(args.evidence_prefix, args.ecosystem, platform, timestamp)
            total_delete_prefixes.extend(
                s3_uri(args.evidence_bucket, prefix)
                for prefix in (
                    paths["requirements_prefix"],
                    paths["env_artifacts_prefix"],
                    paths["model_results_prefix"],
                    paths["governance_prefix"],
                    paths["traceability_prefix"],
                    paths["offline_bundle_prefix"],
                )
            )

    delete_targets(
        delete_keys=unique_ordered(total_delete_keys),
        delete_prefixes=unique_ordered(total_delete_prefixes),
        region=args.region,
        profile=args.profile,
        write=args.write,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AwsAuthExpiredError as exc:
        print(f"AWS authentication expired: {exc}", file=sys.stderr)
        raise SystemExit(2)
