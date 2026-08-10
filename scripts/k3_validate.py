#!/usr/bin/env python3
"""Download one candidate, validate it on K3, and optionally request publish."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from release_lib import (
    ReleaseError,
    load_manifest,
    load_policy,
    preflight_publish,
    read_json_object,
    validate_candidate,
    validate_candidate_run,
    verify_latest_manifest,
    write_json,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "release" / "policy.toml"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--run-id", required=True)
    result.add_argument("--ssh-host", default="k3")
    result.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    result.add_argument("--output", type=Path)
    result.add_argument("--skip-attestation", action="store_true")
    result.add_argument("--request-publish", action="store_true")
    return result


def run(
    command: list[str],
    *,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    print("+", subprocess.list2cmdline(command), flush=True)
    return subprocess.run(
        command,
        check=check,
        text=True,
        capture_output=capture,
    )


def github_run(repository: str, run_id: str) -> dict[str, Any]:
    result = run(
        ["gh", "api", f"repos/{repository}/actions/runs/{run_id}"], capture=True
    )
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ReleaseError("GitHub run response is not an object")
    return value


def checked_remote_directory(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"/tmp/codex-riscv64-k3\.[A-Za-z0-9]+", value) is None:
        raise ReleaseError(f"unsafe remote temporary directory: {value!r}")
    return value


def main() -> int:
    args = parser().parse_args()
    if not args.run_id.isdigit():
        raise ReleaseError("--run-id must be numeric")
    policy_document = load_policy(args.policy)
    repository = policy_document.distribution.repository
    github_token = run(["gh", "auth", "token"], capture=True).stdout.strip()
    if not github_token:
        raise ReleaseError("gh did not return an authentication token")
    run_info = github_run(repository, args.run_id)
    validate_candidate_run(run_info, expected_run_id=args.run_id)

    temporary = Path(tempfile.mkdtemp(prefix="codex-riscv64-k3-local."))
    remote_dir: str | None = None
    try:
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
            ]
        )
        manifest = load_manifest(
            args.policy, candidate_dir / "release-lock.json"
        )
        verify_latest_manifest(manifest, token=github_token)
        candidate = validate_candidate(manifest, candidate_dir)
        validate_candidate_run(
            run_info,
            expected_run_id=args.run_id,
            candidate_head_sha=candidate["candidate_head_sha"],
        )
        if not args.skip_attestation:
            primary = candidate_dir / (
                f"codex-package-{manifest.distribution.target}.tar.gz"
            )
            run(
                [
                    "gh",
                    "attestation",
                    "verify",
                    str(primary),
                    "--repo",
                    repository,
                ]
            )

        remote = run(
            ["ssh", args.ssh_host, "mktemp", "-d", "/tmp/codex-riscv64-k3.XXXXXXXX"],
            capture=True,
        )
        remote_dir = checked_remote_directory(remote.stdout)
        run(["ssh", args.ssh_host, "mkdir", f"{remote_dir}/candidate"])
        asset_paths = [str(candidate_dir / name) for name in candidate["assets"]]
        asset_paths.append(str(candidate_dir / "candidate.json"))
        run(["scp", *asset_paths, f"{args.ssh_host}:{remote_dir}/candidate/"])
        run(
            [
                "scp",
                str(ROOT / "scripts" / "remote_smoke.py"),
                f"{args.ssh_host}:{remote_dir}/remote_smoke.py",
            ]
        )
        remote_report = f"{remote_dir}/raw-report.json"
        smoke = run(
            [
                "ssh",
                args.ssh_host,
                "python3",
                f"{remote_dir}/remote_smoke.py",
                "--candidate-dir",
                f"{remote_dir}/candidate",
                "--output",
                remote_report,
            ],
            check=False,
        )
        local_raw_report = temporary / "raw-report.json"
        run(["scp", f"{args.ssh_host}:{remote_report}", str(local_raw_report)])
        raw_report = read_json_object(local_raw_report)
        if smoke.returncode != 0 or raw_report.get("overall") != "pass":
            raise ReleaseError("K3 smoke tests failed; inspect the saved report")

        report = {
            **raw_report,
            "candidate_run_id": args.run_id,
            "candidate_run_url": run_info.get("html_url"),
            "candidate_head_sha": candidate["candidate_head_sha"],
            "release_tag": candidate["release_tag"],
            "assets": candidate["assets"],
            "validated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        output = args.output or ROOT / "analysis" / f"k3-report-{manifest.release_tag}.json"
        write_json(output, report)
        preflight_publish(
            manifest,
            candidate_dir,
            output,
            expected_run_id=args.run_id,
        )
        verify_latest_manifest(manifest, token=github_token)
        print(f"K3 report: {output}")

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
                ]
            )
            print("Publish workflow requested; the release environment still requires approval.")
    finally:
        if remote_dir is not None:
            run(
                ["ssh", args.ssh_host, "rm", "-rf", "--", remote_dir],
                check=False,
            )
        shutil.rmtree(temporary)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ReleaseError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
