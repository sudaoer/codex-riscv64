from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from typing import BinaryIO
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import publish_release as publisher
from release_lib import (
    Manifest,
    ReleaseError,
    Toolchain,
    Upstream,
    asset_record,
    formal_release_asset_names,
    load_policy,
)
from release_tag import HTTPError, TagError

REPOSITORY = "sudaoer/codex-riscv64"
TAG = "riscv-v0.171.0-r1"
COMMIT = "c" * 40
MAIN_HEAD = "e" * 40
RELEASES = f"/repos/{REPOSITORY}/releases"
REFS = f"/repos/{REPOSITORY}/git/refs"


class FakeGitHub:
    def __init__(self) -> None:
        self.refs = {"refs/heads/main": MAIN_HEAD}
        self.releases: dict[int, dict] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.uploads: list[tuple[int, str]] = []
        self.interrupt_upload = False
        self.corrupt_upload = False
        self.publish_timeout = ""
        self.last_asset_id = 800

    def list_releases(self, repository: str, *, token: str) -> list[dict]:
        return copy.deepcopy(list(self.releases.values()))

    def request(
        self, method: str, path: str, raw_body: bytes | None, token: str
    ) -> dict:
        body = json.loads(raw_body) if raw_body else {}
        self.calls.append((method, path, body))
        if method == "GET" and path.startswith(f"/repos/{REPOSITORY}/git/ref/"):
            return self._ref(path.replace(f"/repos/{REPOSITORY}/git/ref/", "refs/"))
        if method == "POST" and path == REFS:
            ref = body["ref"]
            if ref in self.refs:
                raise HTTPError(422)
            self.refs[ref] = body["sha"]
            return self._ref(ref)
        if method == "DELETE" and path.startswith(f"{REFS}/"):
            del self.refs[path.removeprefix(f"/repos/{REPOSITORY}/git/")]
            return {}
        if method == "POST" and path == RELEASES:
            release_id = 100 + len(self.releases)
            release = {
                **body,
                "id": release_id,
                "assets": [],
                "html_url": f"https://github.com/{REPOSITORY}/releases/tag/{body['tag_name']}",
            }
            self.releases[release_id] = release
            return copy.deepcopy(release)
        if method == "DELETE" and path.startswith(f"{RELEASES}/assets/"):
            asset_id = int(path.split("/")[-1])
            for release in self.releases.values():
                release["assets"] = [
                    a for a in release["assets"] if a["id"] != asset_id
                ]
            return {}
        release = self.releases[int(path.removeprefix(f"{RELEASES}/"))]
        if method == "PATCH":
            if self.publish_timeout != "before":
                release.update(body)
            if self.publish_timeout:
                raise TagError("连接中断")
        return copy.deepcopy(release)

    def _ref(self, name: str) -> dict:
        if name not in self.refs:
            raise HTTPError(404)
        return {"ref": name, "object": {"sha": self.refs[name], "type": "commit"}}

    def upload(
        self, method: str, url: str, handle: BinaryIO, token: str, *, asset_size: int
    ) -> dict:
        parsed = urllib.parse.urlparse(url)
        release_id = int(parsed.path.split("/")[-2])
        name = urllib.parse.parse_qs(parsed.query)["name"][0]
        assert parsed.netloc == "uploads.github.com" and method == "POST"
        assert self.refs[publisher.LOCK_REF] == MAIN_HEAD
        raw = handle.read()
        assert len(raw) == asset_size
        self.uploads.append((release_id, name))
        self.last_asset_id += 1
        release = self.releases[release_id]
        assert release["draft"] is True
        asset = {
            "id": self.last_asset_id,
            "name": name,
            "size": len(raw),
            "state": "uploaded",
            "digest": f"sha256:{hashlib.sha256(raw).hexdigest()}",
        }
        if self.interrupt_upload and len(self.uploads) == 2:
            asset.update(state="starter", size=0, digest=None)
            release["assets"].append(asset)
            raise HTTPError(502)
        if self.corrupt_upload:
            asset["digest"] = "sha256:" + "0" * 64
        release["assets"].append(asset)
        return copy.deepcopy(asset)


class PublishReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        work = Path(directory.name)
        self.candidate_dir = work / "candidate"
        self.candidate_dir.mkdir()
        self.report_path = work / "local-report.json"
        policy = load_policy(ROOT / "release/policy.toml")
        self.manifest = Manifest(
            policy,
            Upstream("openai/codex", "0.171.0", "rust-v0.171.0", "a" * 40, "b" * 40),
            Toolchain("1.95.0", policy.zig, "152.2.0"),
        )
        self.names = set(formal_release_asset_names(self.manifest))
        for name in self.names - {"candidate.json", "k3-report.json"}:
            (self.candidate_dir / name).write_text(name, encoding="utf-8")
        self.candidate = {
            "candidate_head_sha": COMMIT,
            "assets": {
                path.name: asset_record(path) for path in self.candidate_dir.iterdir()
            },
        }
        self.report = {"validation_target": "qemu-system-riscv64"}
        self.report_path.write_text(json.dumps(self.report), encoding="utf-8")
        self.fake = FakeGitHub()
        for target, replacement in (
            ("publish_release._token", lambda: "test-token"),
            ("publish_release._request", self.fake.request),
            ("publish_release._request_url", self.fake.upload),
            ("publish_release.github_releases", self.fake.list_releases),
            ("release_lib.github_releases", self.fake.list_releases),
        ):
            mock = patch(target, replacement)
            mock.start()
            self.addCleanup(mock.stop)

    def publish(self) -> dict:
        (self.candidate_dir / "candidate.json").write_text(
            json.dumps(self.candidate), encoding="utf-8"
        )
        return publisher.publish_release(
            self.manifest,
            self.candidate_dir,
            self.report_path,
            candidate=self.candidate,
            report=self.report,
        )

    def test_publish_checks_the_exact_assets_before_marking_release_latest(
        self,
    ) -> None:
        result = self.publish()
        self.assertTrue(result["published"])
        self.assertEqual(result["asset_count"], 18)
        self.assertEqual(set(result["assets"]), self.names)
        release = self.fake.releases[result["release_id"]]
        self.assertFalse(release["draft"])
        self.assertIn("维护者本地构建", release["body"])
        self.assertEqual(release["make_latest"], "true")
        self.assertEqual(
            self.fake.refs, {"refs/heads/main": MAIN_HEAD, f"refs/tags/{TAG}": COMMIT}
        )
        self.assertEqual(
            [
                body
                for method, path, body in self.fake.calls
                if (method, path) == ("POST", REFS)
            ],
            [
                {"ref": publisher.LOCK_REF, "sha": MAIN_HEAD},
                {"ref": f"refs/tags/{TAG}", "sha": COMMIT},
            ],
        )

    def test_actions_origin_and_older_release_do_not_advance_latest(self) -> None:
        self.candidate["candidate_run_id"] = "123"
        self.fake.releases[90] = {
            "tag_name": "riscv-v0.171.0-r2",
            "draft": False,
            "prerelease": False,
        }
        result = self.publish()
        release = self.fake.releases[result["release_id"]]
        self.assertIn(
            f"https://github.com/{REPOSITORY}/actions/runs/123", release["body"]
        )
        self.assertEqual(release["make_latest"], "false")

    def test_retry_removes_only_its_empty_placeholder_and_resumes_the_same_draft(
        self,
    ) -> None:
        self.fake.interrupt_upload = True
        with self.assertRaisesRegex(ReleaseError, "502"):
            self.publish()
        self.assertNotIn(publisher.LOCK_REF, self.fake.refs)
        ((release_id, first), (_, interrupted)) = self.fake.uploads
        self.assertTrue(self.fake.releases[release_id]["draft"])
        result = self.publish()
        self.assertEqual(result["release_id"], release_id)
        self.assertEqual(self.fake.uploads.count((release_id, first)), 1)
        self.assertEqual(self.fake.uploads.count((release_id, interrupted)), 2)
        deleted = [path for method, path, _ in self.fake.calls if method == "DELETE"]
        self.assertIn(f"{RELEASES}/assets/802", deleted)

    def test_resume_refuses_a_draft_with_different_bytes(self) -> None:
        self.fake.interrupt_upload = True
        with self.assertRaises(ReleaseError):
            self.publish()
        self.candidate["candidate_run_id"] = "456"
        with self.assertRaisesRegex(ReleaseError, "已有其他发布草稿"):
            self.publish()
        self.assertEqual(len(self.fake.uploads), 2)

    def test_retry_verifies_a_public_release_and_never_replaces_it(self) -> None:
        release_id = self.publish()["release_id"]
        self.fake.calls.clear()
        self.fake.uploads.clear()
        self.assertFalse(self.publish()["published"])
        self.assertFalse(self.fake.uploads)
        self.assertNotIn("PATCH", [method for method, _, _ in self.fake.calls])
        self.fake.releases[release_id]["assets"][0]["digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ReleaseError, "SHA-256"):
            self.publish()
        self.assertFalse(self.fake.uploads)

    def test_upload_digest_mismatch_keeps_release_as_draft(self) -> None:
        self.fake.corrupt_upload = True
        with self.assertRaisesRegex(ReleaseError, "SHA-256"):
            self.publish()
        self.assertTrue(self.fake.releases[100]["draft"])
        self.assertNotIn(publisher.LOCK_REF, self.fake.refs)

    def test_conflicting_tag_stops_before_release_creation(self) -> None:
        self.fake.refs[f"refs/tags/{TAG}"] = "d" * 40
        with self.assertRaisesRegex(ReleaseError, "does not resolve"):
            self.publish()
        self.assertFalse(self.fake.releases)

    def test_lock_requires_a_valid_remote_main_commit(self) -> None:
        del self.fake.refs["refs/heads/main"]
        with self.assertRaisesRegex(ReleaseError, "远端 main.*404"):
            self.publish()
        self.assertEqual([method for method, _, _ in self.fake.calls], ["GET", "GET"])

    def test_second_publisher_waits_until_the_remote_lock_is_free(self) -> None:
        self.fake.refs[publisher.LOCK_REF] = "d" * 40
        with patch(
            "publish_release.time.sleep",
            side_effect=lambda _: self.fake.refs.pop(publisher.LOCK_REF),
        ) as wait:
            self.assertTrue(self.publish()["published"])
        wait.assert_called_once()

    def test_publish_timeout_checks_the_result_before_releasing_the_lock(self) -> None:
        self.fake.publish_timeout = "after"
        self.assertTrue(self.publish()["published"])
        self.assertNotIn(publisher.LOCK_REF, self.fake.refs)

    def test_unconfirmed_publish_keeps_the_lock_for_recovery(self) -> None:
        self.fake.publish_timeout = "before"
        with self.assertRaisesRegex(ReleaseError, "无法确认.*发布结果"):
            self.publish()
        self.assertEqual(self.fake.refs[publisher.LOCK_REF], MAIN_HEAD)
        self.assertTrue(self.fake.releases[100]["draft"])

    def test_actions_cli_requires_its_own_preflight(self) -> None:
        args = [
            "publish_release.py",
            "--policy",
            str(self.manifest.path),
            "--release-lock",
            "test-lock.json",
            "--candidate-dir",
            str(self.candidate_dir),
            "--k3-report",
            str(self.report_path),
            "--run-id",
            "123",
        ]
        with (
            patch.object(sys, "argv", args),
            patch("publish_release.load_manifest", return_value=self.manifest),
            patch(
                "publish_release.preflight_publish",
                side_effect=ReleaseError("bad evidence"),
            ) as preflight,
            patch("publish_release.publish_release") as publish,
            patch("publish_release.sys.stderr"),
        ):
            self.assertEqual(publisher.main(), 2)
        preflight.assert_called_once_with(
            self.manifest, self.candidate_dir, self.report_path, expected_run_id="123"
        )
        publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
