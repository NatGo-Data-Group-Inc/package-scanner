"""What each finding means for approval. Kept in data, not code, until Cyber's criteria are agreed.

A finding's action is one of:
  block   the submission fails
  review  a person must look before it can pass
  info    reported only

A policy file (JSON) can override any action and the list of file kinds allowed
to pass automatically. Anything not explicitly allowed needs review: unknown
must never mean pass.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ACTIONS = {
    "MALWARE_DETECTED": "block",
    "EXECUTABLE": "block",
    "PICKLE": "block",
    "OFFICE_MACROS": "block",
    "ARCHIVE_TRAVERSAL": "block",
    "ARCHIVE_LIMIT": "block",
    "SCRIPT": "review",
    "OFFICE_EXTERNAL_LINK": "review",
    "OFFICE_EMBEDDED_OBJECT": "review",
    "OFFICE_REMOTE_TEMPLATE": "review",
    "OFFICE_DDE": "review",
    "LEGACY_OFFICE_UNCHECKED": "review",
    "PDF_ACTIVE_CONTENT": "review",
    "PDF_ENCRYPTED": "review",
    "ARCHIVE_ENCRYPTED": "review",
    "ARCHIVE_LINK": "review",
    "ARCHIVE_SPECIAL": "review",
    "ARCHIVE_CORRUPT": "review",
    "ARCHIVE_UNSUPPORTED": "review",
    "CSV_FORMULA": "review",
    "SECRET": "review",
    "TYPE_MISMATCH": "review",
    "TYPE_NOT_ALLOWED": "review",
    "UNREADABLE": "review",
    "MALWARE_NOT_SCANNED": "review",
    "MALWARE_SCAN_ERROR": "review",
}

DEFAULT_ALLOWED_KINDS = sorted({
    "text", "csv", "tsv", "json", "fasta", "fastq", "vcf", "bam", "cram", "hdf5", "parquet", "sqlite",
    "pdf", "xlsx", "docx", "pptx", "png", "jpeg", "gif", "tiff", "empty",
})

RANK = {"info": 0, "review": 1, "block": 2}
VERDICT = {0: "PASS", 1: "REVIEW", 2: "FAIL"}


@dataclass
class Policy:
    name: str = "default (provisional, pending Cyber acceptance criteria)"
    actions: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ACTIONS))
    allowed_kinds: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_KINDS))

    def action(self, code: str) -> str:
        return self.actions.get(code, "review")   # an unrecognised finding is never ignored

    @classmethod
    def load(cls, path: Path | None) -> "Policy":
        policy = cls()
        if path is None:
            return policy
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        policy.name = data.get("name", str(path))
        for code, action in (data.get("actions") or {}).items():
            if action not in RANK:
                raise ValueError(f"policy {path}: action for {code} must be block, review or info, not {action!r}")
            policy.actions[code] = action
        if "allowed_kinds" in data:
            policy.allowed_kinds = sorted(set(data["allowed_kinds"]))
        return policy

    def to_dict(self) -> dict:
        return {"name": self.name, "actions": dict(sorted(self.actions.items())), "allowed_kinds": self.allowed_kinds}


def verdict_for(actions: list[str]) -> str:
    return VERDICT[max((RANK[a] for a in actions), default=0)]
