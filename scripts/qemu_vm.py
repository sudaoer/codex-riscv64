"""Disposable, unprivileged full-system RISC-V guests for candidate validation."""

from __future__ import annotations

import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import platform
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, BinaryIO

from release_lib import ReleaseError
from validation_ssh import SSHConnection


IMAGE_URL = (
    "https://cloud-images.ubuntu.com/releases/releases/noble/release-20260826/"
    "ubuntu-24.04-server-cloudimg-riscv64.img"
)
# Canonical's SHA256SUMS for the immutable release directory above.
IMAGE_SHA256 = "6d0e58dc153585213020b0ec51112ebd70bedd5d2bc563599207f819586e141f"
CPU = "rv64,v=false"
FIRMWARE_SIZE = 32 * 1024 * 1024
GUEST_USER = "codex"
GUEST_MAC = "52:54:00:12:34:56"


@dataclasses.dataclass(frozen=True)
class QemuConfig:
    cache_dir: Path
    efi_dir: Path
    cpus: int = 4
    memory_mib: int = 4096
    boot_timeout: float = 900


def remaining(deadline: float, limit: float) -> float:
    duration = min(deadline - time.monotonic(), limit)
    if duration <= 0:
        raise ReleaseError("validation exceeded its overall time limit")
    return duration


def sha256_file(path: Path, *, deadline: float | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            if deadline is not None:
                remaining(deadline, 30)
            digest.update(chunk)
    return digest.hexdigest()


def check_dependencies(config: QemuConfig) -> None:
    if platform.system() != "Linux":
        raise ReleaseError("the managed QEMU target requires a Linux host")
    if config.cpus < 1 or config.memory_mib < 512:
        raise ReleaseError("QEMU requires at least one CPU and 512 MiB RAM")
    if not math.isfinite(config.boot_timeout) or config.boot_timeout <= 0:
        raise ReleaseError("QEMU boot timeout must be positive and finite")
    required = (
        "qemu-system-riscv64",
        "qemu-img",
        "cloud-localds",
        "genisoimage",
        "ssh",
        "scp",
        "ssh-keygen",
    )
    missing = [name for name in required if shutil.which(name) is None]
    if missing:
        raise ReleaseError(
            f"missing QEMU dependencies: {', '.join(missing)}; on Ubuntu 26.04 install "
            "qemu-system-riscv qemu-utils qemu-efi-riscv64 cloud-image-utils "
            "genisoimage openssh-client"
        )
    for name in ("RISCV_VIRT_CODE.fd", "RISCV_VIRT_VARS.fd"):
        path = config.efi_dir / name
        if not path.is_file() or path.stat().st_size != FIRMWARE_SIZE:
            raise ReleaseError(
                f"expected 32 MiB RISC-V UEFI firmware: {path}; "
                "install qemu-efi-riscv64 or use --qemu-efi-dir"
            )


def cached_image(
    cache_dir: Path,
    *,
    deadline: float,
    url: str = IMAGE_URL,
    expected_sha256: str = IMAGE_SHA256,
) -> tuple[Path, bool]:
    """Atomically cache only verified bytes; serialize concurrent cold downloads."""
    cache_dir = cache_dir.resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = cache_dir / f"{expected_sha256}.qcow2"
    with (cache_dir / f"{expected_sha256}.lock").open("a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(remaining(deadline, 0.2))
        if destination.exists():
            if sha256_file(destination, deadline=deadline) == expected_sha256:
                print(f"QEMU image cache verified: {destination}", flush=True)
                return destination, True
            print(
                "QEMU image cache is corrupt; downloading verified bytes again",
                flush=True,
            )
            destination.unlink()

        descriptor, name = tempfile.mkstemp(dir=cache_dir, suffix=".part")
        partial = Path(name)
        try:
            digest = hashlib.sha256()
            downloaded = 0
            last_progress = time.monotonic()
            print(f"Downloading QEMU guest: {url}", flush=True)
            request = urllib.request.Request(
                url, headers={"User-Agent": "codex-riscv64-validation"}
            )
            with os.fdopen(descriptor, "wb") as output:
                with urllib.request.urlopen(
                    request, timeout=remaining(deadline, 30)
                ) as response:
                    while chunk := response.read(1024 * 1024):
                        remaining(deadline, 30)
                        output.write(chunk)
                        digest.update(chunk)
                        downloaded += len(chunk)
                        if time.monotonic() - last_progress >= 10:
                            print(
                                f"QEMU image: {downloaded // (1024 * 1024)} MiB",
                                flush=True,
                            )
                            last_progress = time.monotonic()
                output.flush()
                os.fsync(output.fileno())
            if digest.hexdigest() != expected_sha256:
                raise ReleaseError(
                    "downloaded QEMU image SHA-256 does not match the pinned image"
                )
            partial.replace(destination)
            return destination, False
        finally:
            partial.unlink(missing_ok=True)


def stop_process(process: subprocess.Popen[Any]) -> None:
    """Bound cleanup to this process group, including interrupted helper commands."""
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=10)


class QemuVM:
    def __init__(
        self,
        config: QemuConfig,
        *,
        workspace: Path,
        log_dir: Path,
        deadline: float,
    ) -> None:
        self.config = config
        self.work = workspace / "qemu"
        self.log_dir = log_dir
        self.deadline = deadline
        self.process: subprocess.Popen[bytes] | None = None
        self.stderr: BinaryIO | None = None
        self.connection: SSHConnection
        self.metadata: dict[str, Any] = {
            "machine": "virt",
            "cpu": CPU,
            "accelerator": "tcg",
            "cpus": config.cpus,
            "memory_mib": config.memory_mib,
            "disk_gib": 16,
            "image_url": IMAGE_URL,
            "image_sha256": IMAGE_SHA256,
            "image_verified": False,
        }

    def command(self, arguments: list[str], *, timeout: float = 60) -> str:
        print("+", subprocess.list2cmdline(arguments), flush=True)
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            output, _ = process.communicate(timeout=remaining(self.deadline, timeout))
            if process.returncode != 0:
                raise ReleaseError(
                    f"command failed ({process.returncode}): {arguments!r}\n{output[-4000:]}"
                )
            return output
        finally:
            stop_process(process)

    def make_seed(self) -> None:
        self.key = self.work / "client-key"
        host_key = self.work / "host-key"
        for path in (self.key, host_key):
            self.command(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)]
            )
        self.host_public_key = host_key.with_suffix(".pub").read_text().strip()
        # Some RISC-V UEFI images expose their build date instead of QEMU's RTC.
        # Initialize UTC inside the disposable guest without depending on NTP.
        # Guest uptime accounts for slow kernel/cloud-init startup after seeding.
        clock_seed = time.time()
        self.metadata["clock_seed_epoch"] = clock_seed
        self.metadata["clock_source"] = "host-utc-plus-guest-uptime"
        user_data = {
            "users": [
                {
                    "name": GUEST_USER,
                    "shell": "/bin/bash",
                    "lock_passwd": True,
                    "ssh_authorized_keys": [
                        self.key.with_suffix(".pub").read_text().strip()
                    ],
                }
            ],
            "ssh_pwauth": False,
            "disable_root": True,
            "ssh_deletekeys": True,
            "ssh_keys": {
                "ed25519_private": host_key.read_text(),
                "ed25519_public": self.host_public_key,
            },
            "package_update": False,
            "package_upgrade": False,
            "runcmd": [
                [
                    "sh",
                    "-c",
                    "if [ -e /proc/sys/kernel/apparmor_restrict_unprivileged_userns ]; then "
                    "sysctl -w kernel.apparmor_restrict_unprivileged_userns=0; fi",
                ],
                ["systemctl", "enable", "--now", "ssh"],
                [
                    "python3",
                    "-c",
                    "import time; time.clock_settime(time.CLOCK_REALTIME, "
                    f"{clock_seed!r} + time.clock_gettime(time.CLOCK_BOOTTIME))",
                ],
            ],
        }
        documents = {
            "user-data": "#cloud-config\n" + json.dumps(user_data),
            "meta-data": json.dumps(
                {"instance-id": f"codex-{uuid.uuid4()}", "local-hostname": "codex-qemu"}
            ),
            "network-config": json.dumps(
                {
                    "version": 2,
                    "ethernets": {
                        "eth0": {
                            "match": {"macaddress": GUEST_MAC},
                            "set-name": "eth0",
                            "dhcp4": True,
                            "dhcp6": False,
                        }
                    },
                }
            ),
        }
        for name, document in documents.items():
            path = self.work / name
            path.write_text(document + "\n")
            path.chmod(0o600)
        self.command(
            [
                "cloud-localds",
                "--network-config",
                str(self.work / "network-config"),
                str(self.work / "seed.img"),
                str(self.work / "user-data"),
                str(self.work / "meta-data"),
            ]
        )

    def launch_arguments(self, port: int) -> list[str]:
        # JSON blockdev arguments avoid QEMU's comma-delimited filename grammar.
        rtc_base = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        self.metadata["rtc_base_utc"] = rtc_base + "Z"
        block_devices = [
            {
                "driver": "file",
                "node-name": "efi-code",
                "filename": str((self.config.efi_dir / "RISCV_VIRT_CODE.fd").resolve()),
                "read-only": True,
            },
            {
                "driver": "file",
                "node-name": "efi-vars",
                "filename": str(self.work / "vars.fd"),
            },
            {
                "driver": "qcow2",
                "node-name": "root",
                "file": {"driver": "file", "filename": str(self.work / "root.qcow2")},
            },
            {
                "driver": "raw",
                "node-name": "seed",
                "read-only": True,
                "file": {"driver": "file", "filename": str(self.work / "seed.img")},
            },
        ]
        command = [
            "qemu-system-riscv64",
            "-machine",
            "virt,pflash0=efi-code,pflash1=efi-vars",
            "-accel",
            "tcg,thread=multi",
            "-cpu",
            CPU,
            "-rtc",
            f"base={rtc_base},clock=host",
            "-smp",
            str(self.config.cpus),
            "-m",
            str(self.config.memory_mib),
            "-display",
            "none",
            "-monitor",
            "none",
            "-serial",
            f"file:{self.log_dir / 'serial.log'}",
            "-no-reboot",
        ]
        for device in block_devices:
            command.extend(["-blockdev", json.dumps(device)])
        command.extend(
            [
                "-device",
                "virtio-blk-device,drive=root",
                "-device",
                "virtio-blk-device,drive=seed",
                "-netdev",
                f"user,id=net0,restrict=on,hostfwd=tcp:127.0.0.1:{port}-:22",
                "-device",
                f"virtio-net-device,netdev=net0,mac={GUEST_MAC}",
                "-device",
                "virtio-rng-device",
            ]
        )
        return command

    def start(self) -> None:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        known_hosts = self.work / "known_hosts"
        known_hosts.write_text(f"[127.0.0.1]:{port} {self.host_public_key}\n")
        self.connection = SSHConnection(
            f"{GUEST_USER}@127.0.0.1",
            port=port,
            identity_file=self.key,
            known_hosts=known_hosts,
        )
        arguments = self.launch_arguments(port)
        (self.log_dir / "qemu-command.json").write_text(
            json.dumps(arguments, indent=2) + "\n"
        )
        self.stderr = (self.log_dir / "qemu.log").open("wb")
        self.process = subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=self.stderr,
            start_new_session=True,
        )
        print(f"QEMU started; waiting for guest SSH on 127.0.0.1:{port}", flush=True)

    def wait_ready(self) -> None:
        boot_deadline = min(self.deadline, time.monotonic() + self.config.boot_timeout)
        last_error = "SSH has not responded"
        last_progress = time.monotonic()
        while time.monotonic() < boot_deadline:
            if self.process is None or self.process.poll() is not None:
                raise ReleaseError(
                    f"QEMU exited during boot; inspect {self.log_dir / 'qemu.log'}"
                )
            try:
                result = self.connection.run(
                    ["cloud-init", "status", "--format", "json"],
                    capture=True,
                    check=False,
                    timeout=remaining(boot_deadline, 15),
                )
                last_error = (result.stderr or result.stdout or "guest not ready")[
                    -1000:
                ]
                if result.returncode in (0, 1, 2):
                    status = json.loads(result.stdout)
                    state = status.get("status")
                    if state == "error" or (state == "done" and result.returncode != 0):
                        raise ReleaseError(
                            f"guest cloud-init failed: {result.stdout[-4000:]}"
                        )
                    if state == "done":
                        self.check_guest(boot_deadline)
                        print(
                            "QEMU guest ready: Linux/riscv64, ordinary user, cloud-init complete",
                            flush=True,
                        )
                        return
            except (subprocess.TimeoutExpired, json.JSONDecodeError) as error:
                last_error = str(error)[-1000:]
            if time.monotonic() - last_progress >= 30:
                print(
                    "Waiting for QEMU guest; serial output is in "
                    + str(self.log_dir / "serial.log"),
                    flush=True,
                )
                last_progress = time.monotonic()
            time.sleep(max(0, min(2, boot_deadline - time.monotonic())))
        raise ReleaseError(
            f"QEMU guest did not become ready within {self.config.boot_timeout:g}s: {last_error}"
        )

    def check_guest(self, deadline: float) -> None:
        code = (
            "import os,platform,sys,time; from pathlib import Path; "
            "assert platform.system() == 'Linux' and platform.machine() == 'riscv64', 'wrong guest architecture'; "
            "assert sys.version_info >= (3,12), 'guest needs Python 3.12+'; "
            "assert os.getuid() != 0, 'smoke tests must run as an ordinary user'; "
            "p=Path('/proc/sys/kernel/apparmor_restrict_unprivileged_userns'); "
            "assert not p.exists() or p.read_text().strip() == '0', 'guest userns restriction remains enabled'; "
            "print(time.time())"
        )
        result = self.connection.run(
            ["python3", "-c", code],
            capture=True,
            check=False,
            timeout=remaining(deadline, 30),
        )
        if result.returncode != 0:
            raise ReleaseError(
                f"QEMU guest prerequisites failed: {result.stdout}\n{result.stderr}"
            )
        skew = float(result.stdout.strip()) - time.time()
        self.metadata["guest_clock_skew_seconds"] = round(skew, 3)
        if not math.isfinite(skew) or abs(skew) > 300:
            raise ReleaseError(f"QEMU guest clock differs from host UTC by {skew:g}s")

    def __enter__(self) -> QemuVM:
        try:
            check_dependencies(self.config)
            self.work.mkdir(mode=0o700, parents=True)
            self.log_dir.mkdir(parents=True, exist_ok=True)
            version = self.command(["qemu-system-riscv64", "--version"]).splitlines()[0]
            self.metadata["version"] = version
            firmware_code = self.config.efi_dir / "RISCV_VIRT_CODE.fd"
            firmware_vars = self.config.efi_dir / "RISCV_VIRT_VARS.fd"
            self.metadata.update(
                {
                    "firmware_code_sha256": sha256_file(
                        firmware_code, deadline=self.deadline
                    ),
                    "firmware_vars_sha256": sha256_file(
                        firmware_vars, deadline=self.deadline
                    ),
                }
            )
            base, hit = cached_image(self.config.cache_dir, deadline=self.deadline)
            self.metadata.update({"image_cache_hit": hit, "image_verified": True})
            info = json.loads(
                self.command(["qemu-img", "info", "--output=json", str(base)])
            )
            if info.get("format") != "qcow2" or info.get("backing-filename"):
                raise ReleaseError(
                    "pinned guest image must be a standalone qcow2 image"
                )
            self.command(
                [
                    "qemu-img",
                    "create",
                    "-f",
                    "qcow2",
                    "-F",
                    "qcow2",
                    "-b",
                    str(base),
                    str(self.work / "root.qcow2"),
                ]
            )
            self.command(["qemu-img", "resize", str(self.work / "root.qcow2"), "16G"])
            shutil.copyfile(firmware_vars, self.work / "vars.fd")
            self.make_seed()
            self.start()
            self.wait_ready()
            return self
        except BaseException as error:
            try:
                self.close()
            except Exception as cleanup_error:
                error.add_note(f"QEMU cleanup also failed: {cleanup_error}")
            raise

    def close(self) -> None:
        try:
            if self.process is not None:
                stop_process(self.process)
        finally:
            if self.stderr is not None:
                self.stderr.close()
            if self.work.exists():
                shutil.rmtree(self.work)

    def __exit__(
        self, exc_type: Any, error: BaseException | None, traceback: Any
    ) -> None:
        try:
            self.close()
        except Exception as cleanup_error:
            if error is None:
                raise
            error.add_note(f"QEMU cleanup also failed: {cleanup_error}")
