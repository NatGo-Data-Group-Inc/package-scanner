#!/usr/bin/env python3
"""Generate deterministic triage shard manifests from an R requested-package manifest."""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path


GEOSPATIAL_PACKAGES = {
    "classint",
    "exactextractr",
    "lwgeom",
    "maptools",
    "raster",
    "rgdal",
    "s2",
    "sf",
    "sp",
    "spacetime",
    "spdata",
    "stars",
    "terra",
    "vapour",
    "wk",
}

VISUALIZATION_PACKAGES = {
    "bslib",
    "fontawesome",
    "ggeffects",
    "ggplot2",
    "htmltools",
    "htmlwidgets",
    "isoband",
    "knitr",
    "plotly",
    "quarto",
    "ragg",
    "rmarkdown",
    "sass",
    "systemfonts",
    "textshaping",
}

COMPILED_MODELING_PACKAGES = {
    "bcclong",
    "fadpclust",
    "flexmix",
    "gbmt",
    "gmp",
    "latrend",
    "lcmm",
    "nloptr",
    "rcpp",
    "rcpparmadillo",
    "rcppeigen",
    "rmpfr",
    "spacefillr",
    "tmb",
    "vgam",
}

OHDSI_PACKAGES = {
    "andromeda",
    "circer",
    "databaseconnector",
    "featureextraction",
    "ohdsi",
    "ohdsiwebapi",
    "patientlevelprediction",
    "sqlrender",
}


def load_manifest(path: Path) -> dict:
    with path.open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def classify_package(entry: dict) -> str:
    name = str(entry.get("name") or "").strip()
    lowered = name.lower()
    source = str(entry.get("source") or "Repository").strip()

    if source != "Repository":
        return "custom-source"
    if lowered in OHDSI_PACKAGES:
        return "ohdsi"
    if lowered in GEOSPATIAL_PACKAGES:
        return "geospatial"
    if lowered in VISUALIZATION_PACKAGES:
        return "visualization-reporting"
    if lowered in COMPILED_MODELING_PACKAGES or lowered.startswith(("rcpp", "stan", "brms")):
        return "compiled-modeling"
    return "general-cran"


def chunk_entries(entries: list[dict], size: int) -> list[list[dict]]:
    return [entries[idx : idx + size] for idx in range(0, len(entries), size)]


def shard_display_name(group: str, index: int, total: int) -> str:
    if total == 1:
        return group
    return f"{group}-{index:02d}"


def build_shard_manifest(base_manifest: dict, entries: list[dict], *, candidate_id: str, group: str, shard_name: str, shard_index: int, total_shards: int) -> dict:
    manifest = {
        "schema_version": base_manifest.get("schema_version", 1),
        "ecosystem": "r",
        "input_type": "requested-packages",
        "generated_from": "triage-sharder",
        "run_purpose": "triage",
        "workflow": "requested-package-triage",
        "candidate_id": candidate_id,
        "display_name": f"{candidate_id} triage {shard_name}",
        "r": base_manifest.get("r", {}),
        "repositories": base_manifest.get("repositories", {}),
        "packages": entries,
        "triage": {
            "group": group,
            "shard_id": shard_name,
            "shard_name": shard_name,
            "shard_index": shard_index,
            "total_shards": total_shards,
            "package_count": len(entries),
        },
    }
    return manifest


def build_plan(manifest: dict, *, candidate_id: str, max_packages_per_shard: int) -> tuple[dict, list[tuple[str, dict]]]:
    packages = manifest.get("packages") or []
    grouped: dict[str, list[dict]] = defaultdict(list)
    for entry in packages:
        grouped[classify_package(entry)].append(entry)

    ordered_groups = [
        "geospatial",
        "compiled-modeling",
        "visualization-reporting",
        "ohdsi",
        "custom-source",
        "general-cran",
    ]
    shards: list[tuple[str, dict]] = []
    shard_specs: list[dict] = []
    total_package_count = 0
    total_shards = 0
    grouped_chunks: list[tuple[str, int, int, list[dict]]] = []
    for group in ordered_groups:
        entries = sorted(grouped.get(group, []), key=lambda item: str(item.get("name") or "").lower())
        if not entries:
            continue
        chunks = chunk_entries(entries, max_packages_per_shard)
        for idx, chunk in enumerate(chunks, start=1):
            grouped_chunks.append((group, idx, len(chunks), chunk))
            total_shards += 1

    for shard_index, (group, idx, chunk_total, chunk) in enumerate(grouped_chunks, start=1):
        shard_name = shard_display_name(group, idx, chunk_total)
        shard_manifest = build_shard_manifest(
            manifest,
            chunk,
            candidate_id=candidate_id,
            group=group,
            shard_name=shard_name,
            shard_index=shard_index,
            total_shards=total_shards,
        )
        filename = f"{shard_index:02d}-{shard_name}.requested-packages.json"
        shards.append((filename, shard_manifest))
        total_package_count += len(chunk)
        shard_specs.append(
            {
                "shard_id": shard_name,
                "group": group,
                "filename": filename,
                "package_count": len(chunk),
                "package_names": [entry["name"] for entry in chunk],
            }
        )

    plan = {
        "schema_version": 1,
        "ecosystem": "r",
        "workflow": "requested-package-triage",
        "run_purpose": "triage",
        "candidate_id": candidate_id,
        "requested_package_count": total_package_count,
        "shard_count": total_shards,
        "max_packages_per_shard": max_packages_per_shard,
        "suggested_parallelism": min(4, max(1, total_shards)),
        "shards": shard_specs,
    }
    return plan, shards


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requested-file", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--candidate-id", default="")
    parser.add_argument("--max-packages-per-shard", type=int, default=60)
    args = parser.parse_args()

    requested_path = Path(args.requested_file)
    manifest = load_manifest(requested_path)
    candidate_id = args.candidate_id.strip() or str(manifest.get("candidate_id") or requested_path.parent.name or "candidate")
    max_packages_per_shard = max(1, args.max_packages_per_shard)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    plan, shards = build_plan(manifest, candidate_id=candidate_id, max_packages_per_shard=max_packages_per_shard)
    (out_dir / "triage-plan.json").write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    for filename, shard_manifest in shards:
        (out_dir / filename).write_text(json.dumps(shard_manifest, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
