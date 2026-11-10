"""The Python scan image must be reproducible: every tool version is pinned.

An unpinned micromamba once changed the format of conda-list.json between
rebuilds and made every materialize run report FAILED. These tests stop the
Dockerfile drifting back to floating versions.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "docker" / "python-linux.Dockerfile").read_text()
RPM_LOCK = ROOT / "docker" / "python-linux.rpm-lock.txt"
PIP_CONSTRAINTS = ROOT / "docker" / "python-tools.constraints.txt"


def entries(path):
    return [line.strip() for line in path.read_text().splitlines() if line.strip() and not line.startswith("#")]


class PythonImagePins(unittest.TestCase):
    def test_base_image_is_pinned_by_digest(self):
        base = re.search(r"^FROM\s+(\S+)", DOCKERFILE, re.M).group(1)
        self.assertRegex(base, r"@sha256:[0-9a-f]{64}$")

    def test_no_download_floats_to_latest(self):
        self.assertNotIn("/latest", DOCKERFILE)

    def test_micromamba_is_versioned_and_checksummed(self):
        self.assertRegex(DOCKERFILE, r"ARG MICROMAMBA_VERSION=\d+\.\d+\.\d+-\d+")
        self.assertIn("${MICROMAMBA_VERSION}", DOCKERFILE)
        self.assertIn("sha256sum -c -", DOCKERFILE)
        self.assertEqual(len(re.findall(r'MAMBA_SHA256="[0-9a-f]{64}"', DOCKERFILE)), 2)

    def test_trivy_is_versioned(self):
        self.assertRegex(DOCKERFILE, r"ARG TRIVY_VERSION=\d+\.\d+\.\d+")

    def test_dnf_installs_only_from_the_lock(self):
        dnf_lines = re.findall(r"dnf install -y ([^\n\\]*)", DOCKERFILE)
        self.assertEqual(dnf_lines, ["$(grep -v '^#' /tmp/python-linux.rpm-lock.txt) && "])
        self.assertIn("COPY docker/python-linux.rpm-lock.txt", DOCKERFILE)

    def test_rpm_lock_entries_are_exact_versions(self):
        lock = entries(RPM_LOCK)
        self.assertGreater(len(lock), 100)
        for entry in lock:
            with self.subTest(entry=entry):
                # name-[epoch:]version-release, no architecture suffix
                self.assertRegex(entry, r"^[A-Za-z0-9_.+-]+-(\d+:)?[0-9][^-]*-[^-]+$")
                self.assertNotRegex(entry, r"\.(x86_64|aarch64|noarch)$")
        for required in ("gcc-", "gcc-c++-", "python3.11-", "python3.11-pip-", "krb5-devel-", "libpq-devel-", "glibc-"):
            with self.subTest(required=required):
                self.assertTrue(any(e.startswith(required) and e[len(required)].isdigit() for e in lock))

    def test_pip_install_uses_the_constraints(self):
        self.assertIn("COPY docker/python-tools.constraints.txt", DOCKERFILE)
        self.assertRegex(DOCKERFILE, r"pip install[^\n]*\\\n\s*-c /tmp/python-tools.constraints.txt")

    def test_pip_constraints_are_exact_and_cover_the_requested_tools(self):
        constraints = entries(PIP_CONSTRAINTS)
        names = set()
        for entry in constraints:
            with self.subTest(entry=entry):
                self.assertRegex(entry, r"^[a-z0-9][a-z0-9.-]*==[0-9][^\s,;<>=!~*]*$")
            names.add(entry.split("==")[0])
        for tool in ("awscli", "boto3", "conda-pack", "cyclonedx-bom", "pyyaml", "safety"):
            with self.subTest(tool=tool):
                self.assertIn(tool, names)


if __name__ == "__main__":
    unittest.main()
