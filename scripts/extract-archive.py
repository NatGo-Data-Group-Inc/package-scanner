#!/usr/bin/env python3
"""Extract a tar archive into a target directory."""

import argparse
import tarfile
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", required=True)
    ap.add_argument("--destination", required=True)
    args = ap.parse_args()

    destination = Path(args.destination)
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(args.archive, "r:*") as tf:
        tf.extractall(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
