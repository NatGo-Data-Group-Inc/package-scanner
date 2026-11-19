#!/usr/bin/env python3
"""Scan a reference-file submission (a folder, or a single file or archive).

    python3 scripts/scan-reference-files.py <folder-or-file> --out <evidence-dir> [--policy policy.json]

Exit status: 0 PASS, 3 REVIEW (a person must look), 4 FAIL (blocked), 2 usage error.
Writes manifest.csv, findings.csv, coverage.csv and summary.json to --out.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from package_scanner.reference_files import extract, policy as policy_mod  # noqa: E402
from package_scanner.reference_files.scan import scan_submission  # noqa: E402

EXIT = {"PASS": 0, "REVIEW": 3, "FAIL": 4}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Scan reference files bound for the MIP.")
    parser.add_argument("source", type=Path, help="Folder, file or archive to scan")
    parser.add_argument("--out", type=Path, required=True, help="Directory for the evidence files")
    parser.add_argument("--policy", type=Path, help="Policy JSON overriding actions and allowed kinds")
    parser.add_argument("--workdir", type=Path, help="Where to unpack archives (needs space for the unpacked data)")
    parser.add_argument("--max-depth", type=int, default=extract.Limits.max_depth)
    parser.add_argument("--max-total-gib", type=float, default=extract.Limits.max_total_bytes / 1024 ** 3)
    parser.add_argument("--max-ratio", type=float, default=extract.Limits.max_ratio)
    args = parser.parse_args(argv)
    if not args.source.exists():
        print(f"reference-scan-error: {args.source} does not exist", file=sys.stderr)
        return 2
    try:
        policy = policy_mod.Policy.load(args.policy)
    except (OSError, ValueError) as error:
        print(f"reference-scan-error: {error}", file=sys.stderr)
        return 2
    limits = extract.Limits(max_depth=args.max_depth, max_total_bytes=int(args.max_total_gib * 1024 ** 3), max_ratio=args.max_ratio)
    summary = scan_submission(args.source, args.out, policy=policy, limits=limits, workdir=args.workdir)

    print(f"verdict: {summary['verdict']}  ({summary['counts']['files']} files, {summary['counts']['bytes']:,} bytes)")
    print("files by verdict:", ", ".join(f"{k} {v}" for k, v in sorted(summary["files_by_verdict"].items())))
    if summary["findings_by_code"]:
        print("findings:", ", ".join(f"{k} {v}" for k, v in summary["findings_by_code"].items()))
    cov = summary["coverage"]
    if cov["files_not_fully_checked"]:
        print(f"coverage: {cov['files_not_fully_checked']} file(s) not fully checked; see coverage.csv")
    print(f"malware scanner: {summary['malware_scanner']}")
    print(f"evidence: {args.out}")
    return EXIT[summary["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
