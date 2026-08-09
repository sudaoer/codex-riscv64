"""Shared release logic for the Codex RISC-V downstream distribution."""

import base64
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import subprocess
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
TAG_RE = re.compile(r"^rust-v([0-9]+\.[0-9]+\.[0-9]+)$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
TARGET = "riscv64gc-unknown-linux-musl"
SCHEMA_VERSION = 1
CANDIDATE_WORKFLOW_PATH = ".github/workflows/candidate-build.yml"
V8_WORKFLOW_PATH = ".github/workflows/v8-build.yml"
# Bump this whenever the V8 build command or its non-source environment changes.
V8_BUILDER_REVISION = 1
V8_BAZELISK_VERSION = "1.28.1"
V8_RUNNER_IMAGE = "ubuntu-24.04"
V8_PLATFORM = "linux_riscv64_musl"
V8_COMPILATION_MODE = "opt"
V8_BAZEL_CONFIGS = (
    "rusty-v8-upstream-libcxx",
    "v8-target-riscv64",
)
V8_INPUT_FILES = (
    ".bazelversion",
    ".bazelrc",
    "BUILD.bazel",
    "MODULE.bazel",
    "MODULE.bazel.lock",
    ".github/scripts/run_bazel_with_buildbuddy.py",
    ".github/scripts/rusty_v8_bazel.py",
    ".github/scripts/rusty_v8_module_bazel.py",
)
V8_INPUT_DIRECTORIES = (
    "patches",
    "third_party/v8",
)
REQUIRED_K3_TESTS = (
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


class ReleaseError(RuntimeError):
    """A release input or invariant failed validation."""


@dataclasses.dataclass(frozen=True)
class Distribution:
    repository: str
    channel: str
    revision: int
    target: str
    candidate_retention_days: int
    k3_report_max_age_days: int


@dataclasses.dataclass(frozen=True)
class Upstream:
    repository: str
    version: str
    tag: str
    tag_object_sha: str
    commit_sha: str


@dataclasses.dataclass(frozen=True)
class Toolchain:
    rust: str
    zig: str
    rusty_v8: str


@dataclasses.dataclass(frozen=True)
class Policy:
    cpu_baseline: str
    allow_rvv: bool
    release_profile: str
    v8_profile: str


@dataclasses.dataclass(frozen=True)
class Manifest:
    path: Path
    distribution: Distribution
    upstream: Upstream
    toolchain: Toolchain
    policy: Policy

    @property
    def release_tag(self) -> str:
        return (
            f"riscv-v{self.upstream.version}-r{self.distribution.revision}"
        )

    @property
    def package_version(self) -> str:
        return (
            f"{self.upstream.version}-riscv.{self.distribution.revision}"
        )

    @property
    def repository_root(self) -> Path:
        return self.path.resolve().parents[1]

    @property
    def patches_dir(self) -> Path:
        return self.repository_root / "patches"

    def validate(self) -> None:
        errors: list[str] = []
        if self.distribution.repository.count("/") != 1:
            errors.append("distribution.repository must be OWNER/REPO")
        if self.distribution.channel != "stable":
            errors.append("only the stable channel is supported")
        if self.distribution.revision < 1:
            errors.append("distribution.revision must be positive")
        if self.distribution.target != TARGET:
            errors.append(f"stable target must be {TARGET}")
        if self.distribution.candidate_retention_days < 8:
            errors.append("candidate retention must cover the seven-day K3 window")
        if not 1 <= self.distribution.k3_report_max_age_days <= 7:
            errors.append("K3 report age must be between one and seven days")
        if self.upstream.repository != "openai/codex":
            errors.append("upstream repository must be openai/codex")
        tag_match = TAG_RE.fullmatch(self.upstream.tag)
        if tag_match is None or tag_match.group(1) != self.upstream.version:
            errors.append("upstream stable tag and version do not agree")
        if not VERSION_RE.fullmatch(self.upstream.version):
            errors.append("upstream version is not a stable semantic version")
        for field_name, value in (
            ("tag_object_sha", self.upstream.tag_object_sha),
            ("commit_sha", self.upstream.commit_sha),
        ):
            if SHA_RE.fullmatch(value) is None:
                errors.append(f"upstream.{field_name} must be a lowercase Git SHA")
        if self.policy.cpu_baseline != "rv64gc":
            errors.append("stable CPU baseline must remain rv64gc")
        if self.policy.allow_rvv:
            errors.append("stable releases must not require RVV")
        if self.policy.release_profile != "release":
            errors.append("stable releases must use the release profile")
        if self.policy.v8_profile != "ptrcomp_sandbox_release":
            errors.append("stable code mode must use sandboxed rusty_v8")
        for field_name, value in dataclasses.asdict(self.toolchain).items():
            if VERSION_RE.fullmatch(value) is None:
                errors.append(f"toolchain.{field_name} must be an exact X.Y.Z version")
        if errors:
            raise ReleaseError("; ".join(errors))
        patch_files(self.patches_dir)


def load_manifest(path: Path) -> Manifest:
    path = path.resolve()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ReleaseError(f"cannot read manifest {path}: {error}") from error
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ReleaseError(f"unsupported manifest schema in {path}")
    try:
        manifest = Manifest(
            path=path,
            distribution=Distribution(**raw["distribution"]),
            upstream=Upstream(**raw["upstream"]),
            toolchain=Toolchain(**raw["toolchain"]),
            policy=Policy(**raw["policy"]),
        )
    except (KeyError, TypeError) as error:
        raise ReleaseError(f"invalid manifest shape in {path}: {error}") from error
    manifest.validate()
    return manifest


def patch_files(patches_dir: Path) -> list[Path]:
    series_path = patches_dir / "series"
    try:
        names = [
            line.strip()
            for line in series_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    except OSError as error:
        raise ReleaseError(f"cannot read patch series: {error}") from error
    if not names:
        raise ReleaseError("patch series is empty")
    if len(names) != len(set(names)):
        raise ReleaseError("patch series contains duplicate entries")
    paths: list[Path] = []
    for name in names:
        if Path(name).name != name or not name.endswith(".patch"):
            raise ReleaseError(f"unsafe patch-series entry: {name}")
        path = patches_dir / name
        if not path.is_file():
            raise ReleaseError(f"missing patch-series entry: {path}")
        paths.append(path)
    return paths


def patch_series_digest(patches_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in patch_files(patches_dir):
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def check_patch_scope(patches_dir: Path) -> None:
    forbidden = (
        ".github/workflows/",
        ".github/dotslash-config.json",
    )
    errors: list[str] = []
    for patch in patch_files(patches_dir):
        for line in patch.read_text(encoding="utf-8").splitlines():
            if not line.startswith("diff --git a/"):
                continue
            path = line.split(" b/", 1)[0].removeprefix("diff --git a/")
            if path.startswith(forbidden):
                errors.append(f"{patch.name}: forbidden downstream path {path}")
    if errors:
        raise ReleaseError("\n".join(errors))


def run(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    capture_output: bool = False,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    print(f"+ {shlex.join(command)}", flush=True)
    return subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        capture_output=capture_output,
        env=env,
    )


def git_output(source_dir: Path, *args: str) -> str:
    return run(
        ["git", *args], cwd=source_dir, capture_output=True
    ).stdout.strip()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def prepare_source(
    manifest: Manifest,
    source_dir: Path,
    *,
    upstream_url: str | None = None,
    report_path: Path | None = None,
) -> dict[str, Any]:
    source_dir = source_dir.resolve()
    if source_dir.exists() and any(source_dir.iterdir()):
        raise ReleaseError(f"source directory must be empty: {source_dir}")
    source_dir.mkdir(parents=True, exist_ok=True)
    upstream_url = upstream_url or f"https://github.com/{manifest.upstream.repository}.git"
    report_path = report_path or source_dir.parent / "source-info.json"
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "preparing",
        "upstream": dataclasses.asdict(manifest.upstream),
        "patch_series_sha256": patch_series_digest(manifest.patches_dir),
        "patches": [path.name for path in patch_files(manifest.patches_dir)],
    }
    write_json(report_path, report)
    current_patch: str | None = None
    try:
        run(["git", "init", "-b", "downstream"], cwd=source_dir)
        run(["git", "remote", "add", "upstream", upstream_url], cwd=source_dir)
        run(
            [
                "git",
                "fetch",
                "--no-tags",
                "--force",
                "upstream",
                f"+refs/tags/{manifest.upstream.tag}:refs/tags/{manifest.upstream.tag}",
            ],
            cwd=source_dir,
        )
        tag_object = git_output(
            source_dir, "rev-parse", f"refs/tags/{manifest.upstream.tag}"
        )
        commit = git_output(
            source_dir, "rev-parse", f"refs/tags/{manifest.upstream.tag}^{{commit}}"
        )
        if tag_object != manifest.upstream.tag_object_sha:
            raise ReleaseError(
                f"tag object mismatch: expected {manifest.upstream.tag_object_sha}, "
                f"got {tag_object}"
            )
        if commit != manifest.upstream.commit_sha:
            raise ReleaseError(
                f"upstream commit mismatch: expected {manifest.upstream.commit_sha}, "
                f"got {commit}"
            )
        run(["git", "checkout", "--detach", commit], cwd=source_dir)
        run(["git", "config", "user.name", "codex-riscv64-bot"], cwd=source_dir)
        run(
            ["git", "config", "user.email", "codex-riscv64-bot@users.noreply.github.com"],
            cwd=source_dir,
        )
        for patch in patch_files(manifest.patches_dir):
            current_patch = patch.name
            run(
                [
                    "git",
                    "am",
                    "--keep-cr",
                    "--committer-date-is-author-date",
                    str(patch),
                ],
                cwd=source_dir,
            )
        run(["git", "diff", "--check", f"{commit}..HEAD"], cwd=source_dir)
        downstream_commit = git_output(source_dir, "rev-parse", "HEAD")
        report.update(
            {
                "status": "ready",
                "upstream_tag_object_sha": tag_object,
                "upstream_commit_sha": commit,
                "downstream_commit_sha": downstream_commit,
            }
        )
        write_json(report_path, report)
        return report
    except (ReleaseError, subprocess.CalledProcessError) as error:
        report.update(
            {
                "status": "failed",
                "failed_patch": current_patch,
                "error": str(error),
            }
        )
        write_json(report_path, report)
        raise


def github_json(url: str, *, token: str | None = None) -> dict[str, Any]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "codex-riscv64-release-tools",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            value = json.load(response)
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseError(f"GitHub request failed for {url}: {error}") from error
    if not isinstance(value, dict):
        raise ReleaseError(f"GitHub response is not an object: {url}")
    return value


def github_repository_file(
    repository: str, path: str, ref: str, *, token: str | None = None
) -> bytes:
    encoded_path = urllib.parse.quote(path, safe="/")
    encoded_ref = urllib.parse.quote(ref, safe="")
    value = github_json(
        f"https://api.github.com/repos/{repository}/contents/{encoded_path}?ref={encoded_ref}",
        token=token,
    )
    if value.get("type") != "file" or value.get("encoding") != "base64":
        raise ReleaseError(f"GitHub contents response is not a base64 file: {path}")
    content = value.get("content")
    if not isinstance(content, str):
        raise ReleaseError(f"GitHub contents response has no content: {path}")
    try:
        return base64.b64decode("".join(content.split()), validate=True)
    except ValueError as error:
        raise ReleaseError(f"GitHub contents response is invalid base64: {path}") from error


def resolve_upstream_toolchain(
    upstream: Upstream, current: Toolchain, *, token: str | None = None
) -> Toolchain:
    try:
        rust_toolchain = tomllib.loads(
            github_repository_file(
                upstream.repository,
                "codex-rs/rust-toolchain.toml",
                upstream.commit_sha,
                token=token,
            ).decode()
        )
        cargo_lock = tomllib.loads(
            github_repository_file(
                upstream.repository,
                "codex-rs/Cargo.lock",
                upstream.commit_sha,
                token=token,
            ).decode()
        )
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ReleaseError(f"cannot parse upstream toolchain metadata: {error}") from error
    rust = rust_toolchain.get("toolchain", {}).get("channel")
    if not isinstance(rust, str) or VERSION_RE.fullmatch(rust) is None:
        raise ReleaseError("upstream codex-rs/rust-toolchain.toml has no exact stable channel")
    v8_versions = sorted(
        {
            package.get("version")
            for package in cargo_lock.get("package", [])
            if isinstance(package, dict)
            and package.get("name") == "v8"
            and isinstance(package.get("version"), str)
        }
    )
    if len(v8_versions) != 1 or not isinstance(v8_versions[0], str):
        raise ReleaseError(f"expected one upstream v8 crate version, got {v8_versions}")
    if VERSION_RE.fullmatch(v8_versions[0]) is None:
        raise ReleaseError(f"upstream v8 crate version is invalid: {v8_versions[0]}")
    return Toolchain(rust=rust, zig=current.zig, rusty_v8=v8_versions[0])


def resolve_latest_stable(
    upstream_repository: str, *, token: str | None = None
) -> Upstream:
    api = f"https://api.github.com/repos/{upstream_repository}"
    release = github_json(f"{api}/releases/latest", token=token)
    if release.get("draft") or release.get("prerelease"):
        raise ReleaseError("GitHub latest release is not a stable published release")
    tag = release.get("tag_name")
    if not isinstance(tag, str):
        raise ReleaseError("latest release has no tag_name")
    match = TAG_RE.fullmatch(tag)
    if match is None:
        raise ReleaseError(f"latest release tag is not stable Codex: {tag}")

    encoded_tag = urllib.parse.quote(tag, safe="")
    reference = github_json(f"{api}/git/ref/tags/{encoded_tag}", token=token)
    target = reference.get("object")
    if not isinstance(target, dict):
        raise ReleaseError("tag reference has no object")
    tag_object_sha = target.get("sha")
    if not isinstance(tag_object_sha, str) or SHA_RE.fullmatch(tag_object_sha) is None:
        raise ReleaseError("tag reference has an invalid SHA")

    seen: set[str] = set()
    while target.get("type") == "tag":
        sha = target.get("sha")
        if not isinstance(sha, str) or sha in seen:
            raise ReleaseError("invalid or cyclic annotated tag chain")
        seen.add(sha)
        annotated = github_json(f"{api}/git/tags/{sha}", token=token)
        target = annotated.get("object")
        if not isinstance(target, dict):
            raise ReleaseError("annotated tag has no target object")
    commit_sha = target.get("sha")
    if target.get("type") != "commit" or not isinstance(commit_sha, str):
        raise ReleaseError("release tag does not resolve to a commit")
    if SHA_RE.fullmatch(commit_sha) is None:
        raise ReleaseError("release commit SHA is invalid")
    return Upstream(
        repository=upstream_repository,
        version=match.group(1),
        tag=tag,
        tag_object_sha=tag_object_sha,
        commit_sha=commit_sha,
    )


def replace_manifest_values(
    path: Path, updates: Mapping[tuple[str, str], str | int]
) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    section = ""
    seen: set[tuple[str, str]] = set()
    result: list[str] = []
    for line in lines:
        section_match = re.fullmatch(r"\[([^]]+)\]", line.strip())
        if section_match:
            section = section_match.group(1)
            result.append(line)
            continue
        key_match = re.match(r"^([A-Za-z0-9_]+)\s*=", line)
        if key_match and (section, key_match.group(1)) in updates:
            key = (section, key_match.group(1))
            value = updates[key]
            rendered = str(value) if isinstance(value, int) else json.dumps(value)
            result.append(f"{key[1]} = {rendered}")
            seen.add(key)
        else:
            result.append(line)
    missing = set(updates) - seen
    if missing:
        raise ReleaseError(f"manifest keys not found: {sorted(missing)}")
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text("\n".join(result) + "\n", encoding="utf-8")
    temporary.replace(path)


def update_to_latest_stable(
    manifest: Manifest, *, token: str | None = None
) -> tuple[bool, Upstream, Toolchain]:
    latest = resolve_latest_stable(manifest.upstream.repository, token=token)
    toolchain = resolve_upstream_toolchain(latest, manifest.toolchain, token=token)
    if latest == manifest.upstream and toolchain == manifest.toolchain:
        return False, latest, toolchain
    replace_manifest_values(
        manifest.path,
        {
            ("distribution", "revision"): 1,
            ("upstream", "version"): latest.version,
            ("upstream", "tag"): latest.tag,
            ("upstream", "tag_object_sha"): latest.tag_object_sha,
            ("upstream", "commit_sha"): latest.commit_sha,
            ("toolchain", "rust"): toolchain.rust,
            ("toolchain", "rusty_v8"): toolchain.rusty_v8,
        },
    )
    return True, latest, toolchain


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def asset_record(path: Path) -> dict[str, Any]:
    return {"sha256": sha256_file(path), "size": path.stat().st_size}


def v8_artifact_names(manifest: Manifest) -> tuple[str, str, str, str]:
    target = manifest.distribution.target
    profile = manifest.policy.v8_profile
    return (
        f"librusty_v8_{profile}_{target}.a.gz",
        f"src_binding_{profile}_{target}.rs",
        f"rusty_v8_{profile}_{target}.sha256",
        "v8-build.json",
    )


def _v8_lock_record(manifest: Manifest, source_dir: Path) -> dict[str, str]:
    # The rest of Cargo.lock drives the Codex build, not the Bazel V8 pair. By
    # extracting this record, Codex-only dependency churn can reuse exact V8 bytes.
    lock_path = source_dir / "codex-rs" / "Cargo.lock"
    try:
        lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ReleaseError(f"cannot read V8 Cargo lock record: {error}") from error
    packages = [
        package
        for package in lock.get("package", [])
        if isinstance(package, dict) and package.get("name") == "v8"
    ]
    if len(packages) != 1:
        raise ReleaseError(f"expected one V8 Cargo lock record, got {len(packages)}")
    package = packages[0]
    version = package.get("version")
    checksum = package.get("checksum")
    source = package.get("source")
    if version != manifest.toolchain.rusty_v8:
        raise ReleaseError(
            f"locked V8 {version} does not match manifest {manifest.toolchain.rusty_v8}"
        )
    if not isinstance(checksum, str) or re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
        raise ReleaseError("V8 Cargo lock record has no valid checksum")
    if not isinstance(source, str) or not source:
        raise ReleaseError("V8 Cargo lock record has no source")
    return {"version": version, "source": source, "checksum": checksum}


def v8_input_descriptor(manifest: Manifest, source_dir: Path) -> dict[str, Any]:
    source_dir = source_dir.resolve()
    if not source_dir.is_dir():
        raise ReleaseError(f"V8 source directory does not exist: {source_dir}")
    input_paths = [source_dir / name for name in V8_INPUT_FILES]
    for directory_name in V8_INPUT_DIRECTORIES:
        directory = source_dir / directory_name
        if not directory.is_dir() or directory.is_symlink():
            raise ReleaseError(f"V8 input directory is invalid: {directory_name}")
        input_paths.extend(sorted(directory.rglob("*")))

    files: dict[str, dict[str, Any]] = {}
    for path in sorted(input_paths, key=lambda item: item.relative_to(source_dir).as_posix()):
        relative = path.relative_to(source_dir).as_posix()
        if path.is_dir() and not path.is_symlink():
            continue
        if path.is_symlink() or not path.is_file():
            raise ReleaseError(f"V8 input must be a regular file: {relative}")
        files[relative] = asset_record(path)
    missing = [name for name in V8_INPUT_FILES if name not in files]
    if missing:
        raise ReleaseError(f"required V8 inputs are missing: {missing}")

    bazel_version = (source_dir / ".bazelversion").read_text(encoding="utf-8").strip()
    if not bazel_version:
        raise ReleaseError(".bazelversion is empty")
    return {
        "schema_version": SCHEMA_VERSION,
        "builder_revision": V8_BUILDER_REVISION,
        "target": manifest.distribution.target,
        "platform": V8_PLATFORM,
        "profile": manifest.policy.v8_profile,
        "compilation_mode": V8_COMPILATION_MODE,
        "bazel_configs": list(V8_BAZEL_CONFIGS),
        "bazel": bazel_version,
        "bazelisk": V8_BAZELISK_VERSION,
        "runner_image": V8_RUNNER_IMAGE,
        "v8_crate": _v8_lock_record(manifest, source_dir),
        "files": files,
    }


def v8_input_digest(descriptor: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        descriptor, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def v8_release_tag(manifest: Manifest, source_dir: Path) -> str:
    digest = v8_input_digest(v8_input_descriptor(manifest, source_dir))
    return f"rusty-v8-riscv64-v{manifest.toolchain.rusty_v8}-{digest[:12]}"


def finalize_v8_artifact(
    manifest: Manifest,
    source_dir: Path,
    v8_dir: Path,
    *,
    run_id: str,
    head_sha: str,
    source_kind: str,
    bootstrap_candidate_run_id: str | None = None,
    created_at: dt.datetime | None = None,
) -> dict[str, Any]:
    if not run_id.isdigit():
        raise ReleaseError("V8 run ID must be numeric")
    if SHA_RE.fullmatch(head_sha) is None:
        raise ReleaseError("V8 head SHA is invalid")
    if source_kind not in {"build", "bootstrap"}:
        raise ReleaseError("V8 source kind must be build or bootstrap")
    if source_kind == "bootstrap":
        if bootstrap_candidate_run_id is None or not bootstrap_candidate_run_id.isdigit():
            raise ReleaseError("bootstrap V8 source requires a candidate run ID")
    elif bootstrap_candidate_run_id is not None:
        raise ReleaseError("built V8 source cannot name a bootstrap candidate")

    archive_name, binding_name, checksums_name, build_name = v8_artifact_names(
        manifest
    )
    expected = {archive_name, binding_name, checksums_name}
    actual = {path.name for path in v8_dir.iterdir()}
    if actual != expected:
        raise ReleaseError(
            "unsealed V8 artifact set mismatch: "
            f"missing={sorted(expected - actual)}, unexpected={sorted(actual - expected)}"
        )
    unsafe = [
        path.name for path in v8_dir.iterdir() if path.is_symlink() or not path.is_file()
    ]
    if unsafe:
        raise ReleaseError(f"V8 artifacts must be regular files: {unsafe}")

    payload = {
        archive_name: asset_record(v8_dir / archive_name),
        binding_name: asset_record(v8_dir / binding_name),
    }
    expected_sums = "".join(
        f"{payload[name]['sha256']}  {name}\n" for name in (archive_name, binding_name)
    )
    try:
        actual_sums = (v8_dir / checksums_name).read_text(encoding="utf-8")
    except OSError as error:
        raise ReleaseError(f"cannot read V8 checksums: {error}") from error
    if actual_sums != expected_sums:
        raise ReleaseError("V8 checksum manifest does not match payloads")
    assets = {**payload, checksums_name: asset_record(v8_dir / checksums_name)}
    descriptor = v8_input_descriptor(manifest, source_dir)
    digest = v8_input_digest(descriptor)
    builder: dict[str, Any] = {
        "repository": manifest.distribution.repository,
        "workflow": V8_WORKFLOW_PATH,
        "run_id": run_id,
        "head_sha": head_sha,
        "source_kind": source_kind,
    }
    if bootstrap_candidate_run_id is not None:
        builder["bootstrap_candidate_run_id"] = bootstrap_candidate_run_id
    result = {
        "schema_version": SCHEMA_VERSION,
        "created_at": (
            created_at or dt.datetime.now(dt.timezone.utc)
        ).astimezone(dt.timezone.utc).isoformat(),
        "release_tag": f"rusty-v8-riscv64-v{manifest.toolchain.rusty_v8}-{digest[:12]}",
        "input_sha256": digest,
        "input": descriptor,
        "builder": builder,
        "assets": assets,
    }
    write_json(v8_dir / build_name, result)
    return result


def validate_v8_artifact(
    manifest: Manifest, source_dir: Path, v8_dir: Path
) -> dict[str, Any]:
    archive_name, binding_name, checksums_name, build_name = v8_artifact_names(
        manifest
    )
    expected_names = {archive_name, binding_name, checksums_name, build_name}
    actual_names = {path.name for path in v8_dir.iterdir()}
    if actual_names != expected_names:
        raise ReleaseError(
            "V8 artifact set mismatch: "
            f"missing={sorted(expected_names - actual_names)}, "
            f"unexpected={sorted(actual_names - expected_names)}"
        )
    unsafe = [
        path.name for path in v8_dir.iterdir() if path.is_symlink() or not path.is_file()
    ]
    if unsafe:
        raise ReleaseError(f"V8 artifact entries must be regular files: {unsafe}")

    metadata = read_json_object(v8_dir / build_name)
    descriptor = v8_input_descriptor(manifest, source_dir)
    digest = v8_input_digest(descriptor)
    expected_tag = f"rusty-v8-riscv64-v{manifest.toolchain.rusty_v8}-{digest[:12]}"
    if metadata.get("schema_version") != SCHEMA_VERSION:
        raise ReleaseError("V8 build schema is unsupported")
    if metadata.get("release_tag") != expected_tag:
        raise ReleaseError("V8 release tag does not match inputs")
    if metadata.get("input_sha256") != digest or metadata.get("input") != descriptor:
        raise ReleaseError("V8 build input fingerprint does not match source")
    builder = metadata.get("builder")
    if not isinstance(builder, dict):
        raise ReleaseError("V8 build has no builder identity")
    if builder.get("repository") != manifest.distribution.repository:
        raise ReleaseError("V8 builder repository does not match manifest")
    if builder.get("workflow") != V8_WORKFLOW_PATH:
        raise ReleaseError("V8 builder workflow is unexpected")
    if not str(builder.get("run_id", "")).isdigit():
        raise ReleaseError("V8 builder run ID is invalid")
    if SHA_RE.fullmatch(str(builder.get("head_sha", ""))) is None:
        raise ReleaseError("V8 builder head SHA is invalid")
    if builder.get("source_kind") not in {"build", "bootstrap"}:
        raise ReleaseError("V8 build source kind is invalid")
    if builder.get("source_kind") == "bootstrap" and not str(
        builder.get("bootstrap_candidate_run_id", "")
    ).isdigit():
        raise ReleaseError("V8 bootstrap candidate run ID is invalid")

    expected_assets = {
        name: asset_record(v8_dir / name)
        for name in (archive_name, binding_name, checksums_name)
    }
    if metadata.get("assets") != expected_assets:
        raise ReleaseError("V8 asset digest or size mismatch")
    expected_sums = "".join(
        f"{expected_assets[name]['sha256']}  {name}\n"
        for name in (archive_name, binding_name)
    )
    if (v8_dir / checksums_name).read_text(encoding="utf-8") != expected_sums:
        raise ReleaseError("V8 checksum manifest does not match metadata")
    return metadata


def required_payload_names(manifest: Manifest) -> tuple[str, ...]:
    target = manifest.distribution.target
    profile = manifest.policy.v8_profile
    return (
        f"codex-package-{target}.tar.gz",
        f"codex-package-{target}.tar.zst",
        f"codex-app-server-package-{target}.tar.gz",
        f"codex-app-server-package-{target}.tar.zst",
        f"codex-responses-api-proxy-{target}.tar.gz",
        f"librusty_v8_{profile}_{target}.a.gz",
        f"src_binding_{profile}_{target}.rs",
        f"rusty_v8_{profile}_{target}.sha256",
        "build-info.json",
        "sbom.spdx.json",
        "install.sh",
        "LICENSE",
        "NOTICE",
    )


def finalize_candidate(
    manifest: Manifest,
    candidate_dir: Path,
    *,
    run_id: str,
    head_sha: str,
    source_info_path: Path,
    created_at: dt.datetime | None = None,
) -> dict[str, Any]:
    if not run_id.isdigit():
        raise ReleaseError("candidate run ID must be numeric")
    if SHA_RE.fullmatch(head_sha) is None:
        raise ReleaseError("candidate head SHA is invalid")
    source_info = json.loads(source_info_path.read_text(encoding="utf-8"))
    if source_info.get("status") != "ready":
        raise ReleaseError("source-info does not describe a ready source tree")
    if source_info.get("upstream_commit_sha") != manifest.upstream.commit_sha:
        raise ReleaseError("source-info upstream SHA does not match manifest")

    expected_payload_names = set(required_payload_names(manifest))
    actual_payload_names = {path.name for path in candidate_dir.iterdir()}
    if actual_payload_names != expected_payload_names:
        raise ReleaseError(
            "unsealed candidate payload set mismatch: "
            f"missing={sorted(expected_payload_names - actual_payload_names)}, "
            f"unexpected={sorted(actual_payload_names - expected_payload_names)}"
        )
    unsafe_payloads = [
        path.name
        for path in candidate_dir.iterdir()
        if path.is_symlink() or not path.is_file()
    ]
    if unsafe_payloads:
        raise ReleaseError(f"candidate payloads must be regular files: {unsafe_payloads}")

    payload: dict[str, dict[str, Any]] = {}
    for name in required_payload_names(manifest):
        path = candidate_dir / name
        if not path.is_file():
            raise ReleaseError(f"required candidate asset is missing: {name}")
        payload[name] = asset_record(path)

    release = {
        "schema_version": SCHEMA_VERSION,
        "release_tag": manifest.release_tag,
        "package_version": manifest.package_version,
        "upstream": dataclasses.asdict(manifest.upstream),
        "distribution": dataclasses.asdict(manifest.distribution),
        "assets": payload,
    }
    release_path = candidate_dir / "release.json"
    write_json(release_path, release)

    checksummed = {**payload, "release.json": asset_record(release_path)}
    sums_path = candidate_dir / "SHA256SUMS"
    sums_path.write_text(
        "".join(
            f"{record['sha256']}  {name}\n"
            for name, record in sorted(checksummed.items())
        ),
        encoding="utf-8",
    )
    assets = {**checksummed, "SHA256SUMS": asset_record(sums_path)}
    when = created_at or dt.datetime.now(dt.timezone.utc)
    candidate = {
        "schema_version": SCHEMA_VERSION,
        "candidate_run_id": run_id,
        "candidate_head_sha": head_sha,
        "created_at": when.astimezone(dt.timezone.utc).isoformat(),
        "release_tag": manifest.release_tag,
        "package_version": manifest.package_version,
        "upstream": dataclasses.asdict(manifest.upstream),
        "distribution": dataclasses.asdict(manifest.distribution),
        "patch_series_sha256": patch_series_digest(manifest.patches_dir),
        "source": source_info,
        "assets": assets,
    }
    write_json(candidate_dir / "candidate.json", candidate)
    return candidate


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseError(f"cannot read JSON object {path}: {error}") from error
    if not isinstance(value, dict):
        raise ReleaseError(f"expected JSON object in {path}")
    return value


def validate_candidate(manifest: Manifest, candidate_dir: Path) -> dict[str, Any]:
    candidate = read_json_object(candidate_dir / "candidate.json")
    if candidate.get("schema_version") != SCHEMA_VERSION:
        raise ReleaseError("candidate schema is unsupported")
    if candidate.get("release_tag") != manifest.release_tag:
        raise ReleaseError("candidate release tag does not match manifest")
    if candidate.get("patch_series_sha256") != patch_series_digest(
        manifest.patches_dir
    ):
        raise ReleaseError("candidate patch-series digest does not match")
    if candidate.get("upstream") != dataclasses.asdict(manifest.upstream):
        raise ReleaseError("candidate upstream identity does not match manifest")
    if candidate.get("distribution") != dataclasses.asdict(manifest.distribution):
        raise ReleaseError("candidate distribution does not match manifest")
    if not str(candidate.get("candidate_run_id", "")).isdigit():
        raise ReleaseError("candidate run ID is invalid")
    if SHA_RE.fullmatch(str(candidate.get("candidate_head_sha", ""))) is None:
        raise ReleaseError("candidate head SHA is invalid")
    source = candidate.get("source")
    if not isinstance(source, dict) or source.get("status") != "ready":
        raise ReleaseError("candidate source reconstruction is not ready")
    if source.get("upstream_commit_sha") != manifest.upstream.commit_sha:
        raise ReleaseError("candidate source upstream SHA does not match manifest")
    assets = candidate.get("assets")
    if not isinstance(assets, dict):
        raise ReleaseError("candidate has no asset map")
    expected_names = {
        *required_payload_names(manifest),
        "release.json",
        "SHA256SUMS",
    }
    if set(assets) != expected_names:
        raise ReleaseError(
            "candidate asset set mismatch: "
            f"missing={sorted(expected_names - set(assets))}, "
            f"unexpected={sorted(set(assets) - expected_names)}"
        )
    expected_entries = {*expected_names, "candidate.json"}
    actual_entries = {path.name for path in candidate_dir.iterdir()}
    if actual_entries != expected_entries:
        raise ReleaseError(
            "candidate directory entry set mismatch: "
            f"missing={sorted(expected_entries - actual_entries)}, "
            f"unexpected={sorted(actual_entries - expected_entries)}"
        )
    unsafe_entries = [
        path.name
        for path in candidate_dir.iterdir()
        if path.is_symlink() or not path.is_file()
    ]
    if unsafe_entries:
        raise ReleaseError(f"candidate entries must be regular files: {unsafe_entries}")
    for name, expected in assets.items():
        if Path(name).name != name or not isinstance(expected, dict):
            raise ReleaseError(f"invalid candidate asset entry: {name}")
        path = candidate_dir / name
        if not path.is_file():
            raise ReleaseError(f"candidate asset is missing: {name}")
        actual = asset_record(path)
        if actual != expected:
            raise ReleaseError(f"candidate asset digest or size mismatch: {name}")
    release = read_json_object(candidate_dir / "release.json")
    if release.get("release_tag") != manifest.release_tag:
        raise ReleaseError("release.json tag does not match manifest")
    if release.get("upstream") != dataclasses.asdict(manifest.upstream):
        raise ReleaseError("release.json upstream identity does not match manifest")
    if release.get("distribution") != dataclasses.asdict(manifest.distribution):
        raise ReleaseError("release.json distribution does not match manifest")
    release_assets = release.get("assets")
    expected_payload = {
        name: assets[name] for name in required_payload_names(manifest)
    }
    if release_assets != expected_payload:
        raise ReleaseError("release.json payload map does not match candidate")
    expected_sums = "".join(
        f"{assets[name]['sha256']}  {name}\n"
        for name in sorted((*required_payload_names(manifest), "release.json"))
    )
    try:
        actual_sums = (candidate_dir / "SHA256SUMS").read_text(encoding="utf-8")
    except OSError as error:
        raise ReleaseError(f"cannot read SHA256SUMS: {error}") from error
    if actual_sums != expected_sums:
        raise ReleaseError("SHA256SUMS content does not match candidate metadata")
    return candidate


def parse_timestamp(value: Any, *, field: str) -> dt.datetime:
    if not isinstance(value, str):
        raise ReleaseError(f"{field} must be an ISO timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReleaseError(f"{field} is not an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise ReleaseError(f"{field} must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def validate_k3_report(
    manifest: Manifest,
    candidate: Mapping[str, Any],
    report: Mapping[str, Any],
    *,
    now: dt.datetime | None = None,
) -> None:
    if report.get("schema_version") != SCHEMA_VERSION:
        raise ReleaseError("K3 report schema is unsupported")
    if str(report.get("candidate_run_id")) != str(candidate["candidate_run_id"]):
        raise ReleaseError("K3 report run ID does not match candidate")
    if report.get("candidate_head_sha") != candidate["candidate_head_sha"]:
        raise ReleaseError("K3 report head SHA does not match candidate")
    if report.get("release_tag") != manifest.release_tag:
        raise ReleaseError("K3 report release tag does not match manifest")
    if report.get("overall") != "pass":
        raise ReleaseError("K3 report did not pass")
    tests = report.get("tests")
    if not isinstance(tests, dict):
        raise ReleaseError("K3 report has no test map")
    missing = [name for name in REQUIRED_K3_TESTS if tests.get(name) != "pass"]
    if missing:
        raise ReleaseError(f"K3 required tests did not pass: {missing}")
    report_assets = report.get("assets")
    if report_assets != candidate.get("assets"):
        raise ReleaseError("K3 report was not generated from every candidate asset")
    finished_at = parse_timestamp(report.get("finished_at"), field="finished_at")
    current = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    if finished_at > current + dt.timedelta(minutes=5):
        raise ReleaseError("K3 report is dated in the future")
    max_age = dt.timedelta(days=manifest.distribution.k3_report_max_age_days)
    if current - finished_at > max_age:
        raise ReleaseError("K3 report is too old to publish")


def preflight_publish(
    manifest: Manifest,
    candidate_dir: Path,
    k3_report_path: Path,
    *,
    expected_run_id: str,
    now: dt.datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidate = validate_candidate(manifest, candidate_dir)
    if str(candidate.get("candidate_run_id")) != expected_run_id:
        raise ReleaseError("requested run ID does not match candidate metadata")
    report = read_json_object(k3_report_path)
    validate_k3_report(manifest, candidate, report, now=now)
    return candidate, report


def validate_candidate_run(
    run: Mapping[str, Any],
    *,
    expected_run_id: str,
    candidate_head_sha: str | None = None,
) -> None:
    if str(run.get("id")) != expected_run_id:
        raise ReleaseError("GitHub run ID mismatch")
    if run.get("conclusion") != "success":
        raise ReleaseError("candidate workflow did not conclude successfully")
    if run.get("head_branch") != "main":
        raise ReleaseError("candidate workflow did not run from main")
    path = run.get("path")
    if not isinstance(path, str) or path.partition("@")[0] != CANDIDATE_WORKFLOW_PATH:
        raise ReleaseError(f"unexpected candidate workflow path: {path}")
    if candidate_head_sha is not None and run.get("head_sha") != candidate_head_sha:
        raise ReleaseError("GitHub run head SHA does not match candidate")


def build_spdx_document(
    cargo_metadata: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    name: str,
    namespace_seed: str,
    created_at: dt.datetime | None = None,
) -> dict[str, Any]:
    metadata_documents = (
        [cargo_metadata] if isinstance(cargo_metadata, Mapping) else list(cargo_metadata)
    )
    if not metadata_documents:
        raise ReleaseError("at least one Cargo metadata document is required")

    package_records: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    id_maps: list[dict[str, str]] = []
    for metadata in metadata_documents:
        packages = metadata.get("packages")
        if not isinstance(packages, list):
            raise ReleaseError("cargo metadata has no package list")
        id_map: dict[str, str] = {}
        for package in packages:
            if not isinstance(package, Mapping):
                raise ReleaseError("cargo metadata package entry is not an object")
            package_name = package.get("name")
            package_version = package.get("version")
            package_id = package.get("id")
            source = package.get("source")
            if not isinstance(package_name, str) or not isinstance(package_version, str):
                raise ReleaseError("cargo metadata package has no name or version")
            source_key = source if isinstance(source, str) else "local"
            package_key = (package_name, package_version, source_key)
            existing = package_records.get(package_key)
            if existing is None or (
                not existing.get("license") and package.get("license")
            ):
                package_records[package_key] = package
            spdx_id = "SPDXRef-Package-" + hashlib.sha256(
                "@".join(package_key).encode()
            ).hexdigest()[:24]
            if isinstance(package_id, str):
                id_map[package_id] = spdx_id
        id_maps.append(id_map)

    when = created_at or dt.datetime.now(dt.timezone.utc)
    document: dict[str, Any] = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": name,
        "documentNamespace": (
            "https://github.com/sudaoer/codex-riscv64/sbom/"
            + hashlib.sha256(namespace_seed.encode()).hexdigest()
        ),
        "creationInfo": {
            "created": when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "creators": ["Tool: codex-riscv64-release-tools-0.1.0"],
        },
        "packages": [],
        "relationships": [],
    }
    for package_key, package in sorted(package_records.items()):
        spdx_id = "SPDXRef-Package-" + hashlib.sha256(
            "@".join(package_key).encode()
        ).hexdigest()[:24]
        license_value = package.get("license") or "NOASSERTION"
        document["packages"].append(
            {
                "name": package["name"],
                "SPDXID": spdx_id,
                "versionInfo": package["version"],
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "licenseConcluded": "NOASSERTION",
                "licenseDeclared": license_value,
                "externalRefs": [
                    {
                        "referenceCategory": "PACKAGE-MANAGER",
                        "referenceType": "purl",
                        "referenceLocator": (
                            "pkg:cargo/"
                            f"{urllib.parse.quote(package['name'], safe='')}@"
                            f"{urllib.parse.quote(package['version'], safe='')}"
                        ),
                    }
                ],
            }
        )
        document["relationships"].append(
            {
                "spdxElementId": "SPDXRef-DOCUMENT",
                "relationshipType": "DESCRIBES",
                "relatedSpdxElement": spdx_id,
            }
        )

    dependency_relationships: set[tuple[str, str]] = set()
    for metadata, id_map in zip(metadata_documents, id_maps):
        resolve = metadata.get("resolve")
        if resolve is None:
            continue
        if not isinstance(resolve, Mapping) or not isinstance(resolve.get("nodes"), list):
            raise ReleaseError("cargo metadata resolve graph is invalid")
        for node in resolve["nodes"]:
            if not isinstance(node, Mapping) or not isinstance(node.get("id"), str):
                raise ReleaseError("cargo metadata resolve node is invalid")
            source_spdx = id_map.get(node["id"])
            if source_spdx is None:
                continue
            dependencies = node.get("dependencies", [])
            if not isinstance(dependencies, list):
                raise ReleaseError("cargo metadata dependency list is invalid")
            for dependency_id in dependencies:
                if not isinstance(dependency_id, str):
                    raise ReleaseError("cargo metadata dependency ID is invalid")
                dependency_spdx = id_map.get(dependency_id)
                if dependency_spdx is not None and dependency_spdx != source_spdx:
                    dependency_relationships.add((source_spdx, dependency_spdx))
    for source_spdx, dependency_spdx in sorted(dependency_relationships):
        document["relationships"].append(
            {
                "spdxElementId": source_spdx,
                "relationshipType": "DEPENDS_ON",
                "relatedSpdxElement": dependency_spdx,
            }
        )
    return document


def github_output(values: Mapping[str, str]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as output:
        for key, value in values.items():
            if "\n" in value:
                raise ReleaseError(f"GitHub output {key} must be single-line")
            output.write(f"{key}={value}\n")


def load_json_from_stdin() -> dict[str, Any]:
    value = json.load(os.sys.stdin)
    if not isinstance(value, dict):
        raise ReleaseError("stdin JSON must be an object")
    return value


def sorted_asset_paths(candidate_dir: Path, candidate: Mapping[str, Any]) -> Iterable[Path]:
    assets = candidate.get("assets")
    if not isinstance(assets, dict):
        raise ReleaseError("candidate has no assets")
    for name in sorted(assets):
        yield candidate_dir / name
