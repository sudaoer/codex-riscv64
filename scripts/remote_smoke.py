#!/usr/bin/env python3
"""Run the exact release-candidate bytes on a native Linux/riscv64 host."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import select
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
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


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--candidate-dir", type=Path, required=True)
    result.add_argument("--output", type=Path, required=True)
    return result


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    timeout: int = 60,
) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise SmokeError(
            f"command failed ({completed.returncode}): {command!r}\n{completed.stdout}"
        )
    return completed.stdout


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


def read_frame(process: subprocess.Popen[bytes], timeout: float = 20.0) -> dict[str, Any]:
    if process.stdout is None:
        raise SmokeError("code-mode host stdout is unavailable")
    deadline = time.monotonic() + timeout
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


def code_mode_smoke(host: Path) -> str:
    process = subprocess.Popen(
        [str(host), "--listen", "stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    messages: list[dict[str, Any]] = []
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
        ready = read_frame(process)
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
        opened = read_frame(process)
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
            message = read_frame(process)
            messages.append(message)
            if (
                message.get("type") == "operation/response"
                and message.get("id") == 2
                and message.get("result", {}).get("status") == "ok"
                and message.get("result", {}).get("value", {}).get("type")
                == "execution/started"
            ):
                got_started = True
            if message.get("type") == "execute/initialResponse" and message.get("id") == 2:
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
        closed = read_frame(process)
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
        if process.wait(timeout=10) != 0:
            raise SmokeError("code-mode host exited unsuccessfully")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    return f"protocol v1; {len(messages)} frames; marker observed"


def extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True)
    with tarfile.open(archive, "r:gz") as value:
        value.extractall(destination, filter="data")


def main() -> int:
    args = parser().parse_args()
    candidate_dir = args.candidate_dir.resolve()
    target = "riscv64gc-unknown-linux-musl"
    if platform.system() != "Linux" or platform.machine() != "riscv64":
        raise SmokeError("native smoke requires Linux/riscv64")

    tests = {name: "not-run" for name in TEST_NAMES}
    details: dict[str, str] = {}
    work = Path(tempfile.mkdtemp(prefix="codex-riscv64-smoke."))
    try:
        install_root = work / "standalone"
        bin_dir = work / "bin"
        primary = candidate_dir / f"codex-package-{target}.tar.gz"
        release_json = candidate_dir / "release.json"

        def checked(name: str, action: Callable[[], str]) -> None:
            try:
                details[name] = action().strip()[-4_000:]
                tests[name] = "pass"
            except Exception as error:  # Keep a complete structured report.
                tests[name] = "fail"
                details[name] = str(error)[-4_000:]

        checked(
            "installer",
            lambda: run(
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
        checked("codex-version", lambda: run([str(codex), "--version"]))
        checked("codex-help", lambda: run([str(codex), "--help"]))
        checked(
            "codex-sandbox",
            lambda: run(
                [str(codex), "sandbox", "--", "/bin/sh", "-c", "printf sandbox-ok"],
                cwd=work,
            ),
        )
        checked(
            "bwrap-namespaces",
            lambda: run(
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
                    "test \"$$\" -eq 2 && printf bwrap-ok",
                ]
            ),
        )
        checked(
            "ripgrep-pcre2",
            lambda: run(
                [str(package / "codex-path" / "rg"), "--pcre2", "(?<=foo)bar"],
                input_text="foobar\n",
            ),
        )

        app_dir = work / "app-server"
        extract(candidate_dir / f"codex-app-server-package-{target}.tar.gz", app_dir)
        checked(
            "app-server-help",
            lambda: run([str(app_dir / "bin" / "codex-app-server"), "--help"]),
        )
        proxy_dir = work / "proxy"
        extract(candidate_dir / f"codex-responses-api-proxy-{target}.tar.gz", proxy_dir)
        checked(
            "responses-proxy-help",
            lambda: run([str(proxy_dir / "codex-responses-api-proxy"), "--help"]),
        )
        checked(
            "code-mode-stdio",
            lambda: code_mode_smoke(package / "bin" / "codex-code-mode-host"),
        )
    finally:
        shutil.rmtree(work)

    report = {
        "schema_version": 1,
        "overall": "pass" if all(value == "pass" for value in tests.values()) else "fail",
        "host": {
            "system": platform.system(),
            "machine": platform.machine(),
            "kernel": platform.release(),
            "python": platform.python_version(),
        },
        "tests": tests,
        "details": details,
        "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["overall"] == "pass" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, SmokeError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
