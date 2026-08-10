from __future__ import annotations

import datetime as dt
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from release import write_build_info  # noqa: E402

from release_lib import (  # noqa: E402
    Manifest,
    REQUIRED_K3_TESTS,
    ReleaseError,
    Toolchain,
    Upstream,
    build_spdx_document,
    check_patch_scope,
    finalize_candidate,
    finalize_v8_artifact,
    load_manifest,
    load_policy,
    normalize_release_cargo_lock,
    patch_series_digest,
    preflight_publish,
    required_payload_names,
    resolve_latest_manifest,
    resolve_upstream_toolchain,
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


POLICY_PATH = ROOT / "release" / "policy.toml"


def test_manifest() -> Manifest:
    return Manifest(
        policy_document=load_policy(POLICY_PATH),
        upstream=Upstream(
            repository="openai/codex",
            version="1.2.3",
            tag="rust-v1.2.3",
            tag_object_sha="a" * 40,
            commit_sha="b" * 40,
        ),
        toolchain=Toolchain(rust="1.96.0", zig="0.14.0", rusty_v8="151.2.3"),
    )


class ManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = test_manifest()

    def test_policy_has_no_codex_version(self) -> None:
        policy = POLICY_PATH.read_text()
        self.assertNotIn("\nversion =", policy)
        self.assertNotIn("tag =", policy)
        self.assertNotIn("commit_sha", policy)
        self.assertEqual(self.manifest.release_tag, "riscv-v1.2.3-r1")
        check_patch_scope(self.manifest.patches_dir)

    def test_policy_rejects_upstream_version_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.toml"
            path.write_text(
                POLICY_PATH.read_text().replace(
                    '[upstream]\nrepository = "openai/codex"',
                    '[upstream]\nrepository = "openai/codex"\nversion = "1.2.3"',
                )
            )
            with self.assertRaisesRegex(ReleaseError, "only repository"):
                load_policy(path)

    def test_workflows_check_the_full_locked_target_graph(self) -> None:
        for workflow_name in ("compat-check.yml", "candidate-build.yml"):
            workflow = (ROOT / ".github" / "workflows" / workflow_name).read_text()
            self.assertIn("cargo metadata", workflow)
            self.assertIn("--locked", workflow)
            self.assertIn("--filter-platform", workflow)
            self.assertNotIn("--no-deps", workflow)

        candidate = (ROOT / ".github/workflows/candidate-build.yml").read_text()
        self.assertNotIn("toolchain: ${{ steps.policy.outputs.rust }}", candidate)
        self.assertIn("rustup toolchain install", candidate)

    def test_v8_and_candidate_workflows_are_separate(self) -> None:
        candidate = (ROOT / ".github/workflows/candidate-build.yml").read_text()
        v8 = (ROOT / ".github/workflows/v8-build.yml").read_text()
        self.assertIn('workflows: ["V8 build"]', candidate)
        self.assertIn("gh release download", candidate)
        self.assertIn("gh attestation verify", candidate)
        self.assertIn("validate-v8", candidate)
        self.assertIn("v8-handoff.json", candidate)
        self.assertIn("release-lock.json", candidate)
        self.assertNotIn("WORKFLOW_RUN_SHA", candidate)
        self.assertNotIn("setup-bazel@", candidate)
        self.assertNotIn("run_bazel_with_buildbuddy.py", candidate)
        self.assertIn('workflows: ["Compatibility check"]', v8)
        self.assertIn("bash scripts/build_v8.sh", v8)
        self.assertIn("gh release create", v8)
        self.assertIn("actions/attest@", v8)
        self.assertIn("v8-handoff.json", v8)
        self.assertIn("release-lock.json", v8)
        candidate_script = (ROOT / "scripts/build_candidate.sh").read_text()
        v8_script = (ROOT / "scripts/build_v8.sh").read_text()
        self.assertNotIn("run_bazel_with_buildbuddy.py", candidate_script)
        self.assertIn("run_bazel_with_buildbuddy.py", v8_script)
        self.assertIn("validate-v8", candidate_script)

    def test_release_lock_round_trip_is_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release-lock.json"
            write_json(path, self.manifest.release_lock())
            self.assertEqual(load_manifest(POLICY_PATH, path), self.manifest)

    def test_upstream_toolchain_is_derived_from_pinned_source(self) -> None:
        files = {
            "codex-rs/rust-toolchain.toml": b'[toolchain]\nchannel = "1.96.0"\n',
            "codex-rs/Cargo.lock": (
                b'[[package]]\nname = "v8"\nversion = "151.2.3"\n'
            ),
        }
        with patch(
            "release_lib.github_repository_file",
            side_effect=lambda _repository, path, _ref, token=None: files[path],
        ):
            resolved = resolve_upstream_toolchain(
                self.manifest.upstream,
                self.manifest.toolchain.zig,
            )
        self.assertEqual(
            resolved,
            Toolchain(rust="1.96.0", zig="0.14.0", rusty_v8="151.2.3"),
        )

    def test_latest_resolution_freezes_one_canonical_lock(self) -> None:
        policy = load_policy(POLICY_PATH)
        with (
            patch("release_lib.resolve_latest_stable", return_value=self.manifest.upstream),
            patch(
                "release_lib.resolve_upstream_toolchain",
                return_value=self.manifest.toolchain,
            ),
        ):
            resolved = resolve_latest_manifest(policy)
        self.assertEqual(resolved.release_lock(), self.manifest.release_lock())

    def test_latest_verification_rejects_a_newer_release(self) -> None:
        newer = Manifest(
            policy_document=self.manifest.policy_document,
            upstream=Upstream(
                repository="openai/codex",
                version="1.2.4",
                tag="rust-v1.2.4",
                tag_object_sha="c" * 40,
                commit_sha="d" * 40,
            ),
            toolchain=self.manifest.toolchain,
        )
        with (
            patch("release_lib.resolve_latest_manifest", return_value=newer),
            self.assertRaisesRegex(ReleaseError, "no longer latest stable"),
        ):
            verify_latest_manifest(self.manifest)


class CargoLockNormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.source = Path(self.temporary.name)
        cargo = self.source / "codex-rs"
        cargo.mkdir()
        (cargo / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["one", "two"]\n\n'
            '[workspace.package]\nversion = "1.2.3"\n'
        )
        for directory, name in (("one", "crate-one"), ("two", "crate-two")):
            member = cargo / directory
            member.mkdir()
            (member / "Cargo.toml").write_text(
                f'[package]\nname = "{name}"\nversion.workspace = true\n'
            )
        self.lock = cargo / "Cargo.lock"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_lock(self, first: str, second: str) -> None:
        self.lock.write_text(
            "version = 4\n\n"
            "[[package]]\nname = \"crate-one\"\n"
            f'version = "{first}"\n\n'
            "[[package]]\nname = \"crate-two\"\n"
            f'version = "{second}"\n'
        )

    def test_normalizes_only_stale_workspace_versions(self) -> None:
        self.write_lock("0.0.0", "1.2.3")
        result = normalize_release_cargo_lock(self.source, "1.2.3")
        self.assertEqual(result["changed_package_count"], 1)
        self.assertEqual(self.lock.read_text().count('version = "1.2.3"'), 2)
        repeated = normalize_release_cargo_lock(self.source, "1.2.3")
        self.assertEqual(repeated["changed_package_count"], 0)
        self.assertEqual(repeated["before_sha256"], repeated["after_sha256"])

    def test_rejects_unexpected_workspace_lock_version(self) -> None:
        self.write_lock("9.9.9", "0.0.0")
        with self.assertRaisesRegex(ReleaseError, "unexpected workspace lock versions"):
            normalize_release_cargo_lock(self.source, "1.2.3")


class CandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = test_manifest()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.candidate_dir = self.root / "candidate"
        self.candidate_dir.mkdir()
        for name in required_payload_names(self.manifest):
            (self.candidate_dir / name).write_bytes(f"asset:{name}\n".encode())
        write_json(
            self.candidate_dir / "release-lock.json", self.manifest.release_lock()
        )
        self.source_info = self.root / "source-info.json"
        write_json(
            self.source_info,
            {
                "schema_version": 1,
                "status": "ready",
                "upstream_commit_sha": self.manifest.upstream.commit_sha,
                "downstream_commit_sha": "1" * 40,
                "policy_sha256": self.manifest.policy_sha256,
                "release_lock_sha256": self.manifest.release_lock_sha256,
            },
        )
        self.now = dt.datetime(2026, 8, 9, 8, 0, tzinfo=dt.timezone.utc)
        self.candidate = finalize_candidate(
            self.manifest,
            self.candidate_dir,
            run_id="12345",
            head_sha="2" * 40,
            source_info_path=self.source_info,
            created_at=self.now,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def passing_report(self) -> Path:
        path = self.root / "k3-report.json"
        write_json(
            path,
            {
                "schema_version": 1,
                "candidate_run_id": "12345",
                "candidate_head_sha": "2" * 40,
                "release_tag": self.manifest.release_tag,
                "overall": "pass",
                "tests": {name: "pass" for name in REQUIRED_K3_TESTS},
                "assets": self.candidate["assets"],
                "finished_at": self.now.isoformat(),
            },
        )
        return path

    def test_candidate_and_k3_preflight_pass(self) -> None:
        validate_candidate(self.manifest, self.candidate_dir)
        candidate, _ = preflight_publish(
            self.manifest,
            self.candidate_dir,
            self.passing_report(),
            expected_run_id="12345",
            now=self.now + dt.timedelta(hours=1),
        )
        self.assertEqual(candidate["release_tag"], self.manifest.release_tag)

    def test_candidate_byte_tampering_fails(self) -> None:
        primary = self.candidate_dir / required_payload_names(self.manifest)[0]
        primary.write_bytes(primary.read_bytes() + b"tampered")
        with self.assertRaisesRegex(ReleaseError, "digest or size mismatch"):
            validate_candidate(self.manifest, self.candidate_dir)

    def test_candidate_asset_omission_fails(self) -> None:
        metadata_path = self.candidate_dir / "candidate.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["assets"].pop("NOTICE")
        write_json(metadata_path, metadata)
        with self.assertRaisesRegex(ReleaseError, "asset set mismatch"):
            validate_candidate(self.manifest, self.candidate_dir)

    def test_unsealed_extra_candidate_file_fails(self) -> None:
        (self.candidate_dir / "debug.bin").write_bytes(b"not sealed")
        with self.assertRaisesRegex(ReleaseError, "directory entry set mismatch"):
            validate_candidate(self.manifest, self.candidate_dir)

    def test_finalizer_rejects_dirty_candidate_directory(self) -> None:
        dirty = self.root / "dirty-candidate"
        dirty.mkdir()
        for name in required_payload_names(self.manifest):
            (dirty / name).write_bytes(f"asset:{name}\n".encode())
        write_json(dirty / "release-lock.json", self.manifest.release_lock())
        (dirty / "debug.bin").write_bytes(b"not sealed")
        with self.assertRaisesRegex(ReleaseError, "unsealed candidate payload"):
            finalize_candidate(
                self.manifest,
                dirty,
                run_id="12345",
                head_sha="2" * 40,
                source_info_path=self.source_info,
                created_at=self.now,
            )

    def test_stale_k3_report_fails(self) -> None:
        with self.assertRaisesRegex(ReleaseError, "too old"):
            preflight_publish(
                self.manifest,
                self.candidate_dir,
                self.passing_report(),
                expected_run_id="12345",
                now=self.now + dt.timedelta(days=8),
            )

    def test_missing_k3_test_fails(self) -> None:
        report_path = self.passing_report()
        report = json.loads(report_path.read_text())
        report["tests"]["code-mode-stdio"] = "fail"
        write_json(report_path, report)
        with self.assertRaisesRegex(ReleaseError, "required tests"):
            preflight_publish(
                self.manifest,
                self.candidate_dir,
                report_path,
                expected_run_id="12345",
                now=self.now,
            )


class V8ArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = test_manifest()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        for name in (
            ".bazelversion",
            ".bazelrc",
            "BUILD.bazel",
            "MODULE.bazel",
            "MODULE.bazel.lock",
            ".github/scripts/run_bazel_with_buildbuddy.py",
            ".github/scripts/rusty_v8_bazel.py",
            ".github/scripts/rusty_v8_module_bazel.py",
        ):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("9.0.0\n" if name == ".bazelversion" else f"{name}\n")
        for name in ("patches/v8.patch", "third_party/v8/BUILD.bazel"):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{name}\n")
        lock = self.source / "codex-rs" / "Cargo.lock"
        lock.parent.mkdir()
        lock.write_text(
            "[[package]]\n"
            'name = "v8"\n'
            f'version = "{self.manifest.toolchain.rusty_v8}"\n'
            'source = "registry+https://github.com/rust-lang/crates.io-index"\n'
            f'checksum = "{"a" * 64}"\n'
        )

        self.v8_dir = self.root / "v8"
        self.v8_dir.mkdir()
        archive, binding, checksums, _build = v8_artifact_names(self.manifest)
        (self.v8_dir / archive).write_bytes(b"archive")
        (self.v8_dir / binding).write_bytes(b"binding")
        sums = "".join(
            f"{hashlib.sha256((self.v8_dir / name).read_bytes()).hexdigest()}  {name}\n"
            for name in (archive, binding)
        )
        (self.v8_dir / checksums).write_text(sums)
        self.metadata = finalize_v8_artifact(
            self.manifest,
            self.source,
            self.v8_dir,
            run_id="45678",
            head_sha="3" * 40,
            source_kind="build",
            created_at=dt.datetime(2026, 8, 10, tzinfo=dt.timezone.utc),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_v8_identity_and_seal_are_deterministic(self) -> None:
        descriptor = v8_input_descriptor(self.manifest, self.source)
        digest = v8_input_digest(descriptor)
        self.assertEqual(self.metadata["input_sha256"], digest)
        self.assertEqual(
            self.metadata["release_tag"], v8_release_tag(self.manifest, self.source)
        )
        self.assertEqual(
            validate_v8_artifact(self.manifest, self.source, self.v8_dir),
            self.metadata,
        )

    def test_unrelated_cargo_lock_package_does_not_bust_v8_identity(self) -> None:
        before = v8_release_tag(self.manifest, self.source)
        lock = self.source / "codex-rs" / "Cargo.lock"
        lock.write_text(
            lock.read_text()
            + "\n[[package]]\nname = \"unrelated\"\nversion = \"1.2.3\"\n"
        )
        self.assertEqual(v8_release_tag(self.manifest, self.source), before)

    def test_v8_input_or_payload_tampering_fails(self) -> None:
        archive = v8_artifact_names(self.manifest)[0]
        (self.v8_dir / archive).write_bytes(b"tampered")
        with self.assertRaisesRegex(ReleaseError, "digest or size mismatch"):
            validate_v8_artifact(self.manifest, self.source, self.v8_dir)

        (self.source / "third_party/v8/BUILD.bazel").write_text("changed\n")
        with self.assertRaisesRegex(ReleaseError, "release tag does not match inputs"):
            validate_v8_artifact(self.manifest, self.source, self.v8_dir)

    def test_build_info_embeds_independent_v8_provenance(self) -> None:
        source_info = self.root / "source-info.json"
        write_json(
            source_info,
            {
                "schema_version": 1,
                "status": "ready",
                "upstream_commit_sha": self.manifest.upstream.commit_sha,
                "policy_sha256": self.manifest.policy_sha256,
                "release_lock_sha256": self.manifest.release_lock_sha256,
            },
        )
        output = self.root / "build-info.json"
        release_lock = self.root / "release-lock.json"
        write_json(release_lock, self.manifest.release_lock())

        def version(command: list[str], cwd: Path | None = None) -> str:
            del cwd
            if command[0] == "python3":
                return self.manifest.toolchain.rusty_v8
            if command[0] == "rustc":
                return "rustc 1.95.0 test"
            if command[0] == "cargo":
                return "cargo 1.95.0 test"
            if command[0] == "zig":
                return "0.14.0"
            raise AssertionError(f"unexpected version command: {command}")

        with patch("release.command_version", side_effect=version):
            write_build_info(
                POLICY_PATH,
                release_lock,
                source_info,
                self.source,
                self.v8_dir / "v8-build.json",
                output,
            )
        build = json.loads(output.read_text())
        self.assertEqual(build["toolchain"]["bazel"], "9.0.0")
        self.assertEqual(build["v8"]["input_sha256"], self.metadata["input_sha256"])
        self.assertEqual(build["v8"]["builder"], self.metadata["builder"])

class SbomTests(unittest.TestCase):
    def test_spdx_output_is_deterministic_for_fixed_time(self) -> None:
        created = dt.datetime(2026, 8, 9, tzinfo=dt.timezone.utc)
        metadata = {
            "packages": [
                {"name": "zeta", "version": "2.0.0", "license": None},
                {"name": "alpha", "version": "1.0.0", "license": "MIT"},
            ]
        }
        document = build_spdx_document(
            metadata,
            name="test",
            namespace_seed="seed",
            created_at=created,
        )
        self.assertEqual(document["spdxVersion"], "SPDX-2.3")
        self.assertEqual(
            [package["name"] for package in document["packages"]],
            ["alpha", "zeta"],
        )
        self.assertEqual(document["packages"][1]["licenseDeclared"], "NOASSERTION")

    def test_spdx_merges_ripgrep_dependency_graph(self) -> None:
        codex_id = "path+file:///source/codex#codex-cli@1.2.3"
        ripgrep_id = "registry+https://github.com/rust-lang/crates.io-index#ripgrep@15.2.0"
        pcre2_id = "registry+https://github.com/rust-lang/crates.io-index#pcre2@0.2.11"
        codex = {
            "packages": [
                {
                    "id": codex_id,
                    "name": "codex-cli",
                    "version": "1.2.3",
                    "license": "Apache-2.0",
                    "source": None,
                }
            ],
            "resolve": {"nodes": [{"id": codex_id, "dependencies": []}]},
        }
        ripgrep = {
            "packages": [
                {
                    "id": ripgrep_id,
                    "name": "ripgrep",
                    "version": "15.2.0",
                    "license": "Unlicense OR MIT",
                    "source": "registry+https://github.com/rust-lang/crates.io-index",
                },
                {
                    "id": pcre2_id,
                    "name": "pcre2",
                    "version": "0.2.11",
                    "license": "MIT OR Apache-2.0",
                    "source": "registry+https://github.com/rust-lang/crates.io-index",
                },
            ],
            "resolve": {
                "nodes": [
                    {"id": ripgrep_id, "dependencies": [pcre2_id]},
                    {"id": pcre2_id, "dependencies": []},
                ]
            },
        }
        document = build_spdx_document(
            [codex, ripgrep],
            name="test",
            namespace_seed="merged",
            created_at=dt.datetime(2026, 8, 9, tzinfo=dt.timezone.utc),
        )
        package_ids = {
            package["name"]: package["SPDXID"] for package in document["packages"]
        }
        self.assertEqual(set(package_ids), {"codex-cli", "ripgrep", "pcre2"})
        self.assertIn(
            {
                "spdxElementId": package_ids["ripgrep"],
                "relationshipType": "DEPENDS_ON",
                "relatedSpdxElement": package_ids["pcre2"],
            },
            document["relationships"],
        )


class CandidateRunTests(unittest.TestCase):
    def run_metadata(self, path: str) -> dict[str, object]:
        return {
            "id": 12345,
            "conclusion": "success",
            "head_branch": "main",
            "head_sha": "2" * 40,
            "path": path,
        }

    def test_accepts_actions_api_workflow_path(self) -> None:
        validate_candidate_run(
            self.run_metadata(".github/workflows/candidate-build.yml"),
            expected_run_id="12345",
            candidate_head_sha="2" * 40,
        )

    def test_accepts_workflow_reference_path(self) -> None:
        validate_candidate_run(
            self.run_metadata(".github/workflows/candidate-build.yml@refs/heads/main"),
            expected_run_id="12345",
            candidate_head_sha="2" * 40,
        )

    def test_rejects_different_workflow(self) -> None:
        with self.assertRaisesRegex(ReleaseError, "unexpected candidate workflow"):
            validate_candidate_run(
                self.run_metadata(".github/workflows/publish.yml"),
                expected_run_id="12345",
            )


if __name__ == "__main__":
    unittest.main()
