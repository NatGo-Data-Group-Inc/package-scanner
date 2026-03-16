#!/usr/bin/env python3
import argparse
import hashlib
import tarfile
from pathlib import Path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--output-file", required=True)
    parser.add_argument("--checksum-file", required=True)
    args = parser.parse_args()

    source_dir = Path(args.source_dir)
    output_file = Path(args.output_file)
    checksum_file = Path(args.checksum_file)

    if not source_dir.is_dir():
        raise SystemExit(f"Source directory not found: {source_dir}")

    output_file.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output_file, "w:gz") as tar:
        tar.add(source_dir, arcname=".")

    checksum = sha256_file(output_file)
    checksum_file.write_text(f"{checksum}  {output_file.name}\n", encoding="utf-8")


if __name__ == "__main__":
    main()
