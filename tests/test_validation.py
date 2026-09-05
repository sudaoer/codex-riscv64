from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import validate  # noqa: E402
from release_lib import ReleaseError, REQUIRED_K3_TESTS  # noqa: E402
from validation_ssh import SSHConnection  # noqa: E402


class ValidationTests(unittest.TestCase):
    def test_parser_keeps_legacy_k3_defaults(self) -> None:
        args = validate.parser().parse_args(["--run-id", "42"])
        self.assertEqual(args.target, "k3")
        self.assertEqual(args.ssh_host, "k3")
        self.assertEqual(args.qemu_cpus, 4)
        self.assertEqual(args.qemu_memory_mib, 4096)
        self.assertIsNone(args.timeout_scale)

    def test_qemu_defaults_and_scale(self) -> None:
        args = validate.parser().parse_args(
            ["--run-id", "42", "--target", "qemu", "--timeout-scale", "2.5"]
        )
        self.assertEqual(args.target, "qemu")
        self.assertEqual(args.timeout_scale, 2.5)
        self.assertEqual(validate.target_name(args.target), "qemu-system-riscv64")

    def test_timeout_scale_must_be_positive_and_finite(self) -> None:
        for value in ("0", "-1", "nan", "inf"):
            with self.assertRaises(SystemExit):
                validate.parser().parse_args(
                    ["--run-id", "42", "--timeout-scale", value]
                )

    def test_remote_directory_requires_strict_ascii_suffix(self) -> None:
        self.assertEqual(
            validate.checked_remote_directory("/tmp/codex-riscv64-k3.A1b2"),
            "/tmp/codex-riscv64-k3.A1b2",
        )
        for value in (
            "/tmp/codex-riscv64-k3.",
            "/tmp/codex-riscv64-k3.é",
            "/tmp/codex-riscv64-k3.A/../x",
        ):
            with self.assertRaises(ReleaseError):
                validate.checked_remote_directory(value)

    def test_ssh_rejects_option_like_host_and_quotes_remote_argv(self) -> None:
        with self.assertRaises(ValueError):
            SSHConnection("-oProxyCommand=bad")
        with self.assertRaises(ValueError):
            SSHConnection("host:/tmp/remote")
        SSHConnection("codex@[::1]")
        connection = SSHConnection("guest", port=2222)
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch("validation_ssh.subprocess.run", return_value=completed) as mocked:
            connection.run(
                ["printf", "hello world", "$(touch /tmp/nope)"], capture=True
            )
        command = mocked.call_args.args[0]
        self.assertEqual(command[-2], "guest")
        self.assertEqual(command[-1], "printf 'hello world' '$(touch /tmp/nope)'")
        self.assertIn("-p", command)
        self.assertIn("2222", command)

    def test_setup_failure_writes_report_and_never_reaches_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            args = validate.parser().parse_args(
                ["--run-id", "42", "--output", str(output)]
            )
            policy = SimpleNamespace(
                distribution=SimpleNamespace(repository="owner/repo")
            )

            def failed_run(
                *_args: object, **_kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                raise ReleaseError("preflight failed")

            with (
                patch("validate.load_policy", return_value=policy),
                patch("validate.run", side_effect=failed_run) as run_mock,
            ):
                with self.assertRaises(ReleaseError):
                    validate._validate(args)
            report = json.loads(output.read_text())
            self.assertEqual(report["overall"], "fail")
            self.assertEqual(report["validation_target"], "native-k3")
            self.assertEqual(set(report["tests"]), set(REQUIRED_K3_TESTS))
            self.assertFalse(
                any("workflow" in str(call) for call in run_mock.call_args_list)
            )

    def test_malformed_ssh_host_fails_before_github_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            args = validate.parser().parse_args(
                [
                    "--run-id",
                    "42",
                    "--ssh-host",
                    "host:/tmp/remote",
                    "--output",
                    str(output),
                ]
            )
            policy = SimpleNamespace(
                distribution=SimpleNamespace(repository="owner/repo")
            )
            with (
                patch("validate.load_policy", return_value=policy),
                patch("validate.run") as run_mock,
            ):
                with self.assertRaises(ValueError):
                    validate._validate(args)
            self.assertEqual(json.loads(output.read_text())["overall"], "fail")
            run_mock.assert_not_called()

    def test_guest_report_requires_all_checks(self) -> None:
        raw = {
            "validation_target": "qemu-system-riscv64",
            "host": {"system": "Linux", "machine": "riscv64"},
            "overall": "pass",
            "tests": {name: "pass" for name in REQUIRED_K3_TESTS},
        }
        validate._assert_guest_report("qemu", raw)
        raw["validation_target"] = "native-k3"
        with self.assertRaises(ReleaseError):
            validate._assert_guest_report("qemu", raw)
        raw["validation_target"] = "qemu-system-riscv64"
        raw["tests"]["installer"] = "fail"
        with self.assertRaises(ReleaseError):
            validate._assert_guest_report("qemu", raw)

    def test_smoke_report_error_keeps_raw_tests_for_failure_report(self) -> None:
        raw = {
            "validation_target": "qemu-system-riscv64",
            "overall": "pass",
            "host": {"system": "Linux", "machine": "riscv64"},
            "tests": {name: "pass" for name in REQUIRED_K3_TESTS},
            "details": {"installer": "completed"},
        }
        error = validate.SmokeReportError("smoke exited with status 1", raw)
        report = validate._failure_report("qemu", error)
        report.update(error.report)
        report["overall"] = "fail"
        self.assertEqual(report["tests"]["installer"], "pass")
        self.assertEqual(report["overall"], "fail")

    def test_validation_gate_marks_saved_report_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            args = validate.parser().parse_args(
                ["--run-id", "42", "--output", str(output), "--skip-attestation"]
            )
            policy = SimpleNamespace(
                distribution=SimpleNamespace(repository="owner/repo")
            )
            manifest = SimpleNamespace(release_tag="riscv-v1.2.3-r1")
            candidate = {
                "candidate_head_sha": "b" * 40,
                "release_tag": manifest.release_tag,
                "assets": {"asset": {"sha256": "a" * 64, "size": 1}},
            }
            raw = {
                "schema_version": 1,
                "validation_target": "native-k3",
                "overall": "pass",
                "host": {"system": "Linux", "machine": "riscv64"},
                "tests": {name: "pass" for name in REQUIRED_K3_TESTS},
                "details": {"installer": "ok"},
                "finished_at": "2026-09-05T00:00:00+00:00",
            }
            run_info = {"html_url": "https://example.test/run/42"}
            with (
                patch("validate.load_policy", return_value=policy),
                patch(
                    "validate.run",
                    return_value=subprocess.CompletedProcess([], 0, "token\n", ""),
                ) as run_mock,
                patch("validate.github_run", return_value=run_info),
                patch("validate.validate_candidate_run"),
                patch("validate.load_manifest", return_value=manifest),
                patch("validate.validate_candidate", return_value=candidate),
                patch("validate.verify_latest_manifest"),
                patch("validate.SSHConnection"),
                patch("validate._run_smoke", return_value=raw),
                patch(
                    "validate.preflight_publish",
                    side_effect=ReleaseError("newer release appeared"),
                ),
            ):
                with self.assertRaises(ReleaseError):
                    validate._validate(args)
            report = json.loads(output.read_text())
            self.assertEqual(report["overall"], "fail")
            self.assertEqual(report["tests"]["installer"], "pass")
            self.assertIn("newer release appeared", report["details"]["validator"])
            self.assertFalse(
                any("workflow" in str(call) for call in run_mock.call_args_list)
            )


if __name__ == "__main__":
    unittest.main()
