#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from package_scanner.catalog_awscli import s3_get_json, s3_list_keys, s3_put_json
from package_scanner.catalog import (
    build_catalog_record,
    catalog_pointer_key,
    catalog_run_key,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--ecosystem", required=True, choices=["python", "r"])
    ap.add_argument("--prefix", default="evidence")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--profile")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    summary_prefix = f"{args.prefix.rstrip('/')}/orchestration/{args.ecosystem}/"

    latest_successful: dict | None = None
    count = 0
    for key in s3_list_keys(args.bucket, summary_prefix, region=args.region, profile=args.profile):
        if not key.endswith("/orchestration-summary.json"):
            continue
        summary = s3_get_json(args.bucket, key, region=args.region, profile=args.profile)
        record = build_catalog_record(
            ecosystem=args.ecosystem,
            bucket=args.bucket,
            prefix=args.prefix,
            input_bucket=summary.get("input_bucket"),
            input_object_key=summary.get("input_object_key"),
            summary_key=key,
            summary=summary,
        )
        count += 1
        print(json.dumps({"execution_id": record["execution_id"], "status": record["status"], "catalog_key": catalog_run_key(args.prefix, args.ecosystem, record["execution_id"])}))
        if record["status"] == "SUCCEEDED":
            latest_successful = record
        if args.write:
            s3_put_json(args.bucket, catalog_run_key(args.prefix, args.ecosystem, record["execution_id"]), record, region=args.region, profile=args.profile)

    if args.write and latest_successful:
        s3_put_json(
            args.bucket,
            catalog_pointer_key(args.prefix, args.ecosystem, "latest-successful"),
            latest_successful,
            region=args.region,
            profile=args.profile,
        )
    print(json.dumps({"processed_runs": count, "write": args.write}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
