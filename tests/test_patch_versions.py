from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from release_lib import (  # noqa: E402
    ReleaseError,
    applicable_patches,
    check_patch_scope,
    patch_series_digest,
)


class PatchVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        (self.directory / "series").write_text("common.patch\nold.patch\nnew.patch\n")
        self.write_patch("common.patch", "")
        self.write_patch("old.patch", "X-Upstream-Before-Version: 0.169.0\n")
        self.write_patch("new.patch", "X-Upstream-Min-Version: 0.169.0\n")

    def write_patch(self, name: str, headers: str, body: str = "") -> None:
        (self.directory / name).write_text(f"Subject: 测试补丁\n{headers}\n{body}")

    def selected(self, version: str) -> list[str]:
        return [path.name for path in applicable_patches(self.directory, version)]

    def test_version_boundary_preserves_order_and_common_patches(self) -> None:
        for version in ("0.168.0", "0.168.100"):
            self.assertEqual(self.selected(version), ["common.patch", "old.patch"])
        for version in ("0.169.0", "0.170.0", "0.1000.0"):
            self.assertEqual(self.selected(version), ["common.patch", "new.patch"])

    def test_body_does_not_set_version_bounds(self) -> None:
        self.write_patch("common.patch", "", "X-Upstream-Min-Version: 99.0.0\n")
        self.assertIn("common.patch", self.selected("0.168.0"))

    def test_invalid_duplicate_and_empty_bounds_are_rejected(self) -> None:
        cases = (
            "X-Upstream-Min-Version: latest\n",
            "X-Upstream-Before-Version: 0.169\n",
            "X-Upstream-Min-Version: 0.169.0\nX-Upstream-Min-Version: 0.169.0\n",
            "X-Upstream-Min-Version: 0.169.0\nX-Upstream-Before-Version: 0.169.0\n",
        )
        for headers in cases:
            with self.subTest(headers=headers):
                self.write_patch("old.patch", headers)
                with self.assertRaises(ReleaseError):
                    self.selected("0.168.0")

    def test_digest_and_scope_cover_inactive_patches(self) -> None:
        before = patch_series_digest(self.directory)
        self.write_patch(
            "new.patch", "X-Upstream-Min-Version: 0.169.0\n",
            "diff --git a/.github/workflows/build.yml b/.github/workflows/build.yml\n",
        )
        self.assertEqual(self.selected("0.168.0"), ["common.patch", "old.patch"])
        self.assertNotEqual(before, patch_series_digest(self.directory))
        with self.assertRaisesRegex(ReleaseError, "forbidden downstream path"):
            check_patch_scope(self.directory)


if __name__ == "__main__":
    unittest.main()
