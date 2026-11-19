#!/usr/bin/env python3
"""Capture the exact inventory of an installed conda/Python environment in one file.

Run it where the environment lives (for example a MIP host). It reads only
what is on disk, so it works when the environment's own `conda` launcher or
`pip` is broken and when no conda is installed on the host:

  * conda packages, dependencies and requested specs from <prefix>/conda-meta
  * Python distributions from .dist-info / .egg-info metadata
  * the install history from <prefix>/conda-meta/history

Compatible with Python 3.6 (RHEL 8's /usr/libexec/platform-python) and later.
Standard library only.

    /usr/libexec/platform-python capture-environment.py <prefix> --name <name>

Output in the current directory (or --output-dir):
  <name>-capture.tar.gz   the capture
  <name>-capture.b64      the same, base64 text for copying through a terminal
and the md5 plus the chunk commands to copy it out are printed.

The archive contains the three files build-candidate-from-env-artifacts.py
reads (environment.yml, conda-list.json, requirements.txt) plus
conda-meta-records.json (dependency graph), history and capture-info.json.
"""
import argparse
import base64
import datetime
import email.parser
import glob
import gzip
import hashlib
import io
import json
import os
import re
import sys
import tarfile

TOOL_VERSION = "1"
CHANNEL_URL_PREFIXES = ("https://conda.anaconda.org/", "http://conda.anaconda.org/",
                        "https://repo.anaconda.com/", "http://repo.anaconda.com/")


def short_channel(channel):
    channel = (channel or "").rstrip("/")
    for prefix in CHANNEL_URL_PREFIXES:
        if channel.startswith(prefix):
            return channel[len(prefix):]
    return channel


def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def read_conda_meta(prefix):
    records = []
    for path in sorted(glob.glob(os.path.join(prefix, "conda-meta", "*.json"))):
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or not data.get("name"):
            continue
        records.append(data)
    return records


def site_packages_dirs(prefix):
    found = glob.glob(os.path.join(prefix, "lib", "python*", "site-packages"))
    found += glob.glob(os.path.join(prefix, "Lib", "site-packages"))  # Windows layout
    return sorted(set(found))


def read_distributions(prefix):
    """Return [(dist_dir_relative_to_prefix, name, version)] for every installed distribution."""
    parser = email.parser.Parser()
    dists = []
    for site in site_packages_dirs(prefix):
        for entry in sorted(os.listdir(site)):
            full = os.path.join(site, entry)
            if entry.endswith(".dist-info"):
                meta = os.path.join(full, "METADATA")
            elif entry.endswith(".egg-info"):
                meta = os.path.join(full, "PKG-INFO") if os.path.isdir(full) else full
            else:
                continue
            if not os.path.isfile(meta):
                continue
            with open(meta, encoding="utf-8", errors="replace") as handle:
                headers = parser.parse(handle, headersonly=True)
            name, version = headers.get("Name"), headers.get("Version")
            if name and version:
                dists.append((os.path.relpath(full, prefix), name.strip(), version.strip()))
    return dists


def conda_owned_dist_dirs(records):
    owned = set()
    for record in records:
        for path in record.get("files") or []:
            match = re.search(r"([^/]+\.(?:dist-info|egg-info))(?:/|$)", path)
            if match:
                owned.add(match.group(1))
    return owned


def build_capture(prefix, name):
    records = read_conda_meta(prefix)
    if not records:
        raise SystemExit("capture-error: no conda-meta records under {}; is this a conda environment?".format(prefix))
    dists = read_distributions(prefix)
    owned = conda_owned_dist_dirs(records)
    warnings = []

    conda_list = []
    channel_counts = {}
    for r in sorted(records, key=lambda x: x["name"]):
        channel = short_channel(r.get("channel"))
        channel_counts[channel] = channel_counts.get(channel, 0) + 1
        conda_list.append({
            "base_url": (r.get("channel") or "").rstrip("/"),
            "build_number": r.get("build_number"),
            "build_string": r.get("build"),
            "channel": channel,
            "dist_name": "{}-{}-{}".format(r["name"], r.get("version"), r.get("build")),
            "name": r["name"],
            "platform": r.get("subdir"),
            "version": r.get("version"),
        })
    conda_names = {normalize(r["name"]) for r in records}
    pip_only = []
    for rel, dist_name, version in dists:
        if os.path.basename(rel) in owned:
            continue
        if normalize(dist_name) in conda_names:
            continue
        pip_only.append((dist_name, version))
        conda_list.append({"base_url": "https://pypi.org/", "build_number": 0, "build_string": "pypi_0",
                           "channel": "pypi", "dist_name": "{}-{}-pypi_0".format(dist_name, version),
                           "name": dist_name.lower(), "platform": "pypi", "version": version})

    seen = {}
    for _, dist_name, version in dists:
        key = normalize(dist_name)
        if key in seen and seen[key][1] != version:
            warnings.append("two versions of {} installed ({} and {}); keeping {}".format(
                dist_name, seen[key][1], version, seen[key][1]))
            continue
        seen.setdefault(key, (dist_name, version))
    requirements = "".join("{}=={}\n".format(n, v) for n, v in sorted(seen.values(), key=lambda x: x[0].lower()))

    channels = [c for c, _ in sorted(channel_counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    env_lines = ["name: {}".format(name), "channels:"] + ["  - {}".format(c) for c in channels]
    env_lines.append("dependencies:")
    env_lines += ["  - {}={}={}".format(r["name"], r.get("version"), r.get("build"))
                  for r in sorted(records, key=lambda x: x["name"])]
    if pip_only:
        env_lines.append("  - pip:")
        env_lines += ["    - {}=={}".format(n, v) for n, v in sorted(pip_only, key=lambda x: x[0].lower())]
    env_lines.append("prefix: {}".format(prefix))

    graph = [{"name": r["name"], "version": r.get("version"), "build": r.get("build"),
              "channel": short_channel(r.get("channel")), "subdir": r.get("subdir"),
              "depends": r.get("depends") or [], "requested_spec": r.get("requested_spec")}
             for r in sorted(records, key=lambda x: x["name"])]

    python = next((r for r in records if r["name"] == "python"), None)
    files = {
        "conda-list.json": json.dumps(conda_list, indent=1, sort_keys=True) + "\n",
        "environment.yml": "\n".join(env_lines) + "\n",
        "requirements.txt": requirements,
        "conda-meta-records.json": json.dumps(graph, indent=1, sort_keys=True) + "\n",
    }
    history = os.path.join(prefix, "conda-meta", "history")
    if os.path.isfile(history):
        with open(history, encoding="utf-8", errors="replace") as handle:
            files["history"] = handle.read()
    else:
        warnings.append("no conda-meta/history file")
    info = {
        "tool": "capture-environment.py", "tool_version": TOOL_VERSION,
        "captured_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "name": name, "prefix": prefix,
        "python_version": python.get("version") if python else None,
        "counts": {"conda_packages": len(records), "pip_only_packages": len(pip_only),
                   "python_distributions": len(seen), "channels": channel_counts},
        "warnings": warnings,
        "sha256": {k: hashlib.sha256(v.encode("utf-8")).hexdigest() for k, v in sorted(files.items())},
    }
    files["capture-info.json"] = json.dumps(info, indent=1, sort_keys=True) + "\n"
    return files, info


def write_archive(files, path):
    """Deterministic tar.gz: fixed mtimes and owners, so the md5 depends only on content."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name in sorted(files):
            data = files[name].encode("utf-8")
            member = tarfile.TarInfo(name)
            member.size, member.mtime, member.mode = len(data), 0, 0o644
            member.uid = member.gid = 0
            member.uname = member.gname = "root"
            tar.addfile(member, io.BytesIO(data))
    with open(path, "wb") as out:
        with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0, compresslevel=9) as gz:
            gz.write(raw.getvalue())


def main(argv=None):
    parser = argparse.ArgumentParser(description="Capture an installed conda/Python environment's inventory.")
    parser.add_argument("prefix", help="Environment directory, e.g. /efs/.../envs/<env>")
    parser.add_argument("--name", required=True, help="Short name for this capture, e.g. ds-ml-cpu-20260326")
    parser.add_argument("--output-dir", default=".")
    parser.add_argument("--chunk-lines", type=int, default=30, help="Lines per copy chunk (fit your terminal)")
    args = parser.parse_args(argv)

    prefix = os.path.abspath(args.prefix)
    files, info = build_capture(prefix, args.name)
    os.makedirs(args.output_dir, exist_ok=True)
    archive = os.path.join(args.output_dir, "{}-capture.tar.gz".format(args.name))
    write_archive(files, archive)
    with open(archive, "rb") as handle:
        blob = handle.read()
    text = base64.b64encode(blob).decode("ascii")
    lines = [text[i:i + 100] for i in range(0, len(text), 100)]
    b64 = os.path.join(args.output_dir, "{}-capture.b64".format(args.name))
    with open(b64, "w") as handle:
        handle.write("\n".join(lines) + "\n")

    c = info["counts"]
    print("captured {}: python {}, {} conda packages, {} pip-only, {} Python distributions".format(
        args.name, info["python_version"], c["conda_packages"], c["pip_only_packages"], c["python_distributions"]))
    for warning in info["warnings"]:
        print("warning: " + warning)
    print("md5 {}  {}".format(hashlib.md5(blob).hexdigest(), os.path.basename(archive)))
    print("{} lines in {}; copy them out in {} chunks:".format(
        len(lines), os.path.basename(b64), (len(lines) + args.chunk_lines - 1) // args.chunk_lines))
    for start in range(1, len(lines) + 1, args.chunk_lines):
        print("  clear; sed -n '{},{}p' {}".format(start, min(start + args.chunk_lines - 1, len(lines)), os.path.basename(b64)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
