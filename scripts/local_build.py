"""在维护者工作站上构建可发布的 RISC-V 候选包。"""

import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any

from release_lib import (
    VERSION_RE,
    Manifest,
    PolicyDocument,
    ReleaseError,
    check_patch_scope,
    git_output,
    load_manifest,
    patch_series_digest,
    prepare_source,
    read_json_object,
    resolve_stable_manifest,
    run,
    validate_candidate,
    validate_v8_artifact,
    write_json,
)


def local_head(repo_root: Path) -> str:
    if git_output(repo_root, "status", "--porcelain", "--untracked-files=no"):
        raise ReleaseError("本地发布需要已提交的下游源码，工作树含有修改")
    return git_output(repo_root, "rev-parse", "HEAD")


def _source_tree(
    manifest: Manifest, work_dir: Path, lock_path: Path
) -> tuple[Path, Path]:
    source = work_dir / "source"
    source_info = work_dir / "source-info.json"
    if not source.exists():
        with tempfile.TemporaryDirectory(prefix=".source-", dir=work_dir) as temp:
            prepared = Path(temp) / "source"
            prepare_source(manifest, prepared, report_path=source_info)
            prepared.rename(source)

    info = read_json_object(source_info)
    expected = {
        "status": "ready",
        "upstream_commit_sha": manifest.upstream.commit_sha,
        "release_lock_sha256": manifest.release_lock_sha256,
        "policy_sha256": manifest.policy_sha256,
        "patch_series_sha256": patch_series_digest(manifest.patches_dir),
        "downstream_commit_sha": git_output(source, "rev-parse", "HEAD"),
    }
    if any(info.get(key) != value for key, value in expected.items()):
        raise ReleaseError(f"{source_info} 与 {lock_path} 或当前补丁不匹配")
    if git_output(source, "status", "--porcelain", "--untracked-files=no"):
        raise ReleaseError(f"上游构建工作树含有修改，请使用新的工作目录：{source}")
    return source, source_info


def _build_environment(
    manifest: Manifest, source: Path, work_dir: Path
) -> dict[str, str]:
    runner_temp = work_dir / "runner"
    runner_temp.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "CARGO_HOME": os.environ.get("CARGO_HOME") or str(Path.home() / ".cargo"),
        "CARGO_TARGET_DIR": str(source / "codex-rs" / "target"),
        "RUSTUP_TOOLCHAIN": manifest.toolchain.rust,
        "RUNNER_TEMP": str(runner_temp),
    }
    zig = run(["zig", "version"], env=env, capture_output=True).stdout.strip()
    if zig != manifest.toolchain.zig:
        raise ReleaseError(f"需要 Zig {manifest.toolchain.zig}，实际为 {zig}")
    bazel = (source / ".bazelversion").read_text(encoding="utf-8").strip()
    actual_bazel = run(
        [env.get("CODEX_BAZEL_BIN", "bazel"), "--version"],
        cwd=source,
        env=env,
        capture_output=True,
    ).stdout.strip()
    if actual_bazel != f"bazel {bazel}":
        raise ReleaseError(f"需要 Bazel {bazel}，实际为 {actual_bazel}")
    targets = run(
        ["rustup", "target", "list", "--installed"], env=env, capture_output=True
    ).stdout.splitlines()
    if manifest.distribution.target not in targets:
        raise ReleaseError(
            f"请为 Rust {manifest.toolchain.rust} 安装 {manifest.distribution.target}"
        )
    return env


def _musl_environment(source: Path, env: dict[str, str], target: str) -> dict[str, str]:
    with tempfile.TemporaryDirectory(
        prefix="musl-env-", dir=env["RUNNER_TEMP"]
    ) as temp:
        env_file = Path(temp) / "build.env"
        run(
            ["bash", str(source / ".github/scripts/install-musl-build-tools.sh")],
            cwd=source,
            env={
                **env,
                "TARGET": target,
                "GITHUB_ENV": str(env_file),
                "CODEX_MUSL_SKIP_HOST_PACKAGES": "1",
            },
        )
        target_env = dict(env)
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if not separator or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None:
                raise ReleaseError(f"musl 工具链输出了无效的环境变量名：{key!r}")
            target_env[key] = value
    return target_env


def local_build(
    repo_root: Path, policy: PolicyDocument, version: str, work_dir: Path
) -> dict[str, Any]:
    import fcntl

    if VERSION_RE.fullmatch(version) is None:
        raise ReleaseError("--version 必须是 X.Y.Z 格式的稳定版")
    if policy.path.resolve() != repo_root / "release/policy.toml":
        raise ReleaseError("本地构建使用所提交的 release/policy.toml")
    head_sha = local_head(repo_root)
    check_patch_scope(policy.patches_dir)
    work_dir = work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    with (work_dir / ".build.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        lock_path = work_dir / "release-lock.json"
        if lock_path.exists():
            manifest = load_manifest(policy.path, lock_path)
        else:
            token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
            manifest = resolve_stable_manifest(policy, f"rust-v{version}", token=token)
            write_json(lock_path, manifest.release_lock())
        if manifest.upstream.version != version:
            raise ReleaseError(f"{lock_path} 已锁定其他上游版本")

        candidate_dir = work_dir / "candidate"
        if candidate_dir.exists():
            candidate = validate_candidate(
                manifest, candidate_dir, expected_origin="local"
            )
            if candidate["candidate_head_sha"] != head_sha:
                raise ReleaseError(
                    f"{candidate_dir} 来自其他下游提交，请使用新的工作目录"
                )
            return candidate

        source, source_info = _source_tree(manifest, work_dir, lock_path)
        env = _build_environment(manifest, source, work_dir)
        env["DOWNSTREAM_SOURCE_SHA"] = head_sha
        v8_dir = work_dir / "v8"
        if v8_dir.exists():
            v8 = validate_v8_artifact(manifest, source, v8_dir, allow_local=True)
            if v8["builder"]["source_kind"] != "local":
                raise ReleaseError(f"{v8_dir} 须包含本机编译的 V8")
        else:
            with tempfile.TemporaryDirectory(prefix=".v8-", dir=work_dir) as temp:
                output = Path(temp) / "v8"
                run(
                    [
                        "bash",
                        str(repo_root / "scripts/build_v8.sh"),
                        "--source-dir",
                        str(source),
                        "--release-lock",
                        str(lock_path),
                        "--v8-dir",
                        str(output),
                        "--local",
                    ],
                    env=env,
                )
                output.rename(v8_dir)

        env = _musl_environment(source, env, manifest.distribution.target)
        if local_head(repo_root) != head_sha:
            raise ReleaseError("构建期间下游源码发生变化")
        with tempfile.TemporaryDirectory(prefix=".candidate-", dir=work_dir) as temp:
            output = Path(temp) / "candidate"
            run(
                [
                    "bash",
                    str(repo_root / "scripts/build_candidate.sh"),
                    "--source-dir",
                    str(source),
                    "--source-info",
                    str(source_info),
                    "--release-lock",
                    str(lock_path),
                    "--v8-dir",
                    str(v8_dir),
                    "--candidate-dir",
                    str(output),
                    "--local-candidate-id",
                    uuid.uuid4().hex,
                ],
                env=env,
            )
            if local_head(repo_root) != head_sha:
                raise ReleaseError("构建期间下游源码发生变化")
            candidate = validate_candidate(manifest, output, expected_origin="local")
            output.rename(candidate_dir)
        return candidate
