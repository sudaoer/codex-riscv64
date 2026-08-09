from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from release_lib import (  # noqa: E402
    REQUIRED_K3_TESTS,
    ReleaseError,
    Toolchain,
    build_spdx_document,
    check_patch_scope,
    finalize_candidate,
    load_manifest,
    patch_series_digest,
    preflight_publish,
    replace_manifest_values,
    required_payload_names,
    resolve_upstream_toolchain,
    validate_candidate,
    validate_candidate_run,
    write_json,
)


class ManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_manifest(ROOT / "release" / "upstream.toml")

    def test_initial_release_identity_is_pinned(self) -> None:
        self.assertEqual(self.manifest.release_tag, "riscv-v0.147.0-r1")
        self.assertEqual(
            self.manifest.upstream.commit_sha,
            "be6e8eac029b183056b7e4402879f15d2c85f61b",
        )
        self.assertEqual(
            patch_series_digest(self.manifest.patches_dir),
            "86e47e0ad2c1b3d55ecb5aa33c13af7e68349e35d5b774d012f607ef8e2a018b",
        )
        check_patch_scope(self.manifest.patches_dir)

    def test_workflows_check_the_full_locked_target_graph(self) -> None:
        for workflow_name in ("compat-check.yml", "candidate-build.yml"):
            workflow = (ROOT / ".github" / "workflows" / workflow_name).read_text()
            self.assertIn("cargo metadata", workflow)
            self.assertIn("--locked", workflow)
            self.assertIn("--filter-platform", workflow)
            self.assertNotIn("--no-deps", workflow)

        candidate = (ROOT / ".github/workflows/candidate-build.yml").read_text()
        self.assertNotIn("toolchain: ${{ steps.policy.outputs.rust }}", candidate)

    def test_manifest_update_replaces_only_named_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "upstream.toml"
            path.write_text(
                "[distribution]\nrevision = 9\n\n[upstream]\nversion = \"1.2.3\"\n"
            )
            replace_manifest_values(
                path,
                {
                    ("distribution", "revision"): 1,
                    ("upstream", "version"): "2.0.0",
                },
            )
            self.assertEqual(
                path.read_text(),
                "[distribution]\nrevision = 1\n\n[upstream]\nversion = \"2.0.0\"\n",
            )

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
                self.manifest.toolchain,
            )
        self.assertEqual(
            resolved,
            Toolchain(rust="1.96.0", zig="0.14.0", rusty_v8="151.2.3"),
        )


class CandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_manifest(ROOT / "release" / "upstream.toml")
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.candidate_dir = self.root / "candidate"
        self.candidate_dir.mkdir()
        for name in required_payload_names(self.manifest):
            (self.candidate_dir / name).write_bytes(f"asset:{name}\n".encode())
        self.source_info = self.root / "source-info.json"
        write_json(
            self.source_info,
            {
                "schema_version": 1,
                "status": "ready",
                "upstream_commit_sha": self.manifest.upstream.commit_sha,
                "downstream_commit_sha": "1" * 40,
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
        codex_id = "path+file:///source/codex#codex-cli@0.147.0"
        ripgrep_id = "registry+https://github.com/rust-lang/crates.io-index#ripgrep@15.2.0"
        pcre2_id = "registry+https://github.com/rust-lang/crates.io-index#pcre2@0.2.11"
        codex = {
            "packages": [
                {
                    "id": codex_id,
                    "name": "codex-cli",
                    "version": "0.147.0",
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
