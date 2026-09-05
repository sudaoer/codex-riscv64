"""Small, bounded SSH/SCP transport used by release validation."""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path
from typing import Sequence


class SSHConnection:
    """Run commands and copy files through one explicitly configured SSH host."""

    def __init__(
        self,
        host: str,
        port: int | None = None,
        identity_file: Path | None = None,
        known_hosts: Path | None = None,
    ) -> None:
        host_part = host.rsplit("@", 1)[-1] if "@" in host else host
        bracketed_ipv6 = host_part.startswith("[") and host_part.endswith("]")
        if (
            not host
            or host.startswith("-")
            or "/" in host
            or any(char.isspace() or ord(char) < 32 for char in host)
            or not host_part
            or host.count("@") > 1
            or (":" in host_part and not bracketed_ipv6)
            or (
                bracketed_ipv6
                and re.fullmatch(r"\[[0-9A-Fa-f:.]+\]", host_part) is None
            )
        ):
            raise ValueError("SSH host must be a non-empty host name, not an option")
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("SSH port must be between 1 and 65535")
        self.host = host
        self.port = port
        self.identity_file = identity_file
        self.known_hosts = known_hosts

    def _common(self, *, timeout: float) -> list[str]:
        connect_timeout = max(1, min(30, int(timeout)))
        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={connect_timeout}",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
        ]
        if self.known_hosts is not None:
            command.extend(
                [
                    "-o",
                    f"UserKnownHostsFile={self.known_hosts}",
                    "-o",
                    "StrictHostKeyChecking=accept-new",
                ]
            )
        if self.identity_file is not None:
            command.extend(["-o", "IdentitiesOnly=yes", "-i", str(self.identity_file)])
        if self.port is not None:
            command.extend(["-p", str(self.port)])
        return command

    def run(
        self,
        arguments: Sequence[str],
        timeout: float = 60,
        check: bool = True,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        if timeout <= 0:
            raise ValueError("SSH command timeout must be positive")
        command = [
            *self._common(timeout=timeout),
            self.host,
            shlex.join(list(arguments)),
        ]
        print("+", shlex.join(command), flush=True)
        return subprocess.run(
            command,
            check=check,
            text=True,
            capture_output=capture,
            timeout=timeout,
        )

    def copy_to(
        self,
        paths: Sequence[Path],
        remote_path: str,
        timeout: float = 300,
    ) -> subprocess.CompletedProcess[str]:
        if timeout <= 0:
            raise ValueError("SCP timeout must be positive")
        command = [
            "scp",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={max(1, min(30, int(timeout)))}",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
        ]
        if self.known_hosts is not None:
            command.extend(
                [
                    "-o",
                    f"UserKnownHostsFile={self.known_hosts}",
                    "-o",
                    "StrictHostKeyChecking=accept-new",
                ]
            )
        if self.identity_file is not None:
            command.extend(["-o", "IdentitiesOnly=yes", "-i", str(self.identity_file)])
        if self.port is not None:
            command.extend(["-P", str(self.port)])
        command.extend([*(str(path) for path in paths), f"{self.host}:{remote_path}"])
        print("+", shlex.join(command), flush=True)
        return subprocess.run(command, check=True, text=True, timeout=timeout)

    def copy_from(
        self,
        remote_path: str,
        local_path: Path,
        timeout: float = 60,
    ) -> subprocess.CompletedProcess[str]:
        if timeout <= 0:
            raise ValueError("SCP timeout must be positive")
        command = [
            "scp",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={max(1, min(30, int(timeout)))}",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
        ]
        if self.known_hosts is not None:
            command.extend(
                [
                    "-o",
                    f"UserKnownHostsFile={self.known_hosts}",
                    "-o",
                    "StrictHostKeyChecking=accept-new",
                ]
            )
        if self.identity_file is not None:
            command.extend(["-o", "IdentitiesOnly=yes", "-i", str(self.identity_file)])
        if self.port is not None:
            command.extend(["-P", str(self.port)])
        command.extend([f"{self.host}:{remote_path}", str(local_path)])
        print("+", shlex.join(command), flush=True)
        return subprocess.run(command, check=True, text=True, timeout=timeout)
