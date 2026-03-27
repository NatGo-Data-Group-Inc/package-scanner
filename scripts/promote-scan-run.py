#!/usr/bin/env python3
from __future__ import annotations

import argparse

from package_scanner.catalog import catalog_pointer_key, catalog_run_key, utc_now_iso
from package_scanner.catalog_awscli import s3_get_json, s3_put_json


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--ecosystem", required=True, choices=["python", "r"])
    ap.add_argument("--execution-id", required=True)
    ap.add_argument("--approved-by", required=True)
    ap.add_argument("--prefix", default="evidence")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--profile")
    args = ap.parse_args()

    run_key = catalog_run_key(args.prefix, args.ecosystem, args.execution_id)
    record = s3_get_json(args.bucket, run_key, region=args.region, profile=args.profile)
    record["approved"] = True
    record["approved_at"] = utc_now_iso()
    record["approved_by"] = args.approved_by
    s3_put_json(args.bucket, run_key, record, region=args.region, profile=args.profile)
    s3_put_json(args.bucket, catalog_pointer_key(args.prefix, args.ecosystem, "current-approved"), record, region=args.region, profile=args.profile)
    print(f"Promoted {args.execution_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
