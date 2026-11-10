import importlib.util
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-requirements-pinned.py"


def load_script():
    spec = importlib.util.spec_from_file_location("check_requirements_pinned", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


checker = load_script()


def problems(text):
    return [line for _, line in checker.unpinned_requirements(text)[1]]


class PinnedRequirementsAccepted(unittest.TestCase):
    def test_exact_pins_and_their_variants_pass(self):
        text = "\n".join([
            "# resolved lock",
            "",
            "--index-url https://pypi.org/simple",
            "aiodns==4.0.4",
            "zope.interface==7.2",
            "ruamel.yaml.clib==0.2.12",
            "uvicorn[standard]==0.34.0",
            "pywin32==310 ; sys_platform == 'win32'",
            "numpy===2.1.3",
            "requests==2.32.3  # inline comment",
            "certifi==2026.10.18 \\",
            "    --hash=sha256:aaaa \\",
            "    --hash=sha256:bbbb",
            "localpkg @ https://example.invalid/localpkg-1.0-py3-none-any.whl#sha256=cccc",
        ])
        seen, found = checker.unpinned_requirements(text)
        self.assertEqual(found, [])
        self.assertEqual(seen, 9)


class UnpinnedRequirementsRejected(unittest.TestCase):
    def test_bare_names_are_rejected(self):
        self.assertEqual(problems("aiodns\naiohttp==3.14.5\ntorch\n"), ["aiodns", "torch"])

    def test_ranges_wildcards_and_compound_specifiers_are_rejected(self):
        text = "a>=1.0\nb~=2.1\nc!=3\nd<4\ne==1.*\nf==1.0,<2\ng[extra]\n"
        self.assertEqual(len(problems(text)), 7)

    def test_includes_and_editables_cannot_be_verified(self):
        text = "-r other.txt\n--requirement=other.txt\n-c constraints.txt\n-e ./src\n-rother.txt\n"
        self.assertEqual(len(problems(text)), 5)

    def test_reported_line_numbers_are_physical_lines(self):
        text = "# header\nok==1.0\n\nbad\n"
        self.assertEqual(checker.unpinned_requirements(text)[1], [(4, "bad")])

    def test_an_unpinned_name_list_submitted_as_a_lock_is_rejected(self):
        # The shape of the input that was once scanned and approved as a lock:
        # hundreds of bare names, no versions at all.
        names = ["aiodns", "aiohappyeyeballs", "aiohttp", "torch", "zope.interface"] * 60
        seen, found = checker.unpinned_requirements("\n".join(names) + "\n")
        self.assertEqual(seen, 300)
        self.assertEqual(len(found), 300)


class CommandLine(unittest.TestCase):
    def run_checker(self, content):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
            handle.write(content)
        try:
            return subprocess.run(
                [sys.executable, str(SCRIPT), handle.name], capture_output=True, text=True
            )
        finally:
            Path(handle.name).unlink()

    def test_pinned_file_exits_zero(self):
        result = self.run_checker("a==1\nb==2\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("2 requirements, all exactly pinned", result.stdout)

    def test_unpinned_file_exits_two_and_names_the_lines(self):
        result = self.run_checker("a==1\nb\nc>=3\n")
        self.assertEqual(result.returncode, 2)
        self.assertIn("2 of 3 requirements", result.stderr)
        self.assertIn("line 2: b", result.stderr)
        self.assertIn("line 3: c>=3", result.stderr)
        self.assertIn("--input-type requested-packages", result.stderr)

    def test_report_is_capped(self):
        result = self.run_checker("".join(f"pkg{i}\n" for i in range(50)))
        self.assertEqual(result.returncode, 2)
        self.assertIn("... and 30 more", result.stderr)

    def test_missing_file_is_a_clear_error_not_a_traceback(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "/nonexistent/requirements.txt"], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot read requirements-lock input", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_empty_file_is_rejected(self):
        result = self.run_checker("# nothing here\n\n")
        self.assertEqual(result.returncode, 2)
        self.assertIn("contains no requirements", result.stderr)


class Wiring(unittest.TestCase):
    def test_container_checks_requirements_lock_but_not_requested_packages(self):
        source = (ROOT / "scripts" / "run-python-ecs-task.sh").read_text()
        guard = re.search(
            r'if \[\[ "\$\{INPUT_TYPE\}" == "requirements-lock" \]\]; then\n(.*?)\n  fi', source, re.S
        )
        self.assertIsNotNone(guard, "requirements-lock-only guard not found")
        self.assertIn("check-requirements-pinned.py", guard.group(1))
        self.assertLess(
            source.index("check-requirements-pinned.py"),
            source.index('render_requirements_environment "${RUN_DIR}/requirements.lock.txt"'),
            "the pin check must run before the environment is rendered",
        )

    def test_image_ships_the_checker(self):
        dockerfile = (ROOT / "docker" / "python-linux.Dockerfile").read_text()
        self.assertIn("scripts/check-requirements-pinned.py /opt/package-scanner/scripts/", dockerfile)

    def test_cli_checks_before_uploading(self):
        source = (ROOT / "scripts" / "start-python-scan.sh").read_text()
        self.assertLess(
            source.index("check-requirements-pinned.py"),
            source.index('aws s3 cp "${SOURCE_REQUIREMENTS_FILE}"'),
        )


if __name__ == "__main__":
    unittest.main()
