#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml


NAME_RE = re.compile(r"[=<>!~\s]")

CORE_NAMES = {
    "python",
    "python_abi",
    "pip",
    "setuptools",
    "wheel",
    "ca-certificates",
    "openssl",
    "libffi",
    "libsqlite",
    "sqlite",
    "readline",
    "tk",
    "tzdata",
    "ncurses",
    "libzlib",
    "zlib",
    "zstd",
    "bzip2",
    "liblzma",
    "libgcc",
    "libgcc-ng",
    "libstdcxx",
    "libstdcxx-ng",
    "libgomp",
}

NATIVE_PREFIXES = (
    "lib",
    "perl",
    "r-",
    "bioconductor-",
    "xorg-",
    "font-",
    "fonts-",
    "cuda",
    "cudnn",
)

NATIVE_NAMES = {
    "_openmp_mutex",
    "aragorn",
    "archspec",
    "backports.zstd",
    "bakta",
    "bbmap",
    "biopython",
    "blast",
    "brotli",
    "brotli-bin",
    "brotli-python",
    "c-ares",
    "cffi",
    "cgecore",
    "contourpy",
    "curl",
    "diamond",
    "entrez-direct",
    "fastqc",
    "fontconfig",
    "fonttools",
    "freetype",
    "git",
    "gperftools",
    "hmmer",
    "infernal",
    "isa-l",
    "kaleido-core",
    "keyutils",
    "kiwisolver",
    "kma",
    "kraken2",
    "krb5",
    "lcms2",
    "ld_impl_linux-64",
    "lerc",
    "lz4-c",
    "mathjax",
    "matplotlib-base",
    "multiqc",
    "ncbi-amrfinderplus",
    "ncbi-vdb",
    "networkx",
    "nspr",
    "nss",
    "numpy",
    "openjdk",
    "openjpeg",
    "pandas",
    "pbzip2",
    "pcre2",
    "pillow",
    "plotly",
    "popt",
    "psutil",
    "pthread-stubs",
    "pycparser",
    "pydantic-core",
    "pygments",
    "pyhmmer",
    "pyparsing",
    "pyrodigal",
    "pysocks",
    "python-isal",
    "python-kaleido",
    "python-zlib-ng",
    "qhull",
    "regex",
    "rpds-py",
    "rsync",
    "spectra",
    "sra-human-scrubber",
    "tabulate",
    "tar",
    "tiktoken",
    "tqdm",
    "trnascan-se",
    "unicodedata2",
    "urllib3",
    "virulencefinder",
    "wget",
    "xopen",
    "xxhash",
    "yaml",
    "zipp",
    "zlib-ng",
    "zstandard",
}


def package_name(spec: str) -> str:
    return NAME_RE.split(spec.strip(), maxsplit=1)[0].lower()


def write_env(path: Path, name: str, channels: list[str], specs: list[str]) -> None:
    payload = {"name": name, "channels": channels, "dependencies": specs}
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--environment-file", required=True)
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()

    env_path = Path(args.environment_file)
    run_dir = Path(args.run_dir)
    data = yaml.safe_load(env_path.read_text(encoding="utf-8"))

    channels = list(data.get("channels", []))
    dependencies = list(data.get("dependencies", []))
    name = str(data.get("name", "target"))

    conda_specs: list[str] = []
    pip_specs: list[str] = []
    for item in dependencies:
        if isinstance(item, str):
            conda_specs.append(item)
        elif isinstance(item, dict) and "pip" in item:
            pip_specs.extend(str(spec) for spec in item["pip"])

    core_specs: list[str] = []
    native_specs: list[str] = []
    python_specs: list[str] = []

    for spec in conda_specs:
        pkg = package_name(spec)
        if pkg in CORE_NAMES:
            core_specs.append(spec)
        elif pkg.startswith(NATIVE_PREFIXES) or pkg in NATIVE_NAMES:
            native_specs.append(spec)
        else:
            python_specs.append(spec)

    write_env(run_dir / "environment.conda-core.yml", name, channels, core_specs)
    write_env(run_dir / "environment.conda-native.yml", name, channels, native_specs)
    write_env(run_dir / "environment.conda-python.yml", name, channels, python_specs)
    (run_dir / "environment.pip.requirements.txt").write_text(
        "\n".join(pip_specs) + ("\n" if pip_specs else ""),
        encoding="utf-8",
    )
    (run_dir / "environment.install-plan.json").write_text(
        json.dumps(
            {
                "conda_core_count": len(core_specs),
                "conda_native_count": len(native_specs),
                "conda_python_count": len(python_specs),
                "pip_count": len(pip_specs),
                "conda_core": [package_name(spec) for spec in core_specs],
                "conda_native_sample": [package_name(spec) for spec in native_specs[:40]],
                "conda_python_sample": [package_name(spec) for spec in python_specs[:40]],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
