import importlib.util
import shutil
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import yaml


def load_module(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class PythonCandidateBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        repo_root = Path(__file__).resolve().parents[1]
        cls.rebalance_mod = load_module(
            str(repo_root / "scripts" / "rebalance-python-candidate.py"),
            "rebalance_python_candidate",
        )

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="python-candidate-"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_rebalance_moves_python_packages_to_pip_and_keeps_native_conda(self):
        source = self.tmpdir / "candidate.yml"
        dest = self.tmpdir / "candidate.rebalanced.yml"
        source.write_text(
            yaml.safe_dump(
                {
                    "name": "example",
                    "channels": ["defaults", "conda-forge"],
                    "dependencies": [
                        "python",
                        "pip",
                        "libarrow",
                        "aiohttp",
                        "boto3",
                        "backports.zoneinfo",
                        "psycopg2",
                        "python-xxhash",
                        "antlr-python-runtime",
                        {"pip": ["fhir-resources", "psycopg2-binary", "python-tzdata"]},
                    ],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        self.rebalance_mod.main.__wrapped__ = None
        old_argv = __import__("sys").argv
        try:
            __import__("sys").argv = [
                "rebalance-python-candidate.py",
                "--input",
                str(source),
                "--output",
                str(dest),
            ]
            exit_code = self.rebalance_mod.main()
        finally:
            __import__("sys").argv = old_argv

        self.assertEqual(exit_code, 0)
        payload = yaml.safe_load(dest.read_text(encoding="utf-8"))
        deps = payload["dependencies"]
        self.assertIn("libarrow", deps)
        self.assertIn("backports.zoneinfo", deps)
        self.assertIn("psycopg2", deps)
        self.assertIn("python-xxhash", deps)
        self.assertNotIn("boto3", deps)
        pip_section = next(item["pip"] for item in deps if isinstance(item, dict) and "pip" in item)
        self.assertIn("boto3", pip_section)
        self.assertIn("aiohttp", pip_section)
        self.assertIn("antlr4-python3-runtime", pip_section)
        self.assertIn("fhir-resources", pip_section)
        self.assertNotIn("backports.zoneinfo", pip_section)
        self.assertNotIn("psycopg2", pip_section)
        self.assertNotIn("psycopg2-binary", pip_section)
        self.assertNotIn("python-tzdata", pip_section)

    def test_build_candidate_conda_probe_can_target_linux_subdir(self):
        repo_root = Path(__file__).resolve().parents[1]
        builder = load_module(
            str(repo_root / "scripts" / "build-candidate-from-env-artifacts.py"),
            "build_candidate_from_env_artifacts",
        )

        with mock.patch.object(builder.subprocess, "run") as run_mock:
            run_mock.return_value = mock.Mock(returncode=0, stdout='{"idna":[{}]}', stderr="")
            available = builder.conda_has_package(
                conda_bin="conda",
                channels=["conda-forge"],
                name="idna",
                target_subdir="linux-64",
            )

        self.assertTrue(available)
        cmd = run_mock.call_args.args[0]
        self.assertIn("--subdir", cmd)
        self.assertIn("linux-64", cmd)

    def test_rebalance_conda_probe_can_target_linux_subdir(self):
        repo_root = Path(__file__).resolve().parents[1]
        rebalance = load_module(
            str(repo_root / "scripts" / "rebalance-python-candidate.py"),
            "rebalance_python_candidate_subdir",
        )

        with mock.patch.object(rebalance.subprocess, "run") as run_mock:
            run_mock.return_value = mock.Mock(returncode=0, stdout='{"idna":[{}]}', stderr="")
            available = rebalance.conda_has_package(
                conda_bin="conda",
                channels=["conda-forge"],
                name="idna",
                target_subdir="linux-64",
            )

        self.assertTrue(available)
        cmd = run_mock.call_args.args[0]
        self.assertIn("--subdir", cmd)
        self.assertIn("linux-64", cmd)


if __name__ == "__main__":
    unittest.main()
