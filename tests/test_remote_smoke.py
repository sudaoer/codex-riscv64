from __future__ import annotations

import json
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import remote_smoke  # noqa: E402


class RemoteSmokeTests(unittest.TestCase):
    def test_parser_target_and_positive_timeout_scale(self) -> None:
        args = remote_smoke.parser().parse_args(
            ["--candidate-dir", "candidate", "--output", "report"]
        )
        self.assertEqual(args.validation_target, "native-k3")
        self.assertEqual(args.timeout_scale, 1.0)
        args = remote_smoke.parser().parse_args(
            [
                "--candidate-dir",
                "candidate",
                "--output",
                "report",
                "--validation-target",
                "qemu-system-riscv64",
                "--timeout-scale",
                "2.5",
            ]
        )
        self.assertEqual(args.validation_target, "qemu-system-riscv64")
        self.assertEqual(args.timeout_scale, 2.5)
        with self.assertRaises(SystemExit):
            remote_smoke.parser().parse_args(
                [
                    "--candidate-dir",
                    "candidate",
                    "--output",
                    "report",
                    "--timeout-scale",
                    "0",
                ]
            )

    @patch("remote_smoke.os.getpgrp", return_value=1)
    @patch("remote_smoke.os.getpgid", return_value=123)
    @patch("remote_smoke.subprocess.Popen")
    def test_run_scales_command_timeout(self, popen, _getpgid, _getpgrp) -> None:
        process = popen.return_value
        process.pid = 123
        process.returncode = 0
        process.communicate.return_value = ("ok", None)
        self.assertEqual(remote_smoke.run(["true"], timeout=4, timeout_scale=2.5), "ok")
        self.assertEqual(process.communicate.call_args.kwargs["timeout"], 10.0)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_code_mode_frame_deadline_scales(self) -> None:
        class Stdout:
            def fileno(self):
                return 7

        process = type("Process", (), {"stdout": Stdout()})()
        deadlines = []

        def fake_read_exact(_fd, length, deadline):
            deadlines.append((length, deadline))
            return struct.pack("<I", 2) if length == 4 else b"{}"

        with (
            patch.object(remote_smoke.time, "monotonic", return_value=100.0),
            patch.object(remote_smoke, "read_exact", side_effect=fake_read_exact),
        ):
            self.assertEqual(
                remote_smoke.read_frame(process, timeout=20, timeout_scale=2.5), {}
            )
        self.assertEqual(deadlines[0][1], 150.0)

    def test_qemu_host_info_records_guest_userns_setting(self) -> None:
        with patch.object(remote_smoke.Path, "read_text", return_value="0\n"):
            with patch.object(remote_smoke.os, "getuid", return_value=123):
                info = remote_smoke.host_info("qemu-system-riscv64")
        self.assertEqual(info["uid"], 123)
        self.assertEqual(info["apparmor_restrict_unprivileged_userns"], "0")

    def test_architecture_mismatch_writes_structured_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            argv = [
                "remote_smoke.py",
                "--candidate-dir",
                directory,
                "--output",
                str(output),
                "--validation-target",
                "qemu-system-riscv64",
            ]
            with (
                patch.object(sys, "argv", argv),
                patch.object(remote_smoke.platform, "machine", return_value="x86_64"),
            ):
                result = remote_smoke.main()
            report = json.loads(output.read_text())
        self.assertEqual(result, 1)
        self.assertEqual(report["validation_target"], "qemu-system-riscv64")
        self.assertEqual(report["overall"], "fail")
        self.assertIn("setup", report["details"])
        self.assertEqual(set(report["tests"].values()), {"not-run"})

    def test_extract_failure_does_not_skip_independent_proxy_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            argv = [
                "remote_smoke.py",
                "--candidate-dir",
                directory,
                "--output",
                str(output),
            ]

            def fake_run(command, **kwargs):
                return "ok"

            def fake_extract(archive, destination):
                if "app-server" in archive.name:
                    raise remote_smoke.SmokeError("broken app archive")
                destination.mkdir()

            with (
                patch.object(sys, "argv", argv),
                patch.object(remote_smoke.platform, "system", return_value="Linux"),
                patch.object(remote_smoke.platform, "machine", return_value="riscv64"),
                patch.object(remote_smoke, "run", side_effect=fake_run),
                patch.object(remote_smoke, "extract", side_effect=fake_extract),
                patch.object(remote_smoke, "code_mode_smoke", return_value="ok"),
            ):
                result = remote_smoke.main()
            report = json.loads(output.read_text())
        self.assertEqual(result, 1)
        self.assertEqual(report["tests"]["app-server-help"], "fail")
        self.assertEqual(report["tests"]["responses-proxy-help"], "pass")
        self.assertEqual(report["tests"]["code-mode-stdio"], "pass")

    def test_code_mode_timeout_includes_bounded_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            host = Path(directory) / "host.py"
            child_pid = Path(directory) / "child.pid"
            host.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib\n"
                "import subprocess\n"
                "import sys\n"
                "import time\n"
                f"child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])\n"
                f"pathlib.Path({str(child_pid)!r}).write_text(str(child.pid))\n"
                "sys.stderr.write('x' * 200000)\n"
                "sys.stderr.flush()\n"
                "time.sleep(60)\n"
            )
            host.chmod(0o755)
            with self.assertRaises(remote_smoke.SmokeError) as raised:
                remote_smoke.code_mode_smoke(host, timeout_scale=0.01)
            child = int(child_pid.read_text())
        self.assertIn("timed out reading a code-mode frame", str(raised.exception))
        self.assertIn("stderr:", str(raised.exception))
        self.assertLessEqual(len(str(raised.exception)), 70_000)
        for _ in range(20):
            try:
                with open(f"/proc/{child}/stat") as status:
                    state = status.read().split()[2]
            except FileNotFoundError:
                break
            if state == "Z":
                break
            time.sleep(0.01)
        else:
            self.fail(f"descendant process {child} survived group cleanup")


if __name__ == "__main__":
    unittest.main()
