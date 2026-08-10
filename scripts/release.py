#!/usr/bin/env python3
"""CLI for reconstructing, building, and promoting Codex RISC-V releases."""

import argparse
import dataclasses
import json
import os
import subprocess
import sys
import urllib.parse
from pathlib import Path

from release_lib import (
    ReleaseError,
    build_spdx_document,
    check_patch_scope,
    check_release_state,
    decide_build_required,
    finalize_candidate,
    finalize_v8_artifact,
    github_output,
    load_manifest,
    load_policy,
    patch_series_digest,
    preflight_publish,
    prepare_source,
    read_json_object,
    resolve_latest_manifest,
    validate_candidate,
    validate_candidate_run,
    validate_v8_artifact,
    verify_latest_manifest,
    v8_artifact_names,
    v8_input_descriptor,
    v8_input_digest,
    v8_release_tag,
    write_json,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "release" / "policy.toml"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--policy", type=Path, default=DEFAULT_POLICY, help="release policy"
    )
    result.add_argument("--release-lock", type=Path, help="resolved release lock")
    commands = result.add_subparsers(dest="command", required=True)

    commands.add_parser("validate-policy", help="validate the version-free policy")
    commands.add_parser("validate", help="validate the policy and release lock")

    release_state = commands.add_parser(
        "release-state", help="check and verify the formal downstream release"
    )
    release_state.add_argument(
        "--download-dir",
        type=Path,
        help="directory for downloaded release assets (temporary by default)",
    )

    decide_build = commands.add_parser(
        "decide-build", help="decide whether a Candidate build is required"
    )
    decide_build.add_argument("--event-name", required=True)
    decide_build.add_argument(
        "--force-rebuild", choices=("true", "false"), required=True
    )
    decide_build.add_argument(
        "--release-hit", choices=("true", "false"), required=True
    )

    prepare = commands.add_parser("prepare", help="reconstruct patched upstream source")
    prepare.add_argument("--source-dir", type=Path, required=True)
    prepare.add_argument("--upstream-url")
    prepare.add_argument("--report", type=Path)

    resolve = commands.add_parser(
        "resolve-latest", help="resolve latest stable Codex to an immutable lock"
    )
    resolve.add_argument("--output", type=Path, required=True)
    commands.add_parser(
        "verify-latest", help="verify that a release lock is still latest stable"
    )

    build_info = commands.add_parser("build-info", help="write build provenance")
    build_info.add_argument("--source-info", type=Path, required=True)
    build_info.add_argument("--output", type=Path, required=True)
    build_info.add_argument("--source-dir", type=Path, required=True)
    build_info.add_argument("--v8-build", type=Path, required=True)

    v8_key = commands.add_parser(
        "v8-key", help="derive the immutable V8 release identity"
    )
    v8_key.add_argument("--source-dir", type=Path, required=True)

    finalize_v8 = commands.add_parser(
        "finalize-v8", help="seal an independently built V8 release pair"
    )
    finalize_v8.add_argument("--source-dir", type=Path, required=True)
    finalize_v8.add_argument("--v8-dir", type=Path, required=True)
    finalize_v8.add_argument("--run-id", required=True)
    finalize_v8.add_argument("--head-sha", required=True)
    finalize_v8.add_argument(
        "--source-kind", choices=("build", "bootstrap"), required=True
    )
    finalize_v8.add_argument("--bootstrap-candidate-run-id")

    validate_v8 = commands.add_parser(
        "validate-v8", help="verify an independent V8 release pair"
    )
    validate_v8.add_argument("--source-dir", type=Path, required=True)
    validate_v8.add_argument("--v8-dir", type=Path, required=True)

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
    policy_path: Path,
    release_lock_path: Path,
    source_info_path: Path,
    source_dir: Path,
    v8_build_path: Path,
    output: Path,
) -> None:
    manifest = load_manifest(policy_path, release_lock_path)
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
    v8_build = validate_v8_artifact(manifest, source_dir, v8_build_path.parent)
    if v8_build_path.name != "v8-build.json":
        raise ReleaseError("V8 build metadata must be named v8-build.json")
    v8_input = v8_build["input"]
    build = {
        "schema_version": 1,
        "policy_sha256": manifest.policy_sha256,
        "release_lock_sha256": manifest.release_lock_sha256,
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
            "bazel": v8_input["bazel"],
            "bazelisk": v8_input["bazelisk"],
            "rusty_v8_resolved": resolved_v8,
        },
        "v8": {
            "release_tag": v8_build["release_tag"],
            "input_sha256": v8_build["input_sha256"],
            "builder": v8_build["builder"],
            "assets": v8_build["assets"],
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
    policy_document = load_policy(args.policy)
    if args.command == "validate-policy":
        values = {
            "target": policy_document.distribution.target,
            "zig": policy_document.zig,
            "patch_series_sha256": patch_series_digest(policy_document.patches_dir),
            "policy_sha256": policy_document.sha256,
        }
        github_output(values)
        print(json.dumps(values, indent=2, sort_keys=True))
        return 0
    if args.command == "resolve-latest":
        manifest = resolve_latest_manifest(
            policy_document, token=os.environ.get("GITHUB_TOKEN")
        )
        write_json(args.output, manifest.release_lock())
        values = {
            "release_tag": manifest.release_tag,
            "package_version": manifest.package_version,
            "upstream_commit": manifest.upstream.commit_sha,
            "rust": manifest.toolchain.rust,
            "zig": manifest.toolchain.zig,
            "rusty_v8": manifest.toolchain.rusty_v8,
            "release_lock_sha256": manifest.release_lock_sha256,
        }
        github_output(values)
        print(json.dumps(values, indent=2, sort_keys=True))
        return 0
    if args.command == "decide-build":
        build_required, reason = decide_build_required(
            args.event_name,
            args.force_rebuild == "true",
            args.release_hit == "true",
        )
        github_output(
            {
                "build_required": "true" if build_required else "false",
                "reason": reason,
            }
        )
        print(
            json.dumps(
                {"build_required": build_required, "reason": reason},
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.release_lock is None:
        raise ReleaseError(f"{args.command} requires --release-lock")
    manifest = load_manifest(args.policy, args.release_lock)

    if args.command == "release-state":
        state = check_release_state(
            manifest,
            token=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"),
            download_dir=args.download_dir,
        )
        release_url = (
            f"https://github.com/{manifest.distribution.repository}"
            f"/releases/tag/{urllib.parse.quote(manifest.release_tag, safe='')}"
        )
        github_output(
            {
                "hit": "true" if state["exists"] else "false",
                "release_tag": manifest.release_tag,
                "release_url": release_url,
            }
        )
        print(json.dumps(state, indent=2, sort_keys=True))
        return 0
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
            "policy_sha256": manifest.policy_sha256,
            "release_lock_sha256": manifest.release_lock_sha256,
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
    elif args.command == "verify-latest":
        verify_latest_manifest(manifest, token=os.environ.get("GITHUB_TOKEN"))
        print(json.dumps({"release_tag": manifest.release_tag}, indent=2))
    elif args.command == "build-info":
        write_build_info(
            args.policy,
            args.release_lock,
            args.source_info,
            args.source_dir,
            args.v8_build,
            args.output,
        )
    elif args.command == "v8-key":
        descriptor = v8_input_descriptor(manifest, args.source_dir)
        digest = v8_input_digest(descriptor)
        archive, binding, checksums, build = v8_artifact_names(manifest)
        values = {
            "v8_release_tag": v8_release_tag(manifest, args.source_dir),
            "v8_input_sha256": digest,
            "rusty_v8": manifest.toolchain.rusty_v8,
            "v8_archive": archive,
            "v8_binding": binding,
            "v8_checksums": checksums,
            "v8_build": build,
            "bazelisk": descriptor["bazelisk"],
        }
        github_output(values)
        print(json.dumps({**values, "input": descriptor}, indent=2, sort_keys=True))
    elif args.command == "finalize-v8":
        result = finalize_v8_artifact(
            manifest,
            args.source_dir,
            args.v8_dir,
            run_id=args.run_id,
            head_sha=args.head_sha,
            source_kind=args.source_kind,
            bootstrap_candidate_run_id=args.bootstrap_candidate_run_id,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
    elif args.command == "validate-v8":
        result = validate_v8_artifact(manifest, args.source_dir, args.v8_dir)
        print(json.dumps(result, indent=2, sort_keys=True))
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
