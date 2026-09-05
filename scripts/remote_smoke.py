#!/usr/bin/env python3
"""Run the exact release-candidate bytes on a native Linux/riscv64 host."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import select
import signal
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable


TEST_NAMES = (
    "installer",
    "codex-version",
    "codex-help",
    "codex-sandbox",
    "bwrap-namespaces",
    "ripgrep-pcre2",
    "app-server-help",
    "responses-proxy-help",
    "code-mode-stdio",
)


class SmokeError(RuntimeError):
    pass


def positive_finite(value: str) -> float:
    try:
        result = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive finite number") from error
    if not result > 0 or not float("inf") > result:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--candidate-dir", type=Path, required=True)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument(
        "--validation-target",
        choices=("native-k3", "qemu-system-riscv64"),
        default="native-k3",
    )
    result.add_argument("--timeout-scale", type=positive_finite, default=1.0)
    return result


def scaled_timeout(timeout: float, scale: float) -> float:
    """Scale an execution deadline without changing protocol time values."""
    if not scale > 0 or not float("inf") > scale:
        raise ValueError("timeout scale must be a positive finite number")
    return timeout * scale


def _new_session_kwargs() -> dict[str, bool]:
    return {"start_new_session": True} if os.name == "posix" else {}


def _process_group_id(process: subprocess.Popen[Any]) -> int | None:
    if os.name != "posix":
        return None
    try:
        return os.getpgid(process.pid)
    except ProcessLookupError:
        return None


def _terminate_process_group(
    process: subprocess.Popen[Any],
    *,
    group_id: int | None,
    timeout_scale: float,
) -> None:
    """Boundedly terminate a child and all descendants in its private group."""
    if group_id is not None and group_id == os.getpgrp():
        raise SmokeError("refusing to terminate the parent process group")
    if os.name == "posix" and group_id is not None:
        try:
            os.killpg(group_id, signal.SIGTERM)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=scaled_timeout(2, timeout_scale))
    except subprocess.TimeoutExpired:
        pass
    # The direct child may have exited on TERM while a descendant ignored it.
    # KILL the owned group regardless of the direct child's wait result.
    if os.name == "posix" and group_id is not None:
        try:
            os.killpg(group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=scaled_timeout(5, timeout_scale))
    except subprocess.TimeoutExpired:
        pass


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    timeout: float = 60,
    timeout_scale: float = 1.0,
) -> str:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.PIPE if input_text is not None else None,
        **_new_session_kwargs(),
    )
    group_id = _process_group_id(process)
    output = ""
    try:
        output, _ = process.communicate(
            input=input_text,
            timeout=scaled_timeout(timeout, timeout_scale),
        )
    except subprocess.TimeoutExpired as error:
        _terminate_process_group(
            process, group_id=group_id, timeout_scale=timeout_scale
        )
        output = error.output or output or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        raise SmokeError(
            f"command timed out after {scaled_timeout(timeout, timeout_scale):g}s: "
            f"{command!r}\n{output}"
        ) from error
    except BaseException:
        _terminate_process_group(
            process, group_id=group_id, timeout_scale=timeout_scale
        )
        raise
    if process.returncode != 0:
        # A failed direct child may have left descendants in the private group.
        _terminate_process_group(
            process, group_id=group_id, timeout_scale=timeout_scale
        )
    if isinstance(output, bytes):
        output = output.decode(errors="replace")
    if process.returncode != 0:
        raise SmokeError(
            f"command failed ({process.returncode}): {command!r}\n{output}"
        )
    return output


def write_frame(process: subprocess.Popen[bytes], message: dict[str, Any]) -> None:
    if process.stdin is None:
        raise SmokeError("code-mode host stdin is unavailable")
    payload = json.dumps(message, separators=(",", ":")).encode()
    process.stdin.write(struct.pack("<I", len(payload)) + payload)
    process.stdin.flush()


def read_exact(fd: int, length: int, deadline: float) -> bytes:
    value = bytearray()
    while len(value) < length:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            raise SmokeError("timed out reading a code-mode frame")
        chunk = os.read(fd, length - len(value))
        if not chunk:
            raise SmokeError("code-mode host closed stdout mid-frame")
        value.extend(chunk)
    return bytes(value)


def read_frame(
    process: subprocess.Popen[bytes],
    timeout: float = 20.0,
    timeout_scale: float = 1.0,
) -> dict[str, Any]:
    if process.stdout is None:
        raise SmokeError("code-mode host stdout is unavailable")
    deadline = time.monotonic() + scaled_timeout(timeout, timeout_scale)
    length = struct.unpack("<I", read_exact(process.stdout.fileno(), 4, deadline))[0]
    if length > 64 * 1024 * 1024:
        raise SmokeError(f"oversized code-mode frame: {length}")
    value = json.loads(read_exact(process.stdout.fileno(), length, deadline))
    if not isinstance(value, dict):
        raise SmokeError("code-mode host returned a non-object frame")
    return value


def contains_text(value: Any, expected: str) -> bool:
    if isinstance(value, str):
        return expected in value
    if isinstance(value, dict):
        return any(contains_text(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(contains_text(item, expected) for item in value)
    return False


class _BoundedStderr:
    """Continuously drain a child stderr pipe while retaining only its tail."""

    def __init__(self, stream: Any, limit: int = 64 * 1024) -> None:
        self.stream = stream
        self.limit = limit
        self.value = bytearray()
        self.thread = threading.Thread(target=self._drain, daemon=True)

    def _drain(self) -> None:
        while True:
            try:
                chunk = self.stream.read(4096)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            self.value.extend(chunk)
            if len(self.value) > self.limit:
                del self.value[: len(self.value) - self.limit]

    def start(self) -> None:
        self.thread.start()

    def join(self, timeout: float) -> None:
        self.thread.join(timeout=timeout)

    def text(self) -> str:
        return bytes(self.value).decode("utf-8", errors="replace")[-self.limit :]


def code_mode_smoke(host: Path, *, timeout_scale: float = 1.0) -> str:
    process = subprocess.Popen(
        [str(host), "--listen", "stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **_new_session_kwargs(),
    )
    group_id = _process_group_id(process)
    stderr = _BoundedStderr(process.stderr)
    stderr.start()
    messages: list[dict[str, Any]] = []
    failure: BaseException | None = None
    try:
        write_frame(
            process,
            {
                "type": "connection/hello",
                "supportedVersions": [1],
                "requiredCapabilities": [],
                "optionalCapabilities": [],
            },
        )
        ready = read_frame(process, timeout_scale=timeout_scale)
        messages.append(ready)
        if ready.get("type") != "connection/ready" or ready.get("selectedVersion") != 1:
            raise SmokeError(f"code-mode handshake failed: {ready}")

        write_frame(
            process,
            {
                "type": "operation/request",
                "id": 1,
                "request": {"method": "session/open", "sessionId": "session-1"},
            },
        )
        opened = read_frame(process, timeout_scale=timeout_scale)
        messages.append(opened)
        if (
            opened.get("type") != "operation/response"
            or opened.get("id") != 1
            or opened.get("result", {}).get("status") != "ok"
            or opened.get("result", {}).get("value", {}).get("type") != "session/ready"
        ):
            raise SmokeError(f"code-mode session open failed: {opened}")

        write_frame(
            process,
            {
                "type": "operation/request",
                "id": 2,
                "request": {
                    "method": "session/execute",
                    "sessionId": "session-1",
                    "request": {
                        "tool_call_id": "call-1",
                        "enabled_tools": [],
                        "source": "text('riscv64-code-mode-ok');",
                        "yield_time_ms": 10_000,
                        "max_output_tokens": 1_000,
                    },
                },
            },
        )
        got_started = False
        got_initial = False
        got_closed = False
        got_marker = False
        for _ in range(8):
            message = read_frame(process, timeout_scale=timeout_scale)
            messages.append(message)
            if (
                message.get("type") == "operation/response"
                and message.get("id") == 2
                and message.get("result", {}).get("status") == "ok"
                and message.get("result", {}).get("value", {}).get("type")
                == "execution/started"
            ):
                got_started = True
            if (
                message.get("type") == "execute/initialResponse"
                and message.get("id") == 2
            ):
                if message.get("result", {}).get("status") != "ok":
                    raise SmokeError(f"code-mode execution failed: {message}")
                got_initial = True
                got_marker = contains_text(message, "riscv64-code-mode-ok")
            if message.get("type") == "cell/closed":
                got_closed = True
            if got_started and got_initial and got_closed:
                break
        if not (got_started and got_initial and got_closed and got_marker):
            raise SmokeError(f"incomplete code-mode execution: {messages}")

        write_frame(
            process,
            {
                "type": "operation/request",
                "id": 3,
                "request": {"method": "session/shutdown", "sessionId": "session-1"},
            },
        )
        closed = read_frame(process, timeout_scale=timeout_scale)
        messages.append(closed)
        if (
            closed.get("type") != "operation/response"
            or closed.get("id") != 3
            or closed.get("result", {}).get("status") != "ok"
            or closed.get("result", {}).get("value", {}).get("type") != "session/closed"
        ):
            raise SmokeError(f"code-mode shutdown failed: {closed}")
        if process.stdin is not None:
            process.stdin.close()
        if process.wait(timeout=scaled_timeout(10, timeout_scale)) != 0:
            raise SmokeError("code-mode host exited unsuccessfully")
    except BaseException as error:
        failure = error
    finally:
        if failure is not None:
            _terminate_process_group(
                process, group_id=group_id, timeout_scale=timeout_scale
            )
        stderr.join(scaled_timeout(10, timeout_scale))
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()
        if process.stderr is not None:
            process.stderr.close()
    if failure is not None:
        if isinstance(failure, KeyboardInterrupt):
            raise failure
        detail = stderr.text()
        if detail:
            raise SmokeError(f"{failure}; stderr: {detail}") from failure
        raise failure
    return f"protocol v1; {len(messages)} frames; marker observed"


def extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True)
    with tarfile.open(archive, "r:gz") as value:
        value.extractall(destination, filter="data")


def host_info(validation_target: str) -> dict[str, Any]:
    host: dict[str, Any] = {
        "system": platform.system(),
        "machine": platform.machine(),
        "kernel": platform.release(),
        "python": platform.python_version(),
        "uid": os.getuid(),
    }
    if validation_target == "qemu-system-riscv64":
        setting = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
        try:
            host["apparmor_restrict_unprivileged_userns"] = setting.read_text().strip()
        except OSError as error:
            host["apparmor_restrict_unprivileged_userns"] = f"unavailable: {error}"
    return host


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def main() -> int:
    args = parser().parse_args()
    target = "riscv64gc-unknown-linux-musl"
    tests = {name: "not-run" for name in TEST_NAMES}
    details: dict[str, str] = {}
    validation_target = args.validation_target
    host: dict[str, Any] = {}
    work: Path | None = None
    interrupted = False

    def checked(name: str, action: Callable[[], str]) -> None:
        try:
            details[name] = action().strip()[-4_000:]
            tests[name] = "pass"
        except BaseException as error:  # Keep a complete structured report.
            tests[name] = "fail"
            details[name] = str(error)[-4_000:] or type(error).__name__
            if isinstance(error, KeyboardInterrupt):
                raise

    try:
        host = host_info(validation_target)
        if host["system"] != "Linux" or host["machine"] != "riscv64":
            raise SmokeError(
                f"{validation_target} smoke requires Linux/riscv64; "
                f"got {host['system']}/{host['machine']}"
            )
        work = Path(tempfile.mkdtemp(prefix="codex-riscv64-smoke."))
        install_root = work / "standalone"
        bin_dir = work / "bin"
        candidate_dir = args.candidate_dir.resolve()
        primary = candidate_dir / f"codex-package-{target}.tar.gz"
        release_json = candidate_dir / "release.json"

        def command(command_args: list[str], **kwargs: Any) -> str:
            return run(command_args, timeout_scale=args.timeout_scale, **kwargs)

        checked(
            "installer",
            lambda: command(
                [
                    "sh",
                    str(candidate_dir / "install.sh"),
                    "--archive",
                    str(primary),
                    "--release-json",
                    str(release_json),
                    "--install-root",
                    str(install_root),
                    "--bin-dir",
                    str(bin_dir),
                ]
            ),
        )
        codex = bin_dir / "codex"
        package = install_root / "current"
        checked("codex-version", lambda: command([str(codex), "--version"]))
        checked("codex-help", lambda: command([str(codex), "--help"]))
        checked(
            "codex-sandbox",
            lambda: command(
                [str(codex), "sandbox", "--", "/bin/sh", "-c", "printf sandbox-ok"],
                cwd=work,
            ),
        )
        checked(
            "bwrap-namespaces",
            lambda: command(
                [
                    str(package / "codex-resources" / "bwrap"),
                    "--unshare-user",
                    "--unshare-pid",
                    "--unshare-net",
                    "--ro-bind",
                    "/",
                    "/",
                    "--proc",
                    "/proc",
                    "--dev",
                    "/dev",
                    "--",
                    "/bin/sh",
                    "-c",
                    'test "$$" -eq 2 && printf bwrap-ok',
                ]
            ),
        )
        checked(
            "ripgrep-pcre2",
            lambda: command(
                [str(package / "codex-path" / "rg"), "--pcre2", "(?<=foo)bar"],
                input_text="foobar\n",
            ),
        )

        def app_server() -> str:
            app_dir = work / "app-server"
            extract(
                candidate_dir / f"codex-app-server-package-{target}.tar.gz", app_dir
            )
            return command([str(app_dir / "bin" / "codex-app-server"), "--help"])

        checked("app-server-help", app_server)

        def responses_proxy() -> str:
            proxy_dir = work / "proxy"
            extract(
                candidate_dir / f"codex-responses-api-proxy-{target}.tar.gz",
                proxy_dir,
            )
            return command([str(proxy_dir / "codex-responses-api-proxy"), "--help"])

        checked("responses-proxy-help", responses_proxy)
        checked(
            "code-mode-stdio",
            lambda: code_mode_smoke(
                package / "bin" / "codex-code-mode-host",
                timeout_scale=args.timeout_scale,
            ),
        )
    except KeyboardInterrupt:
        interrupted = True
        details.setdefault("setup", "interrupted")
    except BaseException as error:
        details.setdefault("setup", str(error)[-4_000:] or type(error).__name__)
    finally:
        report = {
            "schema_version": 1,
            "validation_target": validation_target,
            "overall": "pass"
            if all(value == "pass" for value in tests.values())
            else "fail",
            "host": host,
            "tests": tests,
            "details": details,
            "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        try:
            write_report(args.output, report)
        finally:
            if work is not None:
                shutil.rmtree(work, ignore_errors=True)
    print(json.dumps(report, indent=2, sort_keys=True))
    if interrupted:
        return 130
    return 0 if report["overall"] == "pass" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, SmokeError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
