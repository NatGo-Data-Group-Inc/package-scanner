"""Reference-file scanner: identification, safe unpacking, checks, policy, coverage and the CLI."""
import csv
import gzip
import io
import json
import os
import pickle
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from package_scanner.reference_files import checks, extract  # noqa: E402
from package_scanner.reference_files.identify import extension_mismatch, identify  # noqa: E402
from package_scanner.reference_files.policy import Policy  # noqa: E402
from package_scanner.reference_files.scan import scan_submission  # noqa: E402

ELF = b"\x7fELF\x02\x01\x01" + b"\x00" * 64
FAKE_CLAMSCAN = """#!/usr/bin/env python3
import os, sys
if "--version" in sys.argv:
    print("ClamAV 1.4.0 (test double)"); sys.exit(0)
root = sys.argv[-1]
paths = [root] if os.path.isfile(root) else [os.path.join(d, f) for d, _, fs in os.walk(root) for f in fs]
hit = False
for p in paths:
    data = open(p, "rb").read()
    if b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE" in data:
        print(p + ": Eicar-Test-Signature FOUND"); hit = True
    elif b"PRETEND-TOO-BIG" in data:
        print(p + ": Heuristics.Limits.Exceeded.MaxFileSize FOUND"); hit = True
sys.exit(1 if hit else 0)
"""


def zip_bytes(members: dict, encrypted: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    data = bytearray(buf.getvalue())
    if encrypted:
        # zipfile clears flag bits on write, so set "encrypted" (bit 0) in every
        # local header (flags at +6) and central directory entry (flags at +8).
        for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            start = 0
            while (pos := data.find(signature, start)) != -1:
                data[pos + offset] |= 0x01
                start = pos + 4
    return bytes(data)


def tar_gz_bytes(members: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def make_submission(root: Path) -> Path:
    sub = root / "submission"
    sub.mkdir()
    (sub / "clean.csv").write_text("sample,count\nA,1\nB,-3\n")
    (sub / "formula.csv").write_text('name,value\nx,"=HYPERLINK(""http://evil"",""click"")"\ny,2\n')
    (sub / "notes.txt").write_text("deploy key AKIAABCDEFGHIJKLMNOP for later\n")
    (sub / "seq.fa").write_text(">chr1\nACGTACGT\n")
    (sub / "variants.vcf.gz").write_bytes(gzip.compress(b"##fileformat=VCFv4.2\n#CHROM\tPOS\n1\t100\n"))
    (sub / "macro.xlsm").write_bytes(zip_bytes({"[Content_Types].xml": b"<Types/>", "xl/workbook.xml": b"<w/>", "xl/vbaProject.bin": b"VBA"}))
    (sub / "links.xlsx").write_bytes(zip_bytes({"[Content_Types].xml": b"<Types/>", "xl/workbook.xml": b"<w/>",
                                                "xl/externalLinks/externalLink1.xml": b"<x/>"}))
    (sub / "report.pdf").write_bytes(b"%PDF-1.4\n1 0 obj << /OpenAction 2 0 R /JS (app.alert(1)) >>\nendobj\n")
    (sub / "program.csv").write_bytes(ELF)
    (sub / "model.pkl").write_bytes(pickle.dumps({"a": 1}, protocol=4))
    (sub / "eicar.txt").write_text("X5O!P%@AP EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*\n")
    (sub / "empty.dat").write_bytes(b"")
    (sub / "blob.bin").write_bytes(bytes(range(256)) * 4)
    (sub / "nested.zip").write_bytes(zip_bytes({"inner.tar.gz": tar_gz_bytes({"deep/deep.csv": b"a,b\n1,2\n", "deep/payload": ELF})}))
    evil = io.BytesIO()
    with tarfile.open(fileobj=evil, mode="w") as tar:
        info = tarfile.TarInfo("../escape.txt"); info.size = 2; tar.addfile(info, io.BytesIO(b"hi"))
        link = tarfile.TarInfo("etc-passwd"); link.type = tarfile.SYMTYPE; link.linkname = "/etc/passwd"; tar.addfile(link)
        ok = tarfile.TarInfo("fine.txt"); ok.size = 3; tar.addfile(ok, io.BytesIO(b"ok\n"))
    (sub / "evil.tar").write_bytes(evil.getvalue())
    (sub / "locked.zip").write_bytes(zip_bytes({"secret.csv": b"a,b\n"}, encrypted=True))
    return sub


class Identify(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="refid-"))
        self.sub = make_submission(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_content_decides_the_type(self):
        expected = {
            "clean.csv": "csv", "seq.fa": "fasta", "variants.vcf.gz": "vcf", "macro.xlsm": "xlsx", "links.xlsx": "xlsx",
            "report.pdf": "pdf", "program.csv": "executable", "model.pkl": "pickle", "empty.dat": "empty",
            "blob.bin": "unknown", "nested.zip": "zip", "evil.tar": "tar", "notes.txt": "text",
        }
        for name, kind in expected.items():
            with self.subTest(name=name):
                self.assertEqual(identify(self.sub / name).kind, kind)

    def test_genomic_and_bam_inside_gzip(self):
        bam = self.tmp / "reads.bam"
        bam.write_bytes(gzip.compress(b"BAM\x01" + b"\x00" * 100))
        self.assertEqual(identify(bam).kind, "bam")
        self.assertEqual(identify(self.sub / "variants.vcf.gz").family, "genomic")

    def test_extension_mismatch(self):
        self.assertIn("suggests csv", extension_mismatch(self.sub / "program.csv", identify(self.sub / "program.csv")))
        self.assertIsNone(extension_mismatch(self.sub / "clean.csv", identify(self.sub / "clean.csv")))
        self.assertIsNone(extension_mismatch(self.sub / "variants.vcf.gz", identify(self.sub / "variants.vcf.gz")))


class SafeUnpacking(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="refx-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_member_paths_that_escape_are_refused(self):
        for bad in ("../x", "/etc/passwd", "a/../../x", "C:/Windows/x", "..\\x"):
            with self.subTest(bad=bad):
                self.assertIsNone(extract.safe_member_path(bad))
        self.assertEqual(str(extract.safe_member_path("./a/b.csv")), "a/b.csv")

    def test_zip_bomb_stops_at_the_ratio_limit(self):
        bomb = self.tmp / "bomb.zip"
        bomb.write_bytes(zip_bytes({"zeros.bin": b"\x00" * (8 * 1024 * 1024)}))
        limits = extract.Limits(max_ratio=50)
        result = extract.extract_zip(bomb, self.tmp / "out", extract.Budget(limits), limits)
        self.assertEqual([i.code for i in result.issues], ["ARCHIVE_LIMIT"])
        self.assertEqual(result.extracted, [])

    def test_total_budget_is_enforced(self):
        archive = self.tmp / "big.zip"
        archive.write_bytes(zip_bytes({"a.bin": os.urandom(2 * 1024 * 1024)}))
        limits = extract.Limits(max_total_bytes=1024 * 1024)
        result = extract.extract_zip(archive, self.tmp / "out", extract.Budget(limits), limits)
        self.assertEqual([i.code for i in result.issues], ["ARCHIVE_LIMIT"])


class FullScan(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="refscan-"))
        self.sub = make_submission(self.tmp)
        fake = self.tmp / "clamscan"
        fake.write_text(FAKE_CLAMSCAN.replace("#!/usr/bin/env python3", f"#!{sys.executable}"))
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.malware = checks.MalwareScanner(binary=str(fake), version="ClamAV test double")
        self.out = self.tmp / "out"
        self.summary = scan_submission(self.sub, self.out, malware=self.malware)
        self.findings = list(csv.DictReader(open(self.out / "findings.csv")))
        self.coverage = {r["path"]: r for r in csv.DictReader(open(self.out / "coverage.csv"))}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def codes(self, path):
        return {f["code"] for f in self.findings if f["path"] == path}

    def test_overall_verdict_fails(self):
        self.assertEqual(self.summary["verdict"], "FAIL")

    def test_each_hazard_is_caught(self):
        expectations = {
            "formula.csv": {"CSV_FORMULA"},
            "notes.txt": {"SECRET"},
            "macro.xlsm": {"OFFICE_MACROS"},
            "links.xlsx": {"OFFICE_EXTERNAL_LINK"},
            "report.pdf": {"PDF_ACTIVE_CONTENT"},
            "program.csv": {"EXECUTABLE", "TYPE_MISMATCH"},
            "model.pkl": {"PICKLE"},
            "eicar.txt": {"MALWARE_DETECTED"},
            "blob.bin": {"TYPE_NOT_ALLOWED"},
            "evil.tar": {"ARCHIVE_TRAVERSAL", "ARCHIVE_LINK"},
            "locked.zip": {"ARCHIVE_ENCRYPTED"},
            "nested.zip!/inner.tar.gz!/deep/payload": {"EXECUTABLE"},
        }
        for path, codes in expectations.items():
            with self.subTest(path=path):
                self.assertTrue(codes <= self.codes(path), f"{path}: {self.codes(path)}")

    def test_clean_files_pass(self):
        for path in ("clean.csv", "seq.fa", "variants.vcf.gz", "empty.dat", "evil.tar!/fine.txt", "nested.zip!/inner.tar.gz!/deep/deep.csv"):
            with self.subTest(path=path):
                self.assertEqual(self.coverage[path]["verdict"], "PASS", self.codes(path))

    def test_negative_numbers_are_not_formulas(self):
        self.assertNotIn("CSV_FORMULA", self.codes("clean.csv"))

    def test_nested_archives_are_inventoried_with_hashes(self):
        manifest = {r["path"]: r for r in csv.DictReader(open(self.out / "manifest.csv"))}
        member = manifest["nested.zip!/inner.tar.gz!/deep/deep.csv"]
        self.assertEqual(member["parent"], "nested.zip!/inner.tar.gz")
        self.assertRegex(member["sha256"], r"^[0-9a-f]{64}$")

    def test_coverage_records_what_ran(self):
        self.assertIn("malware", self.coverage["clean.csv"]["checks_run"])
        self.assertIn("formula-injection", self.coverage["clean.csv"]["checks_run"])
        self.assertIn("secrets: not applied to genomic", self.coverage["seq.fa"]["checks_not_run"])
        self.assertEqual(self.summary["malware_scanner"], "ClamAV test double")

    def test_policy_is_recorded_in_the_summary(self):
        self.assertEqual(self.summary["policy"]["actions"]["OFFICE_MACROS"], "block")


class PolicyAndCoverage(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="refpol-"))
        self.sub = self.tmp / "sub"
        self.sub.mkdir()
        (self.sub / "formula.csv").write_text("a\n=1+1\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_without_clamav_nothing_passes_silently(self):
        summary = scan_submission(self.sub, self.tmp / "out", malware=checks.MalwareScanner(binary=None))
        self.assertEqual(summary["verdict"], "REVIEW")
        self.assertIn("MALWARE_NOT_SCANNED", summary["findings_by_code"])
        self.assertEqual(summary["coverage"]["files_not_fully_checked"], 1)

    def test_policy_file_overrides_actions(self):
        policy_file = self.tmp / "policy.json"
        policy_file.write_text(json.dumps({"name": "test", "actions": {"CSV_FORMULA": "info", "MALWARE_NOT_SCANNED": "info"}}))
        summary = scan_submission(self.sub, self.tmp / "out", policy=Policy.load(policy_file),
                                  malware=checks.MalwareScanner(binary=None))
        self.assertEqual(summary["verdict"], "PASS")

    def test_invalid_policy_action_is_rejected(self):
        bad = self.tmp / "bad.json"
        bad.write_text(json.dumps({"actions": {"CSV_FORMULA": "ignore"}}))
        with self.assertRaises(ValueError):
            Policy.load(bad)

    def test_unknown_finding_code_defaults_to_review(self):
        self.assertEqual(Policy().action("SOMETHING_NEW"), "review")


class CommandLine(unittest.TestCase):
    def test_exit_codes_and_output(self):
        tmp = Path(tempfile.mkdtemp(prefix="refcli-"))
        try:
            (tmp / "in").mkdir()
            (tmp / "in" / "a.csv").write_text("x,y\n1,2\n")
            result = subprocess.run([sys.executable, str(ROOT / "scripts" / "scan-reference-files.py"), str(tmp / "in"),
                                     "--out", str(tmp / "out")], capture_output=True, text=True, env={**os.environ, "PATH": "/usr/bin:/bin"})
            self.assertEqual(result.returncode, 3, result.stderr)   # REVIEW: no ClamAV on this PATH
            self.assertIn("verdict: REVIEW", result.stdout)
            self.assertIn("not installed", result.stdout)
            for name in ("manifest.csv", "findings.csv", "coverage.csv", "summary.json"):
                self.assertTrue((tmp / "out" / name).exists())
            missing = subprocess.run([sys.executable, str(ROOT / "scripts" / "scan-reference-files.py"), str(tmp / "nope"),
                                      "--out", str(tmp / "o")], capture_output=True, text=True)
            self.assertEqual(missing.returncode, 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
