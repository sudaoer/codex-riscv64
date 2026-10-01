from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from release_lib import (  # noqa: E402
    Manifest,
    ReleaseError,
    Toolchain,
    Upstream,
    fetch_candidate_policy,
    load_manifest,
    load_policy,
    patch_series_digest,
    write_json,
)


class CandidatePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = "sudaoer/codex-riscv64"
        self.source_sha = "a" * 40
        self.files = {
            "release/policy.toml": (ROOT / "release/policy.toml").read_bytes(),
            "patches/series": b"# Immutable source series\nold.patch\n",
            "patches/old.patch": b"pinned patch contents\n",
        }

    def repository_file(
        self, repository: str, path: str, ref: str, *, token: str | None = None
    ) -> bytes:
        self.assertEqual(repository, self.repository)
        self.assertEqual(ref, self.source_sha)
        self.assertEqual(token, "token")
        return self.files[path]

    def test_pinned_policy_and_patches_survive_new_main_inputs(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "release_lib.github_repository_file", side_effect=self.repository_file
            ),
        ):
            root = Path(directory)
            old_path = fetch_candidate_policy(
                self.repository, self.source_sha, root / "old", token="token"
            )
            old_policy = load_policy(old_path)
            locked = Manifest(
                policy_document=old_policy,
                upstream=Upstream(
                    repository="openai/codex",
                    version="1.2.3",
                    tag="rust-v1.2.3",
                    tag_object_sha="b" * 40,
                    commit_sha="c" * 40,
                ),
                toolchain=Toolchain(
                    rust="1.96.0", zig=old_policy.zig, rusty_v8="151.2.3"
                ),
            )
            lock = root / "release-lock.json"
            write_json(lock, locked.release_lock())
            new_path = fetch_candidate_policy(
                self.repository, self.source_sha, root / "new", token="token"
            )
            new_path.write_text(
                new_path.read_text().replace("revision = 1", "revision = 2")
            )
            (root / "new/patches/old.patch").write_text("updated main patch\n")
            self.assertNotEqual(load_policy(new_path).sha256, old_policy.sha256)
            self.assertNotEqual(
                patch_series_digest(root / "new/patches"),
                patch_series_digest(old_policy.patches_dir),
            )
            self.assertEqual(
                load_manifest(old_path, lock).release_lock(), locked.release_lock()
            )
            with self.assertRaises(ReleaseError):
                load_manifest(new_path, lock)

    def test_unsafe_source_sha_is_rejected_before_fetch(self) -> None:
        for sha in ("main", "../main", "a" * 39, "G" * 40):
            with (
                self.subTest(sha=sha),
                tempfile.TemporaryDirectory() as directory,
                patch("release_lib.github_repository_file") as fetch,
                self.assertRaisesRegex(ReleaseError, "source SHA is invalid"),
            ):
                fetch_candidate_policy(self.repository, sha, Path(directory))
            fetch.assert_not_called()

    def test_unsafe_duplicate_or_empty_series_never_fetches_patch_paths(self) -> None:
        for series in (
            b"../outside.patch\n",
            b"/outside.patch\n",
            b"old.txt\n",
            b"old.patch\nold.patch\n",
            b"#empty\n",
        ):
            self.files["patches/series"] = series
            with (
                self.subTest(series=series),
                tempfile.TemporaryDirectory() as directory,
                patch(
                    "release_lib.github_repository_file",
                    side_effect=self.repository_file,
                ) as fetch,
                self.assertRaises(ReleaseError),
            ):
                fetch_candidate_policy(
                    self.repository, self.source_sha, Path(directory), token="token"
                )
            self.assertEqual(fetch.call_count, 2)

    def test_policy_cannot_change_trusted_distribution_repository(self) -> None:
        self.files["release/policy.toml"] = self.files["release/policy.toml"].replace(
            self.repository.encode(), b"other/repository"
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "release_lib.github_repository_file", side_effect=self.repository_file
            ),
            self.assertRaisesRegex(ReleaseError, "repository does not match"),
        ):
            fetch_candidate_policy(
                self.repository, self.source_sha, Path(directory), token="token"
            )


if __name__ == "__main__":
    unittest.main()
