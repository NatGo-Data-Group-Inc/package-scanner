#!/usr/bin/env python3
import argparse
import json
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Optional


LINUX_RULES = [
    {
        "id": "cmake",
        "description": "Packages with native builds that require CMake.",
        "packages": ["nloptr"],
        "checks": [
            {
                "type": "command",
                "path": "cmake",
                "label": "cmake",
                "install_hint": "Install the distro CMake package.",
            }
        ],
    },
    {
        "id": "mpfr",
        "description": "Multiple-precision floating-point support required by Rmpfr and dependents.",
        "packages": ["Rmpfr"],
        "checks": [
            {
                "type": "header",
                "paths": ["/usr/include/mpfr.h", "/usr/local/include/mpfr.h"],
                "label": "mpfr.h",
                "install_hint": "Install the distro MPFR development package, such as mpfr-devel.",
            }
        ],
    },
    {
        "id": "png-freetype",
        "description": "libpng and FreeType headers required by isoband/ggplot2 and related graphics packages.",
        "packages": ["isoband", "ggplot2"],
        "checks": [
            {
                "type": "header",
                "paths": ["/usr/include/png.h", "/usr/local/include/png.h"],
                "label": "png.h",
                "install_hint": "Install the distro libpng development package, such as libpng-devel.",
            },
            {
                "type": "header",
                "paths": ["/usr/include/freetype2/ft2build.h", "/usr/local/include/freetype2/ft2build.h"],
                "label": "freetype2/ft2build.h",
                "install_hint": "Install the distro FreeType development package, such as freetype-devel.",
            },
        ],
    },
    {
        "id": "libuv",
        "description": "libuv headers required by fs and dependent packages.",
        "packages": ["fs"],
        "checks": [
            {
                "type": "header",
                "paths": ["/usr/include/uv.h", "/usr/local/include/uv.h"],
                "label": "uv.h",
                "install_hint": "Install the distro libuv development package, such as libuv-devel.",
            }
        ],
    },
    {
        "id": "udunits2",
        "description": "UDUNITS-2 configuration required by spatial/time packages (e.g., spData, terra).",
        "packages": ["spData", "terra"],
        "checks": [
            {
                "type": "command",
                "path": "udunits2-config",
                "label": "udunits2-config",
                "install_hint": "Install the distro udunits2 development package, such as udunits2-devel.",
            }
        ],
    },
    {
        "id": "gmp",
        "description": "GNU MP headers and libraries required by packages such as gmp.",
        "packages": ["gmp"],
        "checks": [
            {
                "type": "header",
                "paths": ["/usr/include/gmp.h", "/usr/local/include/gmp.h"],
                "label": "gmp.h",
                "install_hint": "Install the distro GMP development package, such as gmp-devel.",
            }
        ],
    },
    {
        "id": "geospatial-stack",
        "description": "GDAL/GEOS/PROJ toolchain required by geospatial packages.",
        "packages": ["terra", "sf", "stars", "vapour"],
        "checks": [
            {
                "type": "command",
                "path": "gdal-config",
                "label": "gdal-config",
                "install_hint": "Install the distro GDAL development package, such as gdal310-devel on Amazon Linux 2023.",
            },
            {
                "type": "command",
                "path": "geos-config",
                "label": "geos-config",
                "install_hint": "Install the distro GEOS development package, such as geos-devel.",
            },
            {
                "type": "command",
                "path": "proj",
                "label": "proj",
                "install_hint": "Install the distro PROJ package, such as proj-devel.",
            },
        ],
    },
]


WINDOWS_RULES = [
    {
        "id": "rtools",
        "description": "Source package builds require Rtools on Windows.",
        "packages": ["nloptr", "gmp", "terra", "sf", "stars", "vapour"],
        "checks": [
            {
                "type": "command",
                "path": "make",
                "label": "make",
                "install_hint": "Install and expose Rtools in PATH.",
            }
        ],
    }
]


def load_lockfile(path: Path) -> dict:
    with path.open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def normalize_platform(value: Optional[str]) -> str:
    if value:
        return value
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "linux":
        return f"linux-{machine}"
    if system == "windows":
        return f"windows-{machine}"
    return system


def check_requirement(check: dict) -> dict:
    if check["type"] == "command":
        resolved = shutil.which(check["path"])
        return {
            "label": check["label"],
            "type": check["type"],
            "present": bool(resolved),
            "detail": resolved or f"{check['path']} not found in PATH",
            "install_hint": check["install_hint"],
        }
    if check["type"] == "header":
        for path in check["paths"]:
            if os.path.exists(path):
                return {
                    "label": check["label"],
                    "type": check["type"],
                    "present": True,
                    "detail": path,
                    "install_hint": check["install_hint"],
                }
        return {
            "label": check["label"],
            "type": check["type"],
            "present": False,
            "detail": f"none of {', '.join(check['paths'])} exists",
            "install_hint": check["install_hint"],
        }
    raise ValueError(f"Unsupported check type: {check['type']}")


def platform_rules(target_platform: str) -> list[dict]:
    if target_platform.startswith("windows"):
        return WINDOWS_RULES
    return LINUX_RULES


def build_report(lockfile: Path, target_platform: str) -> dict:
    payload = load_lockfile(lockfile)
    packages = payload.get("Packages", {})
    package_names = sorted(packages.keys(), key=str.lower)
    package_index = {name.lower(): name for name in package_names}
    report_rules = []
    missing = []
    for rule in platform_rules(target_platform):
        matched = [package_index[name.lower()] for name in rule["packages"] if name.lower() in package_index]
        if not matched:
            continue
        checks = [check_requirement(check) for check in rule["checks"]]
        rule_report = {
            "id": rule["id"],
            "description": rule["description"],
            "matched_packages": matched,
            "checks": checks,
        }
        report_rules.append(rule_report)
        failures = [check for check in checks if not check["present"]]
        if failures:
            missing.append(
                {
                    "id": rule["id"],
                    "matched_packages": matched,
                    "missing_checks": failures,
                }
            )
    return {
        "status": "failed" if missing else "passed",
        "platform": target_platform,
        "lockfile": str(lockfile),
        "package_count": len(package_names),
        "matched_rule_count": len(report_rules),
        "rules": report_rules,
        "missing_requirements": missing,
        "notes": [
            "This preflight validates curated native/system dependencies for known package families.",
            "It does not guarantee that every possible source build dependency is covered.",
        ],
    }


def render_text(report: dict) -> str:
    lines = [
        f"R native dependency preflight: {report['status'].upper()}",
        f"Platform: {report['platform']}",
        f"Lockfile: {report['lockfile']}",
        f"Packages in lockfile: {report['package_count']}",
        f"Matched dependency rules: {report['matched_rule_count']}",
    ]
    if not report["rules"]:
        lines.append("No curated native dependency rules matched this lockfile.")
        return "\n".join(lines) + "\n"
    lines.append("")
    lines.append("Rule evaluation:")
    for rule in report["rules"]:
        lines.append(f"- {rule['id']}: {', '.join(rule['matched_packages'])}")
        for check in rule["checks"]:
            state = "OK" if check["present"] else "MISSING"
            lines.append(f"  - {state}: {check['label']} ({check['detail']})")
            if not check["present"]:
                lines.append(f"    Install hint: {check['install_hint']}")
    if report["missing_requirements"]:
        lines.append("")
        lines.append("Preflight failed because required native dependencies are missing.")
    else:
        lines.append("")
        lines.append("Preflight passed.")
    return "\n".join(lines) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock-file", required=True)
    parser.add_argument("--platform")
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-text", required=True)
    args = parser.parse_args(argv)

    lockfile = Path(args.lock_file)
    target_platform = normalize_platform(args.platform)
    report = build_report(lockfile, target_platform)

    Path(args.output_json).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    Path(args.output_text).write_text(render_text(report), encoding="utf-8")
    return 2 if report["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
