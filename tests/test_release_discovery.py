from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import release  # noqa: E402
from release_lib import (  # noqa: E402
    Manifest,
    ReleaseError,
    Toolchain,
    Upstream,
    load_policy,
    resolve_stable_manifest,
    resolve_stable_tag,
    should_make_latest,
)


def manifest(version: str = "1.2.3", revision: int = 1) -> Manifest:
    policy = load_policy(ROOT / "release/policy.toml")
    policy = dataclasses.replace(
        policy, distribution=dataclasses.replace(policy.distribution, revision=revision)
    )
    return Manifest(
        policy_document=policy,
        upstream=Upstream(
            repository="openai/codex",
            version=version,
            tag=f"rust-v{version}",
            tag_object_sha="a" * 40,
            commit_sha="b" * 40,
        ),
        toolchain=Toolchain(rust="1.96.0", zig="0.14.0", rusty_v8="151.2.3"),
    )


def published(tag: str, **values: object) -> dict[str, object]:
    return {"tag_name": tag, "draft": False, "prerelease": False, **values}


class StableReleaseResolutionTests(unittest.TestCase):
    def test_exact_release_follows_annotated_tag_without_reading_latest(self) -> None:
        responses = [
            published("rust-v1.2.3"),
            {"object": {"type": "tag", "sha": "a" * 40}},
            {"object": {"type": "commit", "sha": "b" * 40}},
        ]
        with patch("release_lib.github_json", side_effect=responses) as api:
            resolved = resolve_stable_tag("openai/codex", "rust-v1.2.3", token="token")
        self.assertEqual(resolved, manifest().upstream)
        self.assertTrue(
            api.call_args_list[0].args[0].endswith("/releases/tags/rust-v1.2.3")
        )
        self.assertFalse(
            any(
                call.args[0].endswith("/releases/latest") for call in api.call_args_list
            )
        )

    def test_drafts_prereleases_and_mismatched_tags_are_rejected(self) -> None:
        for response in (
            published("rust-v1.2.3", draft=True),
            published("rust-v1.2.3", prerelease=True),
            published("rust-v1.2.4"),
        ):
            with (
                self.subTest(response=response),
                patch("release_lib.github_json", return_value=response),
                self.assertRaises(ReleaseError),
            ):
                resolve_stable_tag("openai/codex", "rust-v1.2.3")

    def test_invalid_ref_target_and_cyclic_tag_chain_are_rejected(self) -> None:
        for responses in (
            [
                published("rust-v1.2.3"),
                {"object": {"type": "commit", "sha": "invalid"}},
            ],
            [
                published("rust-v1.2.3"),
                {"object": {"type": "tag", "sha": "a" * 40}},
                {"object": {"type": "tag", "sha": "a" * 40}},
            ],
        ):
            with (
                self.subTest(responses=responses),
                patch("release_lib.github_json", side_effect=responses),
                self.assertRaises(ReleaseError),
            ):
                resolve_stable_tag("openai/codex", "rust-v1.2.3")

    def test_exact_manifest_derives_toolchain_from_locked_commit(self) -> None:
        expected = manifest()
        with (
            patch(
                "release_lib.resolve_stable_tag", return_value=expected.upstream
            ) as resolve,
            patch(
                "release_lib.resolve_upstream_toolchain",
                return_value=expected.toolchain,
            ) as toolchain,
        ):
            actual = resolve_stable_manifest(expected.policy_document, "rust-v1.2.3")
        self.assertEqual(actual.release_lock(), expected.release_lock())
        resolve.assert_called_once_with("openai/codex", "rust-v1.2.3", token=None)
        toolchain.assert_called_once_with(expected.upstream, "0.14.0", token=None)


class LatestStateTests(unittest.TestCase):
    def test_no_stable_distribution_can_become_latest(self) -> None:
        releases = [
            published("rusty-v8-riscv64-v151.2.3-abc"),
            published("riscv-v9.0.0-r1", draft=True),
            published("riscv-v9.0.0-r1", prerelease=True),
        ]
        with patch("release_lib._github_response", return_value=releases):
            self.assertTrue(should_make_latest(manifest()))

    def test_older_or_equal_release_cannot_replace_latest(self) -> None:
        for existing in ("riscv-v1.2.4-r1", "riscv-v1.2.3-r1", "riscv-v1.2.3-r2"):
            with (
                self.subTest(existing=existing),
                patch(
                    "release_lib._github_response", return_value=[published(existing)]
                ),
            ):
                self.assertFalse(should_make_latest(manifest()))

    def test_release_version_and_revision_are_compared_numerically(self) -> None:
        for target, existing in (
            (manifest("1.2.10"), "riscv-v1.2.9-r99"),
            (manifest("1.2.3", revision=10), "riscv-v1.2.3-r9"),
            (manifest("1.10.0"), "riscv-v1.9.99-r99"),
        ):
            with (
                self.subTest(existing=existing),
                patch(
                    "release_lib._github_response", return_value=[published(existing)]
                ),
            ):
                self.assertTrue(should_make_latest(target))

    def test_highest_version_on_later_page_prevents_latest_regression(self) -> None:
        first_page = [published(f"unrelated-{index}") for index in range(99)]
        first_page.append(published("riscv-v1.2.2-r1"))
        with patch(
            "release_lib._github_response",
            side_effect=[first_page, [published("riscv-v1.2.4-r1")]],
        ) as api:
            self.assertFalse(should_make_latest(manifest(), token="token"))
        self.assertEqual(api.call_count, 2)
        self.assertTrue(api.call_args_list[1].args[0].endswith("per_page=100&page=2"))

    def test_malformed_distribution_release_fails_closed(self) -> None:
        for tag in ("riscv-v1.2.3", "riscv-v1.2.3-r0", "riscv-v1.2.3-rx"):
            with (
                self.subTest(tag=tag),
                patch("release_lib._github_response", return_value=[published(tag)]),
                self.assertRaisesRegex(ReleaseError, "cannot compare"),
            ):
                should_make_latest(manifest())

    def test_pagination_api_failure_is_not_treated_as_empty(self) -> None:
        first_page = [published(f"unrelated-{index}") for index in range(100)]
        with (
            patch(
                "release_lib._github_response",
                side_effect=[first_page, ReleaseError("request failed")],
            ),
            self.assertRaisesRegex(ReleaseError, "request failed"),
        ):
            should_make_latest(manifest())

    def test_cli_emits_json_boolean_and_lowercase_github_output(self) -> None:
        for decision in (True, False):
            with (
                self.subTest(decision=decision),
                tempfile.TemporaryDirectory() as directory,
            ):
                output = Path(directory) / "output"
                stdout = io.StringIO()
                with (
                    patch.object(
                        sys,
                        "argv",
                        ["release.py", "--release-lock", "lock.json", "latest-state"],
                    ),
                    patch("release.load_manifest", return_value=manifest()),
                    patch("release.should_make_latest", return_value=decision),
                    patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}),
                    contextlib.redirect_stdout(stdout),
                ):
                    self.assertEqual(release.main(), 0)
                self.assertEqual(
                    json.loads(stdout.getvalue()), {"make_latest": decision}
                )
                self.assertEqual(
                    output.read_text(), f"make_latest={str(decision).lower()}\n"
                )

    def test_cli_requires_release_lock(self) -> None:
        with (
            patch.object(sys, "argv", ["release.py", "latest-state"]),
            self.assertRaisesRegex(ReleaseError, "requires --release-lock"),
        ):
            release.main()


if __name__ == "__main__":
    unittest.main()
