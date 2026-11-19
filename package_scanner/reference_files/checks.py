"""Per-format checks. Each streams its file and returns findings; the policy decides what they mean.

Every check reports what it covered, so the coverage table can show, for each
file, which checks ran and which could not (and why).
"""
from __future__ import annotations

import csv
import io
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .identify import FileType

CHUNK = 8 * 1024 * 1024


@dataclass
class Finding:
    code: str
    message: str
    detail: str = ""


@dataclass
class CheckResult:
    findings: list[Finding] = field(default_factory=list)
    ran: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)   # check -> reason


# ---------------------------------------------------------------- code-bearing types

def check_code(ft: FileType, result: CheckResult) -> None:
    result.ran.append("code-type")
    if ft.kind == "executable":
        result.findings.append(Finding("EXECUTABLE", f"executable program ({ft.detail})"))
    elif ft.kind == "script":
        result.findings.append(Finding("SCRIPT", "script with an interpreter line (#!)"))
    elif ft.kind in {"pickle", "pytorch-model"}:
        result.findings.append(Finding("PICKLE", f"serialized Python objects run code when loaded ({ft.detail})"))


# ---------------------------------------------------------------- Office

OOXML_DDE = re.compile(rb"DDEAUTO|\bDDE\b|ddeService|<ddeLink", re.I)


def check_ooxml(path: Path, result: CheckResult) -> None:
    result.ran.append("office")
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            macros = [n for n in names if n.lower().endswith("vbaproject.bin")]
            if macros:
                result.findings.append(Finding("OFFICE_MACROS", "contains VBA macros", ", ".join(macros)))
            ext_links = [n for n in names if "externallink" in n.lower()]
            if ext_links:
                result.findings.append(Finding("OFFICE_EXTERNAL_LINK", "links to external workbooks", ", ".join(ext_links[:5])))
            embedded = [n for n in names if "/embeddings/" in n.lower() or "oleobject" in n.lower()]
            if embedded:
                result.findings.append(Finding("OFFICE_EMBEDDED_OBJECT", "contains embedded OLE objects", ", ".join(embedded[:5])))
            for name in names:
                if name.endswith(".rels"):
                    data = zf.read(name)
                    if b'TargetMode="External"' in data and b"attachedTemplate" in data:
                        result.findings.append(Finding("OFFICE_REMOTE_TEMPLATE", "loads a remote template", name))
                if name.endswith(".xml") and (name.startswith(("xl/", "word/"))):
                    with zf.open(name) as handle:
                        if OOXML_DDE.search(handle.read(16 * 1024 * 1024)):
                            result.findings.append(Finding("OFFICE_DDE", "dynamic data exchange (DDE) field", name))
                            break
    except (zipfile.BadZipFile, OSError) as error:
        result.findings.append(Finding("UNREADABLE", f"Office file could not be read: {error}"))


def check_ole2(path: Path, result: CheckResult) -> None:
    """Legacy .xls/.doc/.ppt: use oletools if installed, otherwise report the gap."""
    try:
        from oletools.olevba import VBA_Parser  # type: ignore
    except ImportError:
        result.skipped["office-legacy"] = "oletools not installed"
        result.findings.append(Finding("LEGACY_OFFICE_UNCHECKED", "legacy Office file not inspected for macros (oletools missing)"))
        return
    result.ran.append("office-legacy")
    parser = VBA_Parser(str(path))
    try:
        if parser.detect_vba_macros():
            result.findings.append(Finding("OFFICE_MACROS", "contains VBA macros (legacy format)"))
    finally:
        parser.close()


# ---------------------------------------------------------------- PDF

PDF_KEYWORDS = [b"/JavaScript", b"/JS", b"/OpenAction", b"/AA", b"/Launch", b"/EmbeddedFile", b"/RichMedia", b"/XFA", b"/SubmitForm", b"/GoToR"]


def check_pdf(path: Path, result: CheckResult) -> None:
    result.ran.append("pdf-active-content")
    found: set[bytes] = set()
    encrypted = False
    tail = b""
    pattern = re.compile(b"(" + b"|".join(re.escape(k) for k in PDF_KEYWORDS + [b"/Encrypt"]) + rb")(?![A-Za-z])")
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            data = tail + chunk
            for match in pattern.finditer(data):
                if match.group(1) == b"/Encrypt":
                    encrypted = True
                else:
                    found.add(match.group(1))
            tail = data[-32:]
    if found:
        result.findings.append(Finding("PDF_ACTIVE_CONTENT", "active or auto-run content markers",
                                       ", ".join(sorted(k.decode() for k in found))))
    if encrypted:
        result.findings.append(Finding("PDF_ENCRYPTED", "encrypted PDF: content could not be inspected"))


# ---------------------------------------------------------------- CSV / TSV

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
NUMERIC = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


def check_tabular(path: Path, ft: FileType, result: CheckResult, opener=None) -> None:
    """Cells that a spreadsheet would execute as a formula when the file is opened."""
    result.ran.append("formula-injection")
    delimiter = "\t" if ft.kind == "tsv" else ","
    hits, first = 0, []
    raw = opener() if opener else open(path, "rb")
    with raw, io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="") as text:
        csv.field_size_limit(16 * 1024 * 1024)
        try:
            for row_number, row in enumerate(csv.reader(text, delimiter=delimiter), start=1):
                for col_number, cell in enumerate(row, start=1):
                    if cell and cell.startswith(FORMULA_PREFIXES) and not NUMERIC.match(cell.strip()):
                        hits += 1
                        if len(first) < 5:
                            first.append(f"row {row_number} col {col_number}: {cell[:40]!r}")
        except csv.Error as error:
            result.findings.append(Finding("UNREADABLE", f"tabular file could not be parsed: {error}"))
    if hits:
        result.findings.append(Finding("CSV_FORMULA", f"{hits} cell(s) would be treated as formulas by a spreadsheet",
                                       "; ".join(first)))


# ---------------------------------------------------------------- secrets

SECRET_PATTERNS = {
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY( BLOCK)?-----"),
    "AWS access key id": re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "Slack token": re.compile(rb"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    "password assignment": re.compile(rb"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key)\s*[:=]\s*['\"]?[^\s'\"]{8,}"),
}
SECRET_SCAN_MAX_BYTES = 20 * 1024 ** 3   # skip secret scanning above 20 GiB of text; reported in coverage


def check_secrets(path: Path, ft: FileType, result: CheckResult, size: int) -> None:
    if ft.family == "genomic":
        result.skipped["secrets"] = "not applied to genomic sequence data"
        return
    if size > SECRET_SCAN_MAX_BYTES:
        result.skipped["secrets"] = f"file larger than {SECRET_SCAN_MAX_BYTES // 1024 ** 3} GiB"
        return
    result.ran.append("secrets")
    found: dict[str, int] = {}
    tail = b""
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            data = tail + chunk
            for label, pattern in SECRET_PATTERNS.items():
                count = len(pattern.findall(data))
                if count:
                    found[label] = found.get(label, 0) + count
            tail = data[-256:]
    for label, count in sorted(found.items()):
        result.findings.append(Finding("SECRET", f"possible {label}", f"{count} match(es)"))


# ---------------------------------------------------------------- malware

@dataclass
class MalwareScanner:
    """Runs ClamAV once over a directory. Files it could not fully scan are reported, not passed."""
    binary: str | None = None
    max_filesize: str = "4000M"
    version: str = ""

    @classmethod
    def detect(cls) -> "MalwareScanner":
        binary = shutil.which("clamscan")
        scanner = cls(binary)
        if binary:
            try:
                scanner.version = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=60).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                scanner.version = "unknown"
        return scanner

    def scan_tree(self, root: Path) -> tuple[dict[str, str], dict[str, str], str | None]:
        """Return ({path: signature}, {path: reason not fully scanned}, error)."""
        if not self.binary:
            return {}, {}, "ClamAV (clamscan) not installed"
        cmd = [self.binary, "--recursive", "--infected", "--no-summary", "--stdout",
               f"--max-filesize={self.max_filesize}", f"--max-scansize={self.max_filesize}",
               "--alert-exceeds-max=yes", "--alert-encrypted=yes", str(root)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        except OSError as error:
            return {}, {}, f"ClamAV failed to run: {error}"
        if proc.returncode not in (0, 1):
            return {}, {}, f"ClamAV error (exit {proc.returncode}): {proc.stderr.strip()[:300]}"
        infected, limited = {}, {}
        for line in proc.stdout.splitlines():
            if not line.endswith(" FOUND") or ": " not in line:
                continue
            path, signature = line[: -len(" FOUND")].rsplit(": ", 1)
            if signature.startswith("Heuristics.Limits.Exceeded") or signature.startswith("Heuristics.Encrypted"):
                limited[path] = signature
            else:
                infected[path] = signature
        return infected, limited, None
