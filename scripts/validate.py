#!/usr/bin/env python3
"""Validate a release candidate on the native K3 host or inside QEMU."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from release_lib import (
    REQUIRED_K3_TESTS,
    ReleaseError,
    fetch_candidate_policy,
    load_manifest,
    load_policy,
    preflight_publish,
    read_json_object,
    validate_candidate,
    validate_candidate_run,
    write_json,
)
from validation_ssh import SSHConnection


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "release" / "policy.toml"
OVERALL_DEADLINE = 7200.0


def positive_finite(value: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return number


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    source = result.add_mutually_exclusive_group(required=True)
    source.add_argument("--run-id")
    source.add_argument("--candidate-dir", type=Path, help="本地构建的候选目录")
    result.add_argument("--target", choices=("k3", "qemu"), default="k3")
    result.add_argument("--ssh-host", default="k3")
    result.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    result.add_argument(
        "--candidate-policy",
        action="store_true",
        help="validate using policy and patches from the candidate source commit",
    )
    result.add_argument("--output", type=Path)
    result.add_argument("--skip-attestation", action="store_true")
    result.add_argument("--request-publish", action="store_true")
    result.add_argument("--qemu-cache-dir", type=Path, default=ROOT / ".work" / "qemu")
    result.add_argument(
        "--qemu-efi-dir", type=Path, default=Path("/usr/share/qemu-efi-riscv64")
    )
    result.add_argument("--qemu-cpus", type=positive_int, default=4)
    result.add_argument("--qemu-memory-mib", type=positive_int, default=4096)
    result.add_argument("--qemu-boot-timeout", type=positive_finite, default=900.0)
    result.add_argument("--timeout-scale", type=positive_finite)
    return result


def run(
    command: list[str],
    *,
    capture: bool = False,
    check: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    print("+", subprocess.list2cmdline(command), flush=True)
    return subprocess.run(
        command,
        check=check,
        text=True,
        capture_output=capture,
        timeout=timeout,
    )


def github_run(
    repository: str, run_id: str, *, timeout: float | None = None
) -> dict[str, Any]:
    result = run(
        ["gh", "api", f"repos/{repository}/actions/runs/{run_id}"],
        capture=True,
        timeout=timeout,
    )
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ReleaseError("GitHub run response is not an object")
    return value


def checked_remote_directory(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"/tmp/codex-riscv64-k3\.[A-Za-z0-9]+", value):
        return value
    raise ReleaseError(f"unsafe remote temporary directory: {value!r}")


def target_name(target: str) -> str:
    return "native-k3" if target == "k3" else "qemu-system-riscv64"


class SmokeReportError(ReleaseError):
    """A smoke failure with the guest's structured report attached."""

    def __init__(self, message: str, report: dict[str, Any]) -> None:
        super().__init__(message)
        self.report = report


def _remaining(deadline: float, requested: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ReleaseError("validation overall deadline exceeded")
    return min(requested, remaining)


def _failure_report(
    target: str,
    error: BaseException,
    qemu_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    report = {
        "schema_version": 1,
        "validation_target": target_name(target),
        "overall": "fail",
        "tests": {name: "not-run" for name in REQUIRED_K3_TESTS},
        "details": {"validator": str(error)[-4_000:]},
        "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    if qemu_metadata:
        report["qemu"] = qemu_metadata
    return report


def _mark_report_failed(path: Path, error: BaseException) -> None:
    """Retain a saved report's evidence while making any gate failure fatal."""
    report = read_json_object(path)
    report["overall"] = "fail"
    details = report.get("details")
    if not isinstance(details, dict):
        details = {}
    details["validator"] = str(error)[-4_000:]
    report["details"] = details
    write_json(path, report)


def _assert_guest_report(target: str, raw_report: dict[str, Any]) -> None:
    expected_target = target_name(target)
    if raw_report.get("validation_target") != expected_target:
        raise ReleaseError(
            f"smoke report target {raw_report.get('validation_target')!r} does not match {expected_target}"
        )
    host = raw_report.get("host")
    if (
        not isinstance(host, dict)
        or host.get("system") != "Linux"
        or host.get("machine") != "riscv64"
    ):
        raise ReleaseError("validation guest is not Linux/riscv64")
    if raw_report.get("overall") != "pass":
        raise ReleaseError(
            f"{target_name(target)} smoke tests failed; inspect the saved report"
        )
    tests = raw_report.get("tests")
    if not isinstance(tests, dict):
        raise ReleaseError("smoke report has no test map")
    missing = [name for name in REQUIRED_K3_TESTS if tests.get(name) != "pass"]
    if missing:
        raise ReleaseError(f"required smoke tests did not pass: {missing}")


def _qemu_session(args: argparse.Namespace, work: Path, logs: Path, deadline: float):
    try:
        from qemu_vm import QemuConfig, QemuVM, check_dependencies
    except ImportError as error:
        raise ReleaseError(f"QEMU backend is unavailable: {error}") from error
    config = QemuConfig(
        cache_dir=args.qemu_cache_dir,
        efi_dir=args.qemu_efi_dir,
        cpus=args.qemu_cpus,
        memory_mib=args.qemu_memory_mib,
        boot_timeout=args.qemu_boot_timeout,
    )
    check_dependencies(config)
    return QemuVM(config, workspace=work, log_dir=logs, deadline=deadline)


def _run_smoke(
    connection: SSHConnection,
    candidate: dict[str, Any],
    candidate_dir: Path,
    temporary: Path,
    logs: Path,
    *,
    target: str,
    timeout_scale: float,
    deadline: float,
) -> dict[str, Any]:
    remote = connection.run(
        ["mktemp", "-d", "/tmp/codex-riscv64-k3.XXXXXXXX"],
        capture=True,
        timeout=_remaining(deadline, 60),
    )
    remote_dir = checked_remote_directory(remote.stdout)
    remote_report = f"{remote_dir}/raw-report.json"
    local_raw_report = temporary / "raw-report.json"
    try:
        connection.run(
            ["mkdir", f"{remote_dir}/candidate"], timeout=_remaining(deadline, 60)
        )
        paths = [candidate_dir / name for name in candidate["assets"]]
        paths.append(candidate_dir / "candidate.json")
        connection.copy_to(
            paths, f"{remote_dir}/candidate/", timeout=_remaining(deadline, 300)
        )
        connection.copy_to(
            [ROOT / "scripts" / "remote_smoke.py"],
            f"{remote_dir}/remote_smoke.py",
            timeout=_remaining(deadline, 60),
        )
        smoke_error: BaseException | None = None
        smoke_output = ""
        try:
            smoke = connection.run(
                [
                    "python3",
                    f"{remote_dir}/remote_smoke.py",
                    "--candidate-dir",
                    f"{remote_dir}/candidate",
                    "--output",
                    remote_report,
                    "--validation-target",
                    target_name(target),
                    "--timeout-scale",
                    str(timeout_scale),
                ],
                check=False,
                capture=True,
                timeout=_remaining(deadline, 3600 * timeout_scale),
            )
            smoke_output = (smoke.stdout or "") + (smoke.stderr or "")
            if smoke.returncode != 0:
                smoke_error = ReleaseError(
                    f"smoke exited with status {smoke.returncode}"
                )
        except (
            BaseException
        ) as error:  # Preserve the raw report after timeout/interrupt.
            smoke_error = error
            output = getattr(error, "stdout", None) or getattr(error, "output", None)
            stderr = getattr(error, "stderr", None)
            smoke_output = "\n".join(
                value for value in (output, stderr) if isinstance(value, str)
            )
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "remote-smoke.log").write_text(
            smoke_output[-100_000:], encoding="utf-8"
        )
        raw_error: BaseException | None = None
        try:
            connection.copy_from(
                remote_report, local_raw_report, timeout=_remaining(deadline, 120)
            )
        except BaseException as error:
            raw_error = error
        if raw_error is not None:
            raise ReleaseError(
                f"cannot retrieve smoke report: {raw_error}"
            ) from raw_error
        raw_report = read_json_object(local_raw_report)
        if smoke_error is not None:
            raise SmokeReportError(str(smoke_error), raw_report) from smoke_error
        try:
            _assert_guest_report(target, raw_report)
        except ReleaseError as error:
            raise SmokeReportError(str(error), raw_report) from error
        return raw_report
    finally:
        try:
            connection.run(
                ["rm", "-rf", "--", remote_dir],
                check=False,
                timeout=_remaining(deadline, 60),
            )
        except BaseException:
            pass


def _download_candidate(
    args: argparse.Namespace, repository: str, temporary: Path, deadline: float
) -> tuple[Path, Path, dict[str, Any]]:
    token = run(
        ["gh", "auth", "token"], capture=True, timeout=_remaining(deadline, 60)
    ).stdout.strip()
    if not token:
        raise ReleaseError("gh did not return an authentication token")
    run_info = github_run(repository, args.run_id, timeout=_remaining(deadline, 60))
    validate_candidate_run(run_info, expected_run_id=args.run_id)
    candidate_dir = temporary / "candidate"
    candidate_dir.mkdir()
    run(
        [
            "gh",
            "run",
            "download",
            args.run_id,
            "--repo",
            repository,
            "--name",
            "codex-riscv64-candidate",
            "--dir",
            str(candidate_dir),
        ],
        timeout=_remaining(deadline, 600),
    )
    candidate_policy = args.policy
    if args.candidate_policy:
        metadata = read_json_object(candidate_dir / "candidate.json")
        source_sha = metadata.get("candidate_head_sha")
        if not isinstance(source_sha, str):
            raise ReleaseError("candidate source SHA is invalid")
        candidate_policy = fetch_candidate_policy(
            repository, source_sha, temporary / "locked-source", token=token
        )
    return candidate_dir, candidate_policy, run_info


def _check_local_report_paths(output: Path, logs: Path, candidate_dir: Path) -> None:
    if any(path.resolve().is_relative_to(candidate_dir) for path in (output, logs)):
        raise ReleaseError("验证报告和日志必须写入本地候选目录之外")


def _validate(args: argparse.Namespace) -> Path:
    deadline = time.monotonic() + OVERALL_DEADLINE
    timeout_scale = (
        args.timeout_scale
        if args.timeout_scale is not None
        else (1.0 if args.target == "k3" else 10.0)
    )
    output = args.output or ROOT / "analysis" / f"{args.target}-report-unknown.json"
    logs = output.with_name(f"{output.stem}-logs")
    local_dir = args.candidate_dir.resolve() if args.candidate_dir is not None else None
    if local_dir is not None:
        if args.candidate_policy or args.skip_attestation or args.request_publish:
            raise ReleaseError(
                "--candidate-policy、--skip-attestation 和 --request-publish 仅适用于 --run-id"
            )
        _check_local_report_paths(output, logs, local_dir)
        if args.target == "qemu" and args.qemu_cache_dir.resolve().is_relative_to(
            local_dir
        ):
            raise ReleaseError("QEMU 缓存必须位于本地候选目录之外")
    temporary = Path(tempfile.mkdtemp(prefix="codex-riscv64-validation."))
    report_written = False
    binding: dict[str, Any] = {}
    manifest = None
    qemu_metadata: dict[str, Any] | None = None
    qemu: Any | None = None
    connection: SSHConnection | None = None
    try:
        repository = load_policy(args.policy).distribution.repository
        if args.run_id is not None and not args.run_id.isdigit():
            raise ReleaseError("--run-id must be numeric")
        if args.target == "k3":
            connection = SSHConnection(args.ssh_host)
        if local_dir is None:
            candidate_dir, candidate_policy, run_info = _download_candidate(
                args, repository, temporary, deadline
            )
        else:
            candidate_dir, candidate_policy, run_info = local_dir, args.policy, None
        manifest = load_manifest(candidate_policy, candidate_dir / "release-lock.json")
        report_output = (
            args.output
            or ROOT / "analysis" / f"{args.target}-report-{manifest.release_tag}.json"
        )
        report_logs = report_output.with_name(f"{report_output.stem}-logs")
        if local_dir is not None:
            _check_local_report_paths(report_output, report_logs, local_dir)
        output, logs = report_output, report_logs
        origin = "local" if local_dir is not None else "github-actions"
        candidate = validate_candidate(manifest, candidate_dir, expected_origin=origin)
        binding = {
            "candidate_head_sha": candidate["candidate_head_sha"],
            "release_tag": candidate["release_tag"],
            "assets": candidate["assets"],
        }
        smoke_dir = candidate_dir
        if local_dir is not None:
            binding.update(build_origin="local", candidate_id=candidate["candidate_id"])
            smoke_dir = temporary / "candidate"
            smoke_dir.mkdir()
            for name in (*candidate["assets"], "candidate.json"):
                shutil.copyfile(
                    candidate_dir / name, smoke_dir / name, follow_symlinks=False
                )
            snapshot = validate_candidate(manifest, smoke_dir, expected_origin="local")
            if snapshot != candidate:
                raise ReleaseError("复制验证输入时本地候选发生变化")
        else:
            assert run_info is not None
            binding.update(
                candidate_run_id=args.run_id, candidate_run_url=run_info.get("html_url")
            )
            validate_candidate_run(
                run_info,
                expected_run_id=args.run_id,
                candidate_head_sha=candidate["candidate_head_sha"],
                candidate_workflow_head_sha=candidate.get(
                    "candidate_workflow_head_sha", candidate["candidate_head_sha"]
                ),
            )
            if not args.skip_attestation:
                primary = (
                    candidate_dir
                    / f"codex-package-{manifest.distribution.target}.tar.gz"
                )
                run(
                    ["gh", "attestation", "verify", str(primary), "--repo", repository],
                    timeout=_remaining(deadline, 600),
                )

        if args.target == "qemu":
            qemu = _qemu_session(args, temporary, logs, deadline)
            qemu_metadata = dict(qemu.metadata)
            with qemu as vm:
                connection = vm.connection
                raw_report = _run_smoke(
                    connection,
                    candidate,
                    smoke_dir,
                    temporary,
                    logs,
                    target=args.target,
                    timeout_scale=timeout_scale,
                    deadline=deadline,
                )
                qemu_metadata = dict(vm.metadata)
        else:
            assert connection is not None
            raw_report = _run_smoke(
                connection,
                candidate,
                smoke_dir,
                temporary,
                logs,
                target=args.target,
                timeout_scale=timeout_scale,
                deadline=deadline,
            )
            qemu_metadata = None

        report = {
            **raw_report,
            "validation_target": target_name(args.target),
            **({"qemu": qemu_metadata} if qemu_metadata is not None else {}),
            **binding,
            "validated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        write_json(output, report)
        report_written = True
        preflight_publish(manifest, candidate_dir, output, expected_run_id=args.run_id)
        print(f"{target_name(args.target)} report: {output}")
        if args.request_publish:
            encoded = base64.b64encode(output.read_bytes()).decode("ascii")
            run(
                [
                    "gh",
                    "workflow",
                    "run",
                    "publish.yml",
                    "--repo",
                    repository,
                    "--ref",
                    "main",
                    "-f",
                    f"candidate_run_id={args.run_id}",
                    "-f",
                    f"k3_report_b64={encoded}",
                ],
                timeout=_remaining(deadline, 120),
            )
            print(
                "Publish workflow requested; candidate and evidence will be reverified before publication."
            )
        return output
    except BaseException as error:
        if qemu is not None:
            qemu_metadata = dict(getattr(qemu, "metadata", {}) or {}) or qemu_metadata
        logs.mkdir(parents=True, exist_ok=True)
        if report_written:
            try:
                _mark_report_failed(output, error)
            except (OSError, ReleaseError, json.JSONDecodeError):
                pass
        else:
            failure = _failure_report(args.target, error, qemu_metadata)
            raw_failure = getattr(error, "report", None)
            if isinstance(raw_failure, dict):
                failure.update(raw_failure)
                failure["overall"] = "fail"
                details = dict(raw_failure.get("details", {}))
                details["validator"] = str(error)[-4_000:]
                failure["details"] = details
            failure.update(binding)
            if manifest is not None:
                failure["release_tag"] = manifest.release_tag
            try:
                write_json(output, failure)
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def main() -> int:
    args = parser().parse_args()
    try:
        _validate(args)
    except (
        OSError,
        ReleaseError,
        ValueError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
        KeyboardInterrupt,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
