"""Identify a file's type from its content, not its name.

A renamed executable must not be treated as a CSV, so every decision in the
reference-file scanner starts from the bytes. The extension is only used to
report mismatches and to pick between text sub-types that look alike.
"""
from __future__ import annotations

import bz2
import gzip
import lzma
import zipfile
from dataclasses import dataclass
from pathlib import Path

HEAD_BYTES = 64 * 1024


@dataclass(frozen=True)
class FileType:
    kind: str           # e.g. "csv", "xlsx", "bam", "executable"
    family: str         # text | tabular | genomic | office | pdf | archive | compressed | data | image | code | empty | unknown
    detail: str = ""    # e.g. "gzip(bgzf) -> vcf"


# Extensions we expect for each kind; a known extension on the wrong content is a mismatch.
EXPECTED_EXTENSIONS = {
    "csv": {".csv"}, "tsv": {".tsv", ".tab"}, "json": {".json", ".geojson"}, "text": {".txt", ".log", ".md", ".xml", ".yaml", ".yml"},
    "fasta": {".fa", ".fasta", ".fna", ".faa", ".ffn"}, "fastq": {".fq", ".fastq"}, "vcf": {".vcf"},
    "bam": {".bam"}, "cram": {".cram"}, "hdf5": {".h5", ".hdf5", ".h5ad", ".loom"}, "parquet": {".parquet"},
    "sqlite": {".sqlite", ".db", ".sqlite3"}, "pdf": {".pdf"},
    "xlsx": {".xlsx", ".xlsm"}, "docx": {".docx", ".docm"}, "pptx": {".pptx", ".pptm"}, "ole2": {".xls", ".doc", ".ppt", ".msi"},
    "zip": {".zip"}, "tar": {".tar"}, "gzip": {".gz", ".tgz", ".bgz"}, "bzip2": {".bz2", ".tbz2"}, "xz": {".xz", ".txz"},
    "png": {".png"}, "jpeg": {".jpg", ".jpeg"}, "gif": {".gif"}, "tiff": {".tif", ".tiff"},
}
EXTENSION_KINDS = {ext: kind for kind, exts in EXPECTED_EXTENSIONS.items() for ext in exts}


def file_extension(path: Path) -> str:
    """Last suffix, ignoring a compression suffix when the inner suffix is meaningful (x.vcf.gz -> .vcf.gz parts)."""
    return path.suffix.lower()


def _peek_decompressed(path: Path, opener) -> bytes:
    try:
        with opener(path, "rb") as handle:
            return handle.read(HEAD_BYTES)
    except (OSError, EOFError, lzma.LZMAError, ValueError):
        return b""


def _text_kind(head: bytes, path: Path) -> tuple[str, str] | None:
    """Return (kind, family) if the bytes look like text, else None."""
    if not head:
        return None
    if b"\x00" in head:
        return None
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError as error:
        if error.start < len(head) - 4:          # not just a multibyte character cut at the boundary
            try:
                text = head.decode("latin-1")
            except UnicodeDecodeError:
                return None
            if sum(ch < " " and ch not in "\t\r\n\f" for ch in text) > len(text) // 100:
                return None
        else:
            text = head[: error.start].decode("utf-8", errors="ignore")
    stripped = text.lstrip("﻿ \t\r\n")
    ext = file_extension(path)
    if stripped.startswith("##fileformat=VCF"):
        return "vcf", "genomic"
    if stripped.startswith(">") and ext not in {".csv", ".tsv", ".txt"}:
        return "fasta", "genomic"
    lines = stripped.splitlines()
    if len(lines) >= 4 and lines[0].startswith("@") and lines[2].startswith("+"):
        return "fastq", "genomic"
    if stripped[:1] in "{[" and ext in {".json", ".geojson", ""}:
        return "json", "text"
    if ext in {".csv"}:
        return "csv", "tabular"
    if ext in {".tsv", ".tab"}:
        return "tsv", "tabular"
    first = lines[0] if lines else ""
    if ext in {"", ".txt", ".dat"} and first.count(",") >= 2 and all(l.count(",") == first.count(",") for l in lines[1:6] if l):
        return "csv", "tabular"
    if ext in {"", ".txt", ".dat"} and first.count("\t") >= 2 and all(l.count("\t") == first.count("\t") for l in lines[1:6] if l):
        return "tsv", "tabular"
    if stripped.startswith("#!"):
        return "script", "code"
    return "text", "text"


def _zip_kind(path: Path) -> tuple[str, str, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
    except (zipfile.BadZipFile, OSError):
        return "zip", "archive", "unreadable zip"
    if "[Content_Types].xml" in names:
        for prefix, kind in (("xl/", "xlsx"), ("word/", "docx"), ("ppt/", "pptx")):
            if any(n.startswith(prefix) for n in names):
                return kind, "office", "OOXML"
        return "ooxml", "office", "OOXML, unrecognised part layout"
    if any(n.endswith("data.pkl") for n in names):
        return "pytorch-model", "code", "zip containing a pickle (PyTorch checkpoint)"
    return "zip", "archive", ""


def _inner_kind(inner: bytes) -> str | None:
    if inner.startswith(b"BAM\x01"):
        return "bam"
    if len(inner) > 262 and inner[257:262] == b"ustar":
        return "tar"
    return None


def identify(path: Path) -> FileType:
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        return FileType("unreadable", "unknown", "cannot stat")
    if size == 0:
        return FileType("empty", "empty")
    with open(path, "rb") as handle:
        head = handle.read(HEAD_BYTES)

    if head.startswith(b"\x7fELF"):
        return FileType("executable", "code", "ELF")
    if head.startswith(b"MZ"):
        return FileType("executable", "code", "Windows PE")
    if head[:4] in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce"):
        return FileType("executable", "code", "Mach-O")
    if head.startswith(b"%PDF-"):
        return FileType("pdf", "pdf")
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        kind, family, detail = _zip_kind(path)
        return FileType(kind, family, detail)
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return FileType("ole2", "office", "legacy Office / OLE2 compound file")
    if head.startswith(b"\x1f\x8b"):
        bgzf = len(head) > 16 and head[3] & 0x04 and head[12:14] == b"BC"
        inner = _peek_decompressed(path, gzip.open)
        label = "gzip(bgzf)" if bgzf else "gzip"
        inner_kind = _inner_kind(inner)
        if inner_kind == "bam":
            return FileType("bam", "genomic", label)
        if inner_kind == "tar":
            return FileType("tar", "archive", f"{label} -> tar")
        text = _text_kind(inner, Path(path.stem))
        if text and text[1] in {"genomic", "tabular"}:
            return FileType(text[0], text[1], f"{label} -> {text[0]}")
        return FileType("gzip", "compressed", label)
    if head.startswith(b"BZh"):
        inner = _peek_decompressed(path, bz2.open)
        return FileType("tar", "archive", "bzip2 -> tar") if _inner_kind(inner) == "tar" else FileType("bzip2", "compressed")
    if head.startswith(b"\xfd7zXZ\x00"):
        inner = _peek_decompressed(path, lzma.open)
        return FileType("tar", "archive", "xz -> tar") if _inner_kind(inner) == "tar" else FileType("xz", "compressed")
    if head.startswith(b"(\xb5/\xfd"):
        return FileType("zstd", "compressed", "zstandard (not unpacked)")
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return FileType("7z", "archive", "7-Zip (not unpacked)")
    if head.startswith(b"Rar!\x1a\x07"):
        return FileType("rar", "archive", "RAR (not unpacked)")
    if head.startswith(b"CRAM"):
        return FileType("cram", "genomic")
    if head.startswith(b"\x89HDF\r\n\x1a\n"):
        return FileType("hdf5", "data")
    if head.startswith(b"PAR1"):
        return FileType("parquet", "data")
    if head.startswith(b"SQLite format 3\x00"):
        return FileType("sqlite", "data")
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return FileType("png", "image")
    if head.startswith(b"\xff\xd8\xff"):
        return FileType("jpeg", "image")
    if head.startswith((b"GIF87a", b"GIF89a")):
        return FileType("gif", "image")
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return FileType("tiff", "image")
    if len(head) > 262 and head[257:262] == b"ustar":
        return FileType("tar", "archive")
    if len(head) >= 2 and head[0] == 0x80 and 2 <= head[1] <= 5:
        return FileType("pickle", "code", f"Python pickle protocol {head[1]}")
    text = _text_kind(head, path)
    if text:
        return FileType(text[0], text[1])
    return FileType("unknown", "unknown")


def extension_mismatch(path: Path, file_type: FileType) -> str | None:
    """Explain a mismatch between a known extension and the content, or return None."""
    ext = file_extension(Path(path))
    if ext in {".gz", ".bgz"} and file_type.detail.startswith("gzip"):
        return None
    expected = EXTENSION_KINDS.get(ext)
    if expected is None or expected == file_type.kind:
        return None
    compatible = {("csv", "text"), ("tsv", "text"), ("text", "csv"), ("text", "tsv"), ("text", "json"), ("json", "text"),
                  ("xlsx", "ooxml"), ("gzip", "tar"), ("bzip2", "tar"), ("xz", "tar"), ("text", "script")}
    if (expected, file_type.kind) in compatible:
        return None
    return f"extension {ext} suggests {expected}, content is {file_type.kind}"
