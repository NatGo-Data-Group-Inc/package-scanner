#!/usr/bin/env python3
"""Capture the exact inventory of installed R package libraries in one file.

Run it where the libraries live (for example a MIP Posit host). It reads each
package's DESCRIPTION file directly, so it needs neither R nor network access.
Python 3.6+ (RHEL 8's /usr/libexec/platform-python), standard library only.

    /usr/libexec/platform-python capture-r-library.py <libpath> [<libpath> ...] \
        --name <name> --r-version 4.4.0

Give library paths in .libPaths() order: when a package is in several, the
first one wins, as it does in R.

Output in the current directory (or --output-dir):
  <name>-capture.tar.gz / .b64, plus its sha256.
The archive contains:
  renv.lock            the effective library as an renv lockfile (the R pipeline's input)
  r-packages.csv       every package in every library, including shadowed copies
  build-skew.csv       packages built under a different R minor version than --r-version
  installed-packages.csv  every distinct package version on disk (the vulnerability scan's input)
  capture-info.json    counts, warnings, per-file sha256
"""
import argparse
import base64
import csv
import datetime
import gzip
import hashlib
import io
import json
import os
import re
import sys
import tarfile

TOOL_VERSION = "2"
DEP_FIELDS = ("Depends", "Imports", "LinkingTo")
KEEP_FIELDS = ("Package", "Version", "Priority", "Repository", "NeedsCompilation", "Built", "License", "Title",
               "biocViews", "RemoteType", "RemoteHost", "RemoteRepo", "RemoteUsername", "RemoteRef", "RemoteSha",
               "RemoteUrl", "Packaged") + DEP_FIELDS


def parse_dcf(text):
    """Parse an R DESCRIPTION (Debian control format): Key: value, continuation lines indented."""
    fields, key = {}, None
    for line in text.splitlines():
        if not line.strip():
            continue
        if line[0] in " \t" and key:
            fields[key] += "\n" + line.strip()
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            fields[key] = value.strip()
    return fields


def dep_names(value):
    names = []
    for part in (value or "").replace("\n", " ").split(","):
        name = re.split(r"[\s(]", part.strip(), maxsplit=1)[0]
        if name and name != "R":
            names.append(name)
    return names


def built_info(built):
    """'R 4.1.3; x86_64-pc-linux-gnu; 2023-05-01 12:00:00 UTC; unix' -> ('4.1.3', 'x86_64-pc-linux-gnu', '2023-05-01 ...')."""
    parts = [p.strip() for p in (built or "").split(";")]
    version = parts[0][2:].strip() if parts and parts[0].startswith("R ") else ""
    platform = parts[1] if len(parts) > 1 else ""
    when = parts[2] if len(parts) > 2 else ""
    return version, platform, when


def minor(version):
    return ".".join(version.split(".")[:2]) if version else ""


def source_of(desc):
    remote = (desc.get("RemoteType") or "").lower()
    if remote in ("github", "gitlab", "bitbucket", "git", "url", "local"):
        return remote.capitalize() if remote != "url" else "URL"
    if desc.get("biocViews") or (desc.get("Repository") or "").lower().startswith("bioc"):
        return "Bioconductor"
    if desc.get("Repository"):
        return "Repository"
    return "Unknown"


def read_library(libpath):
    packages = []
    try:
        entries = sorted(os.listdir(libpath))
    except OSError as error:
        return packages, ["cannot read library {}: {}".format(libpath, error)]
    warnings = []
    hidden = [e for e in entries if e.startswith(".")]
    if hidden:
        # renv keeps backups such as .renv-backup-42-jsonlite... inside the library;
        # R never loads hidden folders, so neither does the capture.
        warnings.append("{}: skipped {} hidden folder(s), e.g. {}".format(libpath, len(hidden), ", ".join(hidden[:3])))
    for entry in entries:
        if entry.startswith("."):
            continue
        desc_path = os.path.join(libpath, entry, "DESCRIPTION")
        if not os.path.isfile(desc_path):
            continue
        try:
            with open(desc_path, encoding="utf-8", errors="replace") as handle:
                desc = parse_dcf(handle.read())
        except OSError as error:
            warnings.append("cannot read {}: {}".format(desc_path, error))
            continue
        if not desc.get("Package") or not desc.get("Version"):
            warnings.append("{} has no Package/Version".format(desc_path))
            continue
        if not desc.get("Built"):
            # Installed packages always carry Built; R's own translations folder and
            # stray source trees do not, and installed.packages() ignores them too.
            if entry != "translations":
                warnings.append("{}: no Built field, not an installed package; skipped".format(desc_path))
            continue
        if desc["Package"] != entry:
            warnings.append("{}: folder name differs from Package field {}".format(desc_path, desc["Package"]))
        desc["_libpath"] = libpath
        packages.append(desc)
    return packages, warnings


def build_capture(libpaths, name, r_version):
    all_packages, warnings = [], []
    for libpath in libpaths:
        found, lib_warnings = read_library(libpath)
        all_packages.extend(found)
        warnings.extend(lib_warnings)
        if not found:
            warnings.append("no packages found in {}".format(libpath))
    if not all_packages:
        raise SystemExit("capture-error: no R packages found in {}".format(", ".join(libpaths)))

    effective, shadowed = {}, []
    for desc in all_packages:                      # libpaths order: first wins
        if desc["Package"] in effective:
            shadowed.append(desc)
        else:
            effective[desc["Package"]] = desc

    rows, skew = [], []
    target_minor = minor(r_version)
    for desc in all_packages:
        bv, platform, when = built_info(desc.get("Built"))
        is_effective = effective[desc["Package"]] is desc
        compiled = (desc.get("NeedsCompilation") or "").lower() == "yes"
        needs = (desc.get("NeedsCompilation") or "").lower()
        rows.append([desc["Package"], desc["Version"], desc["_libpath"], "yes" if is_effective else "no (shadowed)",
                     desc.get("Priority", ""), source_of(desc), desc.get("Repository", ""),
                     needs if needs in ("yes", "no") else "", bv, platform, when])
        if is_effective and target_minor and bv and minor(bv) != target_minor:
            skew.append([desc["Package"], desc["Version"], desc["_libpath"], bv, r_version,
                         "yes" if compiled else "no", "HIGH" if compiled else "low"])

    lock_packages = {}
    for pkg in sorted(effective):
        desc = effective[pkg]
        if desc.get("Priority") == "base":
            continue                               # ships with R; renv does not lock these
        entry = {"Package": pkg, "Version": desc["Version"], "Source": source_of(desc)}
        if desc.get("Repository"):
            entry["Repository"] = desc["Repository"]
        elif entry["Source"] == "Bioconductor":
            entry["Repository"] = "Bioconductor"
        for field in ("RemoteType", "RemoteHost", "RemoteRepo", "RemoteUsername", "RemoteRef", "RemoteSha", "RemoteUrl"):
            if desc.get(field):
                entry[field] = desc[field]
        for field in ("NeedsCompilation", "Priority"):
            if desc.get(field):
                entry[field] = desc[field]
        for field in DEP_FIELDS:
            if desc.get(field):
                entry[field] = [d.strip() for d in desc[field].replace("\n", " ").split(",") if d.strip()]
        lock_packages[pkg] = entry
    lock = {"R": {"Version": r_version, "Repositories": [{"Name": "CRAN", "URL": "https://cloud.r-project.org"}]},
            "Packages": lock_packages}

    missing_deps = sorted({d for desc in effective.values() for f in DEP_FIELDS for d in dep_names(desc.get(f))}
                          - set(effective) - BASE_PACKAGES)
    if missing_deps:
        warnings.append("dependencies not installed in any captured library: {}".format(", ".join(missing_deps[:30])))

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["package", "version", "libpath", "effective", "priority", "source", "repository",
                "needs_compilation", "built_r_version", "built_platform", "built_date"])
    w.writerows(rows)
    skew_out = io.StringIO()
    sw = csv.writer(skew_out)
    sw.writerow(["package", "version", "libpath", "built_r_version", "running_r_version", "needs_compilation", "risk"])
    sw.writerows(sorted(skew, key=lambda r: (r[6] != "HIGH", r[0].lower())))

    # For the vulnerability scan: every distinct package version on disk, not only
    # the winning copy. Different IDEs see different library sets (e.g. one adds
    # the shared library, the other does not), so a copy shadowed in one session
    # can be the one loaded in another. Same columns as R's installed.packages().
    scan_out = io.StringIO()
    iw = csv.writer(scan_out)
    iw.writerow(["Package", "Version", "Priority", "Repository", "LibPath"])
    seen_versions = set()
    for desc in all_packages:
        key = (desc["Package"], desc["Version"])
        if key in seen_versions:
            continue
        seen_versions.add(key)
        repository = desc.get("Repository") or ("Bioconductor" if source_of(desc) == "Bioconductor" else "")
        iw.writerow([desc["Package"], desc["Version"], desc.get("Priority", ""), repository, desc["_libpath"]])

    files = {
        "renv.lock": json.dumps(lock, indent=2, sort_keys=True) + "\n",
        "r-packages.csv": out.getvalue(),
        "build-skew.csv": skew_out.getvalue(),
        "installed-packages.csv": scan_out.getvalue(),
    }
    by_lib = {}
    for desc in all_packages:
        by_lib[desc["_libpath"]] = by_lib.get(desc["_libpath"], 0) + 1
    built_versions = {}
    for desc in effective.values():
        bv = minor(built_info(desc.get("Built"))[0]) or "unknown"
        built_versions[bv] = built_versions.get(bv, 0) + 1
    info = {
        "tool": "capture-r-library.py", "tool_version": TOOL_VERSION,
        "captured_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "name": name, "libpaths": list(libpaths), "running_r_version": r_version,
        "counts": {"packages_found": len(all_packages), "effective_packages": len(effective),
                   "distinct_package_versions": len(seen_versions),
                   "shadowed_copies": len(shadowed), "locked_packages": len(lock_packages),
                   "packages_by_libpath": by_lib, "effective_packages_by_built_r_minor": built_versions,
                   "build_skew": len(skew), "build_skew_compiled": sum(1 for r in skew if r[6] == "HIGH")},
        "warnings": warnings,
        "sha256": {k: hashlib.sha256(v.encode("utf-8")).hexdigest() for k, v in sorted(files.items())},
    }
    files["capture-info.json"] = json.dumps(info, indent=1, sort_keys=True) + "\n"
    return files, info


BASE_PACKAGES = {"base", "compiler", "datasets", "graphics", "grDevices", "grid", "methods", "parallel",
                 "splines", "stats", "stats4", "tcltk", "tools", "utils"}


def write_archive(files, path):
    """Deterministic tar.gz: fixed mtimes and owners, so the fingerprint depends only on content."""
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
    parser = argparse.ArgumentParser(description="Capture installed R libraries as an renv.lock plus inventory.")
    parser.add_argument("libpaths", nargs="+", help="Library directories, in .libPaths() order")
    parser.add_argument("--name", required=True, help="Short name for this capture, e.g. posit-shared-r-library")
    parser.add_argument("--r-version", required=True, help="R version the libraries are loaded into, e.g. 4.4.0")
    parser.add_argument("--output-dir", default=".")
    args = parser.parse_args(argv)

    libpaths = [os.path.abspath(p) for p in args.libpaths]
    files, info = build_capture(libpaths, args.name, args.r_version)
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
    print("captured {}: {} effective packages ({} found, {} shadowed copies), {} in renv.lock".format(
        args.name, c["effective_packages"], c["packages_found"], c["shadowed_copies"], c["locked_packages"]))
    print("built under R: " + ", ".join("{} {}".format(k, v) for k, v in sorted(c["effective_packages_by_built_r_minor"].items())))
    print("build skew vs R {}: {} packages, {} with compiled code".format(args.r_version, c["build_skew"], c["build_skew_compiled"]))
    for warning in info["warnings"]:
        print("warning: " + warning[:300])
    # sha256, not md5: FIPS-mode hosts (such as MIP RHEL 8) refuse md5 in Python
    print("sha256 {}  {}".format(hashlib.sha256(blob).hexdigest(), os.path.basename(archive)))
    print("{} lines in {}".format(len(lines), os.path.basename(b64)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
