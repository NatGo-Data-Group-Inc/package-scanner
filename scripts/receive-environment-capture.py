#!/usr/bin/env python3
"""Verify and unpack a capture made by capture-environment.py.

    python3 scripts/receive-environment-capture.py <name>-capture.b64 --sha256 <sha256 printed on the host> --out <dir>

Decodes the base64 text (copied out of a terminal), checks the sha256 (or md5)
against the one capture-environment.py printed, refuses anything that does not
match, unpacks safely, re-checks every file against capture-info.json, and
prints the build-candidate command to run next.
"""
import argparse
import base64
import binascii
import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

EXPECTED = {
    "capture-environment.py": {"capture-info.json", "conda-list.json", "environment.yml", "requirements.txt", "conda-meta-records.json"},
    "capture-r-library.py": {"capture-info.json", "renv.lock", "r-packages.csv", "build-skew.csv", "installed-packages.csv"},
}


class ReceiveError(Exception):
    pass


def receive(b64_path: Path, expected_digest: str, out_dir: Path, algorithm: str = "sha256") -> dict:
    text = "".join(b64_path.read_text(encoding="ascii", errors="strict").split())
    try:
        blob = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ReceiveError(f"not valid base64 ({error}); a chunk was probably cut or doubled") from error
    actual = hashlib.new(algorithm, blob).hexdigest()
    if actual != expected_digest.strip().lower():
        lines = sum(1 for line in b64_path.read_text().splitlines() if line.strip())
        raise ReceiveError(
            f"{algorithm} mismatch: got {actual}, expected {expected_digest}. {lines} non-empty lines received; "
            "compare with the line count the host printed to find the missing or doubled chunk"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            if not member.isfile() or "/" in member.name or member.name.startswith(".."):
                raise ReceiveError(f"unexpected archive member {member.name!r}")
        contents = {m.name: tar.extractfile(m).read() for m in members}
    if "capture-info.json" not in contents:
        raise ReceiveError("capture is missing capture-info.json")
    info = json.loads(contents["capture-info.json"])
    expected = EXPECTED.get(info.get("tool"))
    if expected is None:
        raise ReceiveError(f"unrecognised capture tool {info.get('tool')!r}")
    missing = expected - set(contents)
    if missing:
        raise ReceiveError(f"capture is missing {sorted(missing)}")
    for name, digest in info["sha256"].items():
        if hashlib.sha256(contents[name]).hexdigest() != digest:
            raise ReceiveError(f"{name} does not match its checksum in capture-info.json")
    for name, data in contents.items():
        (out_dir / name).write_bytes(data)
    return info


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("b64", type=Path)
    digest = parser.add_mutually_exclusive_group(required=True)
    digest.add_argument("--sha256", help="sha256 printed by the capture script on the host")
    digest.add_argument("--md5", help="md5, for captures made by older versions of the capture scripts")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        info = receive(args.b64, args.sha256 or args.md5, args.out, "sha256" if args.sha256 else "md5")
    except (ReceiveError, OSError) as error:
        print(f"receive-error: {error}", file=sys.stderr)
        return 2
    c = info["counts"]
    if info["tool"] == "capture-r-library.py":
        print(f"verified {info['name']} (captured {info['captured_at']}): R {info['running_r_version']}, "
              f"{c['effective_packages']} effective packages, {c['locked_packages']} in renv.lock, "
              f"build skew {c['build_skew']} ({c['build_skew_compiled']} with compiled code)")
        for warning in info.get("warnings", []):
            print(f"warning: {warning[:300]}")
        print(f"next (as-is scan): python3 scripts/scan-r-vulnerabilities.py --installed-packages-file {args.out}/installed-packages.csv "
              f"--lock-file {args.out}/renv.lock --out-file {args.out}/osv-report.json")
        return 0
    print(f"verified {info['name']} (captured {info['captured_at']}): python {info['python_version']}, "
          f"{c['conda_packages']} conda packages, {c['pip_only_packages']} pip-only, "
          f"{c['python_distributions']} Python distributions")
    for warning in info.get("warnings", []):
        print(f"warning: {warning}")
    out = args.out
    print("next:\n  python3 scripts/build-candidate-from-env-artifacts.py --name {n} \\\n"
          "    --environment-yml {o}/environment.yml --conda-list-json {o}/conda-list.json \\\n"
          "    --requirements-txt {o}/requirements.txt --conda-bin <conda> \\\n"
          "    --prefer-conda-available --target-subdir linux-64 --pin-versions --output {o}/{n}.yml".format(
              n=info["name"], o=out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
