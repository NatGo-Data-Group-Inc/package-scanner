"""Unpack archives safely, recursively, without trusting their headers.

Every member is streamed to disk with a running byte count, so a zip bomb that
lies about its sizes still stops at the limit. Paths that escape the target
directory, links, devices and encrypted members are refused and reported, not
extracted.
"""
from __future__ import annotations

import bz2
import gzip
import lzma
import os
import shutil
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

CHUNK = 8 * 1024 * 1024


@dataclass
class Limits:
    max_depth: int = 5                       # archives inside archives
    max_members: int = 200_000               # files per archive
    max_total_bytes: int = 2 * 1024 ** 4     # 2 TiB unpacked per submission
    max_ratio: float = 200.0                 # unpacked / packed for any one member


@dataclass
class ExtractIssue:
    code: str          # ARCHIVE_TRAVERSAL | ARCHIVE_LINK | ARCHIVE_SPECIAL | ARCHIVE_ENCRYPTED | ARCHIVE_LIMIT | ARCHIVE_CORRUPT
    member: str
    message: str


@dataclass
class ExtractResult:
    extracted: list[Path] = field(default_factory=list)
    issues: list[ExtractIssue] = field(default_factory=list)


class LimitExceeded(Exception):
    pass


class Budget:
    """Shared byte budget across a whole submission."""

    def __init__(self, limits: Limits):
        self.limits = limits
        self.used = 0

    def take(self, n: int) -> None:
        self.used += n
        if self.used > self.limits.max_total_bytes:
            raise LimitExceeded(f"unpacked total exceeds {self.limits.max_total_bytes} bytes")


def safe_member_path(name: str) -> PurePosixPath | None:
    """Normalise an archive member name; None if it is absolute or escapes upward."""
    name = name.replace("\\", "/")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        return None
    parts = [p for p in PurePosixPath(name).parts if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return PurePosixPath(*parts)


def _copy_limited(src, dest: Path, budget: Budget, packed_size: int, limits: Limits) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(dest, "wb") as out:
        while True:
            chunk = src.read(CHUNK)
            if not chunk:
                break
            written += len(chunk)
            budget.take(len(chunk))
            if packed_size > 0 and written > 1024 * 1024 and written / packed_size > limits.max_ratio:
                raise LimitExceeded(f"compression ratio above {limits.max_ratio:.0f}:1")
            out.write(chunk)


def extract_tar(archive: Path, target: Path, budget: Budget, limits: Limits) -> ExtractResult:
    result = ExtractResult()
    packed = archive.stat().st_size
    try:
        with tarfile.open(archive, mode="r:*") as tar:
            for index, member in enumerate(tar):
                if index >= limits.max_members:
                    result.issues.append(ExtractIssue("ARCHIVE_LIMIT", member.name, f"more than {limits.max_members} members; rest not unpacked"))
                    break
                rel = safe_member_path(member.name)
                if rel is None:
                    result.issues.append(ExtractIssue("ARCHIVE_TRAVERSAL", member.name, "path is absolute or escapes the archive"))
                    continue
                if member.issym() or member.islnk():
                    result.issues.append(ExtractIssue("ARCHIVE_LINK", member.name, f"link to {member.linkname!r} not followed"))
                    continue
                if member.isdir():
                    continue
                if not member.isfile():
                    result.issues.append(ExtractIssue("ARCHIVE_SPECIAL", member.name, "device, FIFO or other special file"))
                    continue
                src = tar.extractfile(member)
                if src is None:
                    continue
                dest = target / rel
                try:
                    _copy_limited(src, dest, budget, max(packed, 1), limits)
                except LimitExceeded as error:
                    dest.unlink(missing_ok=True)
                    result.issues.append(ExtractIssue("ARCHIVE_LIMIT", member.name, str(error)))
                    break
                result.extracted.append(dest)
    except (tarfile.TarError, OSError, EOFError, lzma.LZMAError, zipfile.BadZipFile) as error:
        result.issues.append(ExtractIssue("ARCHIVE_CORRUPT", archive.name, f"could not read archive: {error}"))
    return result


def extract_zip(archive: Path, target: Path, budget: Budget, limits: Limits) -> ExtractResult:
    result = ExtractResult()
    try:
        with zipfile.ZipFile(archive) as zf:
            infos = zf.infolist()
            for index, info in enumerate(infos):
                if index >= limits.max_members:
                    result.issues.append(ExtractIssue("ARCHIVE_LIMIT", info.filename, f"more than {limits.max_members} members; rest not unpacked"))
                    break
                rel = safe_member_path(info.filename)
                if rel is None:
                    result.issues.append(ExtractIssue("ARCHIVE_TRAVERSAL", info.filename, "path is absolute or escapes the archive"))
                    continue
                if info.is_dir():
                    continue
                if info.flag_bits & 0x1:
                    result.issues.append(ExtractIssue("ARCHIVE_ENCRYPTED", info.filename, "encrypted member cannot be scanned"))
                    continue
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    result.issues.append(ExtractIssue("ARCHIVE_LINK", info.filename, "symbolic link not followed"))
                    continue
                dest = target / rel
                try:
                    with zf.open(info) as src:
                        _copy_limited(src, dest, budget, max(info.compress_size, 1), limits)
                except LimitExceeded as error:
                    dest.unlink(missing_ok=True)
                    result.issues.append(ExtractIssue("ARCHIVE_LIMIT", info.filename, str(error)))
                    break
                except (zipfile.BadZipFile, RuntimeError, OSError, NotImplementedError) as error:
                    dest.unlink(missing_ok=True)
                    result.issues.append(ExtractIssue("ARCHIVE_CORRUPT", info.filename, f"could not unpack: {error}"))
                    continue
                result.extracted.append(dest)
    except (zipfile.BadZipFile, OSError) as error:
        result.issues.append(ExtractIssue("ARCHIVE_CORRUPT", archive.name, f"could not read archive: {error}"))
    return result


def decompress_single(archive: Path, kind: str, target: Path, budget: Budget, limits: Limits) -> ExtractResult:
    """A lone .gz/.bz2/.xz that is not a tar: unpack it to one file."""
    result = ExtractResult()
    opener = {"gzip": gzip.open, "bzip2": bz2.open, "xz": lzma.open}[kind]
    name = archive.name
    for suffix in (".gz", ".bgz", ".bz2", ".xz"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    dest = target / (name or "decompressed")
    try:
        with opener(archive, "rb") as src:
            _copy_limited(src, dest, budget, max(archive.stat().st_size, 1), limits)
    except LimitExceeded as error:
        dest.unlink(missing_ok=True)
        result.issues.append(ExtractIssue("ARCHIVE_LIMIT", archive.name, str(error)))
        return result
    except (OSError, EOFError, lzma.LZMAError) as error:
        dest.unlink(missing_ok=True)
        result.issues.append(ExtractIssue("ARCHIVE_CORRUPT", archive.name, f"could not decompress: {error}"))
        return result
    result.extracted.append(dest)
    return result


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def is_within(path: Path, root: Path) -> bool:
    try:
        Path(os.path.realpath(path)).relative_to(os.path.realpath(root))
        return True
    except ValueError:
        return False
