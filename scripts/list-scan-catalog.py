#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from package_scanner.catalog_awscli import s3_get_json, s3_list_keys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--ecosystem", required=True, choices=["python", "r"])
    ap.add_argument("--prefix", default="evidence")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--profile")
    ap.add_argument("--limit", type=int, default=25)
    args = ap.parse_args()

    catalog_prefix = f"{args.prefix.rstrip('/')}/catalog/{args.ecosystem}/runs/"
    rows = []
    for key in s3_list_keys(args.bucket, catalog_prefix, region=args.region, profile=args.profile):
        if key.endswith(".json"):
            rows.append(s3_get_json(args.bucket, key, region=args.region, profile=args.profile))
    rows.sort(key=lambda row: row.get("scan_timestamp", ""), reverse=True)
    for row in rows[: args.limit]:
        print(
            json.dumps(
                {
                    "execution_id": row["execution_id"],
                    "status": row["status"],
                    "scan_timestamp": row["scan_timestamp"],
                    "validated_platforms": row["validated_platforms"],
                    "platform_count": row["platform_count"],
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
