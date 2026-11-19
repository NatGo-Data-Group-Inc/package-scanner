"""Scan a reference-file submission: inventory, unpack, identify, check, decide, report.

    result = scan_submission(Path("incoming"), Path("out"))

Writes to the output directory:
  manifest.csv   every file, including those found inside archives (path, size, sha256, type)
  findings.csv   every finding with the action the policy assigns
  coverage.csv   for every file, which checks ran and which did not, and why
  summary.json   overall verdict, counts, policy, tool versions
"""
from __future__ import annotations

import csv
import datetime
import gzip
import hashlib
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import checks, extract
from .identify import FileType, extension_mismatch, identify
from .policy import Policy, verdict_for

TOOL_VERSION = "0.1"
SPLITTABLE = {"zip", "tar", "gzip", "bzip2", "xz"}


@dataclass
class FileRecord:
    path: str                  # relative to the submission; members as archive.tar!/inner/file
    size: int
    sha256: str
    file_type: FileType
    parent: str = ""
    findings: list[checks.Finding] = field(default_factory=list)
    ran: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(checks.CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class Scanner:
    def __init__(self, policy: Policy, limits: extract.Limits, malware: checks.MalwareScanner, workdir: Path):
        self.policy = policy
        self.limits = limits
        self.malware = malware
        self.workdir = workdir
        self.budget = extract.Budget(limits)
        self.records: list[FileRecord] = []
        self.on_disk: dict[str, Path] = {}     # display path -> real path (for the malware pass)

    # -- inventory and recursion ------------------------------------------------
    def add(self, real: Path, display: str, parent: str = "", depth: int = 0) -> None:
        ft = identify(real)
        size = real.stat().st_size
        record = FileRecord(display, size, sha256_file(real), ft, parent)
        self.records.append(record)
        self.on_disk[display] = real

        mismatch = extension_mismatch(real, ft)
        if mismatch:
            record.findings.append(checks.Finding("TYPE_MISMATCH", mismatch))

        container = ft.kind in SPLITTABLE or (ft.detail.startswith("gzip") and ft.family in {"tabular", "text"})
        if container:
            self.unpack(real, record, ft, display, depth)
            return
        if ft.kind in {"7z", "rar", "zstd"}:
            record.findings.append(checks.Finding("ARCHIVE_UNSUPPORTED", f"{ft.kind} archives are not unpacked; contents unchecked"))
        self.check_file(real, record, ft)

    def unpack(self, real: Path, record: FileRecord, ft: FileType, display: str, depth: int) -> None:
        record.ran.append("unpack")
        if depth >= self.limits.max_depth:
            record.findings.append(checks.Finding("ARCHIVE_LIMIT", f"nested deeper than {self.limits.max_depth} levels; not unpacked"))
            return
        target = Path(tempfile.mkdtemp(prefix="unpack-", dir=self.workdir))
        try:
            if ft.kind == "zip":
                result = extract.extract_zip(real, target, self.budget, self.limits)
            elif ft.kind == "tar":
                result = extract.extract_tar(real, target, self.budget, self.limits)
            else:
                result = extract.decompress_single(real, "gzip" if ft.detail.startswith("gzip") else ft.kind, target, self.budget, self.limits)
        except extract.LimitExceeded as error:
            record.findings.append(checks.Finding("ARCHIVE_LIMIT", str(error)))
            return
        for issue in result.issues:
            record.findings.append(checks.Finding(issue.code, issue.message, issue.member))
        for member in sorted(result.extracted):
            if not extract.is_within(member, target):
                record.findings.append(checks.Finding("ARCHIVE_TRAVERSAL", "member resolved outside the unpack directory", str(member)))
                continue
            self.add(member, f"{display}!/{member.relative_to(target).as_posix()}", display, depth + 1)

    # -- per-file checks ---------------------------------------------------------
    def check_file(self, real: Path, record: FileRecord, ft: FileType) -> None:
        result = checks.CheckResult()
        if ft.family == "code":
            checks.check_code(ft, result)
        elif ft.kind in {"xlsx", "docx", "pptx", "ooxml"}:
            checks.check_ooxml(real, result)
        elif ft.kind == "ole2":
            checks.check_ole2(real, result)
        elif ft.kind == "pdf":
            checks.check_pdf(real, result)
        if ft.kind in {"csv", "tsv"}:
            opener = (lambda: gzip.open(real, "rb")) if ft.detail.startswith("gzip") else None
            checks.check_tabular(real, ft, result, opener)
        if ft.family in {"text", "tabular", "code", "genomic"} and not ft.detail.startswith("gzip"):
            checks.check_secrets(real, ft, result, record.size)   # records its own skip reason for genomic data
        if ft.kind not in self.policy.allowed_kinds and ft.family != "code":
            result.findings.append(checks.Finding("TYPE_NOT_ALLOWED", f"{ft.kind} is not on the list of types allowed to pass automatically"))
        record.findings.extend(result.findings)
        record.ran.extend(result.ran)
        record.skipped.update(result.skipped)

    # -- malware pass over everything on disk ------------------------------------
    def malware_pass(self, roots: list[Path]) -> None:
        by_real = {str(p.resolve()): d for d, p in self.on_disk.items()}
        records = {r.path: r for r in self.records}
        infected, limited, error = {}, {}, None
        for root in roots:
            i, l, e = self.malware.scan_tree(root)
            infected.update(i); limited.update(l); error = error or e
        for record in self.records:
            if error:
                record.skipped["malware"] = error
                record.findings.append(checks.Finding("MALWARE_NOT_SCANNED", error))
            else:
                record.ran.append("malware")
        if error:
            return
        for path, signature in infected.items():
            display = by_real.get(str(Path(path).resolve()))
            if display:
                records[display].findings.append(checks.Finding("MALWARE_DETECTED", f"ClamAV: {signature}"))
        for path, signature in limited.items():
            display = by_real.get(str(Path(path).resolve()))
            if display:
                records[display].ran.remove("malware")
                records[display].skipped["malware"] = f"not fully scanned ({signature})"
                records[display].findings.append(checks.Finding("MALWARE_NOT_SCANNED", f"ClamAV could not fully scan: {signature}"))


def scan_submission(source: Path, out_dir: Path, policy: Policy | None = None,
                    limits: extract.Limits | None = None, malware: checks.MalwareScanner | None = None,
                    workdir: Path | None = None) -> dict:
    source = Path(source)
    policy = policy or Policy()
    limits = limits or extract.Limits()
    malware = malware or checks.MalwareScanner.detect()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.datetime.now(datetime.timezone.utc)

    with tempfile.TemporaryDirectory(prefix="refscan-", dir=workdir) as tmp:
        scanner = Scanner(policy, limits, malware, Path(tmp))
        if source.is_dir():
            files = sorted(p for p in source.rglob("*") if p.is_file() and not p.is_symlink())
            for link in sorted(p for p in source.rglob("*") if p.is_symlink()):
                scanner.records.append(FileRecord(link.relative_to(source).as_posix(), 0, "", FileType("link", "unknown"),
                                                  findings=[checks.Finding("ARCHIVE_LINK", "symbolic link in submission not followed")]))
            for path in files:
                scanner.add(path, path.relative_to(source).as_posix())
            roots = [source, Path(tmp)]
        else:
            scanner.add(source, source.name)
            roots = [source, Path(tmp)]
        scanner.malware_pass(roots)
        summary = write_reports(scanner, out_dir, source, policy, malware, started)
    return summary


def write_reports(scanner: Scanner, out_dir: Path, source: Path, policy: Policy,
                  malware: checks.MalwareScanner, started: datetime.datetime) -> dict:
    records = scanner.records
    with open(out_dir / "manifest.csv", "w", newline="", encoding="utf-8") as handle:
        w = csv.writer(handle)
        w.writerow(["path", "parent", "size_bytes", "sha256", "kind", "family", "detail"])
        for r in records:
            w.writerow([r.path, r.parent, r.size, r.sha256, r.file_type.kind, r.file_type.family, r.file_type.detail])

    file_verdicts = {}
    with open(out_dir / "findings.csv", "w", newline="", encoding="utf-8") as handle:
        w = csv.writer(handle)
        w.writerow(["path", "code", "action", "message", "detail"])
        for r in records:
            actions = []
            for f in r.findings:
                action = policy.action(f.code)
                actions.append(action)
                w.writerow([r.path, f.code, action, f.message, f.detail])
            file_verdicts[r.path] = verdict_for(actions)

    with open(out_dir / "coverage.csv", "w", newline="", encoding="utf-8") as handle:
        w = csv.writer(handle)
        w.writerow(["path", "kind", "verdict", "checks_run", "checks_not_run"])
        for r in records:
            w.writerow([r.path, r.file_type.kind, file_verdicts[r.path], ";".join(sorted(set(r.ran))),
                        "; ".join(f"{k}: {v}" for k, v in sorted(r.skipped.items()))])

    counts = {"files": len(records), "bytes": sum(r.size for r in records)}
    by_verdict: dict[str, int] = {}
    for v in file_verdicts.values():
        by_verdict[v] = by_verdict.get(v, 0) + 1
    by_code: dict[str, int] = {}
    for r in records:
        for f in r.findings:
            by_code[f.code] = by_code.get(f.code, 0) + 1
    by_kind: dict[str, int] = {}
    for r in records:
        by_kind[r.file_type.kind] = by_kind.get(r.file_type.kind, 0) + 1
    not_fully_checked = sorted({r.path for r in records if r.skipped})
    summary = {
        "tool": "reference-file-scanner", "tool_version": TOOL_VERSION,
        "source": str(source),
        "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "finished_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "verdict": verdict_for([{"PASS": "info", "REVIEW": "review", "FAIL": "block"}[v] for v in file_verdicts.values()]),
        "counts": counts, "files_by_verdict": by_verdict, "findings_by_code": dict(sorted(by_code.items())),
        "files_by_kind": dict(sorted(by_kind.items())),
        "coverage": {"files_not_fully_checked": len(not_fully_checked), "examples": not_fully_checked[:20]},
        "malware_scanner": malware.version or ("not installed" if not malware.binary else "clamscan"),
        "policy": policy.to_dict(),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary
