#!/usr/bin/env python3
"""CLI for reconstructing, building, and promoting Codex RISC-V releases."""

import argparse
import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

from release_lib import (
    ReleaseError,
    build_spdx_document,
    check_patch_scope,
    finalize_candidate,
    github_output,
    load_manifest,
    patch_series_digest,
    preflight_publish,
    prepare_source,
    read_json_object,
    update_to_latest_stable,
    validate_candidate,
    validate_candidate_run,
    write_json,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "release" / "upstream.toml"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--manifest", type=Path, default=DEFAULT_MANIFEST, help="release manifest"
    )
    commands = result.add_subparsers(dest="command", required=True)

    commands.add_parser("validate", help="validate the manifest and patch policy")

    prepare = commands.add_parser("prepare", help="reconstruct patched upstream source")
    prepare.add_argument("--source-dir", type=Path, required=True)
    prepare.add_argument("--upstream-url")
    prepare.add_argument("--report", type=Path)

    commands.add_parser("update-upstream", help="update the manifest to GitHub latest")

    build_info = commands.add_parser("build-info", help="write build provenance")
    build_info.add_argument("--source-info", type=Path, required=True)
    build_info.add_argument("--output", type=Path, required=True)
    build_info.add_argument("--source-dir", type=Path, required=True)

    sbom = commands.add_parser("sbom", help="convert Cargo metadata to SPDX 2.3")
    sbom.add_argument(
        "--cargo-metadata", type=Path, action="append", required=True
    )
    sbom.add_argument("--output", type=Path, required=True)
    sbom.add_argument("--namespace-seed", required=True)

    finalize = commands.add_parser("finalize-candidate", help="seal candidate metadata")
    finalize.add_argument("--candidate-dir", type=Path, required=True)
    finalize.add_argument("--source-info", type=Path, required=True)
    finalize.add_argument("--run-id", required=True)
    finalize.add_argument("--head-sha", required=True)

    validate = commands.add_parser("validate-candidate", help="verify candidate bytes")
    validate.add_argument("--candidate-dir", type=Path, required=True)

    preflight = commands.add_parser("preflight", help="verify candidate and K3 evidence")
    preflight.add_argument("--candidate-dir", type=Path, required=True)
    preflight.add_argument("--k3-report", type=Path, required=True)
    preflight.add_argument("--run-id", required=True)

    validate_run = commands.add_parser(
        "validate-run", help="verify a selected Candidate build run"
    )
    validate_run.add_argument("--run-json", type=Path, required=True)
    validate_run.add_argument("--candidate-dir", type=Path, required=True)
    validate_run.add_argument("--run-id", required=True)

    return result


def command_version(command: list[str], cwd: Path | None = None) -> str:
    try:
        return subprocess.check_output(command, cwd=cwd, text=True).strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise ReleaseError(f"cannot resolve tool version {command}: {error}") from error


def write_build_info(
    manifest_path: Path, source_info_path: Path, source_dir: Path, output: Path
) -> None:
    manifest = load_manifest(manifest_path)
    source_info = read_json_object(source_info_path)
    if source_info.get("status") != "ready":
        raise ReleaseError("source-info is not ready")
    resolved_v8 = command_version(
        [
            "python3",
            ".github/scripts/rusty_v8_bazel.py",
            "resolved-v8-crate-version",
        ],
        cwd=source_dir,
    )
    if resolved_v8 != manifest.toolchain.rusty_v8:
        raise ReleaseError(
            f"resolved V8 {resolved_v8} does not match manifest "
            f"{manifest.toolchain.rusty_v8}"
        )
    build = {
        "schema_version": 1,
        "release_tag": manifest.release_tag,
        "package_version": manifest.package_version,
        "upstream": dataclasses.asdict(manifest.upstream),
        "distribution": dataclasses.asdict(manifest.distribution),
        "policy": dataclasses.asdict(manifest.policy),
        "toolchain": {
            **dataclasses.asdict(manifest.toolchain),
            "rustc_verbose": command_version(["rustc", "-Vv"]),
            "cargo": command_version(["cargo", "-V"]),
            "zig_resolved": command_version(["zig", "version"]),
            "bazel": command_version(["bazel", "--version"], cwd=source_dir),
            "rusty_v8_resolved": resolved_v8,
        },
        "patch_series_sha256": patch_series_digest(manifest.patches_dir),
        "source": source_info,
        "github": {
            "repository": os.environ.get("GITHUB_REPOSITORY"),
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "sha": os.environ.get("GITHUB_SHA"),
        },
    }
    write_json(output, build)


def main() -> int:
    args = parser().parse_args()
    manifest = load_manifest(args.manifest)

    if args.command == "validate":
        check_patch_scope(manifest.patches_dir)
        values = {
            "release_tag": manifest.release_tag,
            "upstream_commit": manifest.upstream.commit_sha,
            "target": manifest.distribution.target,
            "rust": manifest.toolchain.rust,
            "zig": manifest.toolchain.zig,
            "rusty_v8": manifest.toolchain.rusty_v8,
            "patch_series_sha256": patch_series_digest(manifest.patches_dir),
        }
        github_output(values)
        print(
            json.dumps(
                values,
                indent=2,
                sort_keys=True,
            )
        )
    elif args.command == "prepare":
        check_patch_scope(manifest.patches_dir)
        result = prepare_source(
            manifest,
            args.source_dir,
            upstream_url=args.upstream_url,
            report_path=args.report,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
    elif args.command == "update-upstream":
        changed, latest, toolchain = update_to_latest_stable(
            manifest, token=os.environ.get("GITHUB_TOKEN")
        )
        values = {
            "changed": str(changed).lower(),
            "version": latest.version,
            "tag": latest.tag,
            "tag_object_sha": latest.tag_object_sha,
            "commit_sha": latest.commit_sha,
            "rust": toolchain.rust,
            "zig": toolchain.zig,
            "rusty_v8": toolchain.rusty_v8,
        }
        github_output(values)
        print(json.dumps(values, indent=2, sort_keys=True))
    elif args.command == "build-info":
        write_build_info(
            args.manifest, args.source_info, args.source_dir, args.output
        )
    elif args.command == "sbom":
        cargo_metadata = [read_json_object(path) for path in args.cargo_metadata]
        document = build_spdx_document(
            cargo_metadata,
            name=f"Codex {manifest.package_version} for Linux/riscv64",
            namespace_seed=args.namespace_seed,
        )
        write_json(args.output, document)
    elif args.command == "finalize-candidate":
        candidate = finalize_candidate(
            manifest,
            args.candidate_dir,
            run_id=args.run_id,
            head_sha=args.head_sha,
            source_info_path=args.source_info,
        )
        print(json.dumps(candidate, indent=2, sort_keys=True))
    elif args.command == "validate-candidate":
        candidate = validate_candidate(manifest, args.candidate_dir)
        print(json.dumps(candidate, indent=2, sort_keys=True))
    elif args.command == "preflight":
        candidate, report = preflight_publish(
            manifest,
            args.candidate_dir,
            args.k3_report,
            expected_run_id=args.run_id,
        )
        github_output(
            {
                "release_tag": candidate["release_tag"],
                "candidate_head_sha": candidate["candidate_head_sha"],
            }
        )
        print(
            json.dumps(
                {"candidate": candidate, "k3_report": report},
                indent=2,
                sort_keys=True,
            )
        )
    elif args.command == "validate-run":
        run = read_json_object(args.run_json)
        candidate = validate_candidate(manifest, args.candidate_dir)
        validate_candidate_run(
            run,
            expected_run_id=args.run_id,
            candidate_head_sha=candidate["candidate_head_sha"],
        )
        print(
            json.dumps(
                {
                    "run_id": args.run_id,
                    "head_sha": candidate["candidate_head_sha"],
                    "workflow_path": run["path"],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        raise AssertionError(args.command)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReleaseError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
