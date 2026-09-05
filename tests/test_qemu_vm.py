from __future__ import annotations

import hashlib
import io
import json
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from qemu_vm import QemuConfig, QemuVM, cached_image, stop_process
from release_lib import ReleaseError


class ImageCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.payload = b"fixture cloud image bytes"
        self.digest = hashlib.sha256(self.payload).hexdigest()
        self.path = self.root / f"{self.digest}.qcow2"
        self.options = {
            "deadline": time.monotonic() + 30,
            "expected_sha256": self.digest,
            "url": "https://example.invalid/pinned.img",
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_cold_download_then_verified_cache_reuse(self) -> None:
        with patch(
            "qemu_vm.urllib.request.urlopen", return_value=io.BytesIO(self.payload)
        ) as download:
            first, hit = cached_image(self.root, **self.options)
            self.assertFalse(hit)
            second, hit = cached_image(self.root, **self.options)
            self.assertTrue(hit)
        self.assertEqual(first, second)
        self.assertEqual(first.read_bytes(), self.payload)
        download.assert_called_once()

    def test_corrupt_cache_is_replaced_by_verified_download(self) -> None:
        self.path.write_bytes(b"corrupt cache")
        with patch(
            "qemu_vm.urllib.request.urlopen", return_value=io.BytesIO(self.payload)
        ):
            result, hit = cached_image(self.root, **self.options)
        self.assertFalse(hit)
        self.assertEqual(result.read_bytes(), self.payload)

    def test_bad_download_never_becomes_a_cache_entry(self) -> None:
        with patch(
            "qemu_vm.urllib.request.urlopen", return_value=io.BytesIO(b"wrong bytes")
        ):
            with self.assertRaisesRegex(ReleaseError, "SHA-256"):
                cached_image(self.root, **self.options)
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.root.glob("*.part")), [])

    def test_interrupted_download_removes_partial_file(self) -> None:
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.side_effect = [self.payload, KeyboardInterrupt()]
        with patch("qemu_vm.urllib.request.urlopen", return_value=response):
            with self.assertRaises(KeyboardInterrupt):
                cached_image(self.root, **self.options)
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.root.glob("*.part")), [])

    def test_expired_deadline_does_not_download(self) -> None:
        self.options["deadline"] = time.monotonic() - 1
        with patch("qemu_vm.urllib.request.urlopen") as download:
            with self.assertRaisesRegex(ReleaseError, "time limit"):
                cached_image(self.root, **self.options)
        download.assert_not_called()
        self.assertEqual(list(self.root.glob("*.part")), [])


class GuestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.efi = self.root / "firmware"
        self.efi.mkdir()
        (self.efi / "RISCV_VIRT_CODE.fd").write_bytes(b"code")
        (self.efi / "RISCV_VIRT_VARS.fd").write_bytes(b"variables")
        self.vm = QemuVM(
            QemuConfig(cache_dir=self.root / "cache", efi_dir=self.efi, boot_timeout=3),
            workspace=self.root / "workspace",
            log_dir=self.root / "logs",
            deadline=time.monotonic() + 60,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_seed_has_ephemeral_keys_ordinary_user_and_guest_local_userns(self) -> None:
        self.vm.work.mkdir(parents=True)

        def command(arguments: list[str]) -> str:
            if arguments[0] == "ssh-keygen":
                path = Path(arguments[-1])
                path.write_text("fixture-private-key")
                path.with_suffix(".pub").write_text(f"ssh-ed25519 {path.name}-public")
            return ""

        with patch.object(self.vm, "command", side_effect=command) as run:
            self.vm.make_seed()
        data = json.loads((self.vm.work / "user-data").read_text().split("\n", 1)[1])
        self.assertEqual(data["users"][0]["name"], "codex")
        self.assertNotIn("sudo", data["users"][0])
        self.assertTrue(data["users"][0]["lock_passwd"])
        self.assertFalse(data["ssh_pwauth"])
        self.assertFalse(data["package_update"])
        self.assertIn(
            "kernel.apparmor_restrict_unprivileged_userns=0", data["runcmd"][0][2]
        )
        self.assertEqual(data["ssh_keys"]["ed25519_private"], "fixture-private-key")
        self.assertEqual(data["runcmd"][-1][:2], ["python3", "-c"])
        clock_code = data["runcmd"][-1][2]
        with (
            patch("time.clock_gettime", return_value=80),
            patch("time.clock_settime") as set_clock,
        ):
            exec(clock_code, {})
        set_clock.assert_called_once_with(
            time.CLOCK_REALTIME, self.vm.metadata["clock_seed_epoch"] + 80
        )
        self.assertEqual(run.call_args.args[0][0], "cloud-localds")
        self.assertEqual((self.vm.work / "user-data").stat().st_mode & 0o777, 0o600)

    def test_qemu_uses_writable_overlay_and_private_vars_without_host_mounts(
        self,
    ) -> None:
        arguments = self.vm.launch_arguments(23456)
        block_devices = [
            json.loads(arguments[i + 1])
            for i, arg in enumerate(arguments)
            if arg == "-blockdev"
        ]
        devices = {device["node-name"]: device for device in block_devices}
        self.assertTrue(devices["efi-code"]["read-only"])
        self.assertEqual(Path(devices["efi-vars"]["filename"]).parent, self.vm.work)
        self.assertEqual(Path(devices["root"]["file"]["filename"]).parent, self.vm.work)
        self.assertTrue(devices["seed"]["read-only"])
        self.assertIn("tcg,thread=multi", arguments)
        self.assertIn("rv64,v=false", arguments)
        self.assertIn(
            "user,id=net0,restrict=on,hostfwd=tcp:127.0.0.1:23456-:22", arguments
        )
        self.assertNotIn("-virtfs", arguments)

    def test_qemu_blockdev_paths_round_trip_commas(self) -> None:
        self.vm.work = self.root / "space and,comma"
        args = self.vm.launch_arguments(23456)
        blocks = [
            json.loads(args[i + 1]) for i, arg in enumerate(args) if arg == "-blockdev"
        ]
        root = next(block for block in blocks if block["node-name"] == "root")
        self.assertEqual(root["file"]["filename"], str(self.vm.work / "root.qcow2"))

    def test_rtc_uses_explicit_host_utc(self) -> None:
        args = self.vm.launch_arguments(23456)
        rtc = args[args.index("-rtc") + 1]
        self.assertEqual(
            rtc, f"base={self.vm.metadata['rtc_base_utc'][:-1]},clock=host"
        )

    def test_stale_guest_clock_fails_before_smoke(self) -> None:
        self.vm.connection = Mock()
        self.vm.connection.run.return_value = subprocess.CompletedProcess(
            [], 0, str(time.time() - 40 * 86400), ""
        )
        with self.assertRaisesRegex(ReleaseError, "clock differs"):
            self.vm.check_guest(time.monotonic() + 30)

    def test_current_guest_clock_is_recorded(self) -> None:
        self.vm.connection = Mock()
        self.vm.connection.run.return_value = subprocess.CompletedProcess(
            [], 0, str(time.time()), ""
        )
        self.vm.check_guest(time.monotonic() + 30)
        self.assertLess(abs(self.vm.metadata["guest_clock_skew_seconds"]), 1)

    def test_ready_requires_cloud_init_and_guest_prerequisites(self) -> None:
        self.vm.process = Mock()
        self.vm.process.poll.return_value = None
        self.vm.connection = Mock()
        self.vm.connection.run.return_value = subprocess.CompletedProcess(
            [], 0, '{"status":"done"}', ""
        )
        with patch.object(self.vm, "check_guest") as check:
            self.vm.wait_ready()
        check.assert_called_once()

    def test_failed_cloud_init_cannot_be_ready(self) -> None:
        self.vm.process = Mock()
        self.vm.process.poll.return_value = None
        self.vm.connection = Mock()
        self.vm.connection.run.return_value = subprocess.CompletedProcess(
            [], 2, '{"status":"done","errors":["sysctl failed"]}', ""
        )
        with patch.object(self.vm, "check_guest") as check:
            with self.assertRaisesRegex(ReleaseError, "cloud-init failed"):
                self.vm.wait_ready()
        check.assert_not_called()

    def test_guest_boot_wait_has_deadline(self) -> None:
        self.vm.process = Mock()
        self.vm.process.poll.return_value = None
        self.vm.connection = Mock()
        self.vm.connection.run.return_value = subprocess.CompletedProcess(
            [], 255, "", "connection refused"
        )
        clock = [0.0]
        with (
            patch("qemu_vm.time.monotonic", side_effect=lambda: clock[0]),
            patch(
                "qemu_vm.time.sleep",
                side_effect=lambda duration: clock.__setitem__(0, clock[0] + duration),
            ),
        ):
            with self.assertRaisesRegex(ReleaseError, "within 3s"):
                self.vm.wait_ready()
        self.assertEqual(clock[0], 3)

    def test_qemu_exit_stops_readiness_polling(self) -> None:
        self.vm.process = Mock()
        self.vm.process.poll.return_value = 1
        self.vm.connection = Mock()
        with self.assertRaisesRegex(ReleaseError, "exited during boot"):
            self.vm.wait_ready()
        self.vm.connection.run.assert_not_called()

    def test_failed_enter_stops_vm_and_preserves_logs(self) -> None:
        process = Mock()

        def start() -> None:
            self.vm.process = process
            (self.vm.log_dir / "serial.log").write_text("guest boot diagnostics")

        def command(arguments: list[str]) -> str:
            if arguments[0] == "qemu-system-riscv64":
                return "QEMU emulator version 10.2.1\n"
            if arguments[1] == "info":
                return '{"format":"qcow2"}'
            return ""

        with (
            patch("qemu_vm.check_dependencies"),
            patch("qemu_vm.cached_image", return_value=(self.root / "base.img", True)),
            patch.object(self.vm, "command", side_effect=command),
            patch.object(self.vm, "make_seed"),
            patch.object(self.vm, "start", side_effect=start),
            patch.object(self.vm, "wait_ready", side_effect=KeyboardInterrupt),
            patch("qemu_vm.stop_process") as stop,
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.vm.__enter__()
        stop.assert_called_once_with(process)
        self.assertFalse(self.vm.work.exists())
        self.assertEqual(
            (self.vm.log_dir / "serial.log").read_text(), "guest boot diagnostics"
        )
        self.assertTrue(self.vm.metadata["image_cache_hit"])

    def test_cleanup_escalates_only_its_own_process_group(self) -> None:
        process = Mock(pid=12345)
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("qemu", 10), 0]
        with patch("qemu_vm.os.killpg") as kill:
            stop_process(process)
        self.assertEqual(
            [call.args for call in kill.call_args_list],
            [(12345, signal.SIGTERM), (12345, signal.SIGKILL)],
        )


if __name__ == "__main__":
    unittest.main()
