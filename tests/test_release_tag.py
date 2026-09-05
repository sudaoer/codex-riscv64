from __future__ import annotations

import io
import json
import sys
import urllib.error
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import release_tag as tag  # noqa: E402


REPO = "sudaoer/codex-riscv64"
NAME = "riscv-v0.153.4-r1"
COMMIT = "a" * 40
TAG1 = "b" * 40
TAG2 = "c" * 40


def http404() -> tag.HTTPError:
    return tag.HTTPError(404)


class FakeAPI(tag.GitHub):
    def __init__(self, responses: list[object]) -> None:
        self.calls: list[tuple[str, str, bytes | None]] = []
        self.responses = list(responses)
        super().__init__("token", REPO, request=self.request_fake)

    def request_fake(
        self, method: str, path: str, body: bytes | None, token: str
    ) -> dict[str, object]:
        self.calls.append((method, path, body))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def ref(sha: str, kind: str = "commit") -> dict[str, object]:
    return {"ref": f"refs/tags/{NAME}", "object": {"sha": sha, "type": kind}}


class ReleaseTagTests(unittest.TestCase):
    def test_transport_builds_post_with_json_and_bounded_timeout(self) -> None:
        class Response:
            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return b"{}"

        body = (
            b'{"ref":"refs/tags/riscv-v0.153.4-r1","sha":"'
            + COMMIT.encode()
            + b'"}'
        )
        with patch(
            "release_tag.urllib.request.urlopen", return_value=Response()
        ) as opened:
            api = tag.GitHub("token", REPO)
            self.assertEqual(api.request("POST", "/repos/x/git/refs", body), {})
        (request,) = opened.call_args.args
        self.assertIsInstance(request, urllib.request.Request)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), json.loads(body))
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(request.get_header("Accept"), "application/vnd.github+json")
        self.assertEqual(opened.call_args.kwargs["timeout"], 30)

    def test_existing_lightweight_tag_is_verified_without_write(self) -> None:
        api = FakeAPI([ref(COMMIT)])
        self.assertEqual(tag.ensure_tag(REPO, NAME, COMMIT, api), COMMIT)
        self.assertEqual([call[0] for call in api.calls], ["GET"])

    def test_missing_tag_is_created_with_exact_commit_then_verified(self) -> None:
        api = FakeAPI([http404(), {}, ref(COMMIT)])
        self.assertEqual(tag.ensure_tag(REPO, NAME, COMMIT, api), COMMIT)
        self.assertEqual(api.calls[1][0], "POST")
        self.assertEqual(
            json.loads(api.calls[1][2] or b""),
            {"ref": f"refs/tags/{NAME}", "sha": COMMIT},
        )

    def test_existing_wrong_tag_is_rejected_without_write(self) -> None:
        api = FakeAPI([ref("d" * 40)])
        with self.assertRaisesRegex(tag.TagError, "does not resolve"):
            tag.ensure_tag(REPO, NAME, COMMIT, api)
        self.assertEqual(len(api.calls), 1)

    def test_annotated_tag_chain_resolves_and_checks_object_shas(self) -> None:
        first = ref(TAG1, "tag")
        second = {"sha": TAG1, "object": {"sha": TAG2, "type": "tag"}}
        third = {"sha": TAG2, "object": {"sha": COMMIT, "type": "commit"}}
        api = FakeAPI([first, second, third])
        self.assertEqual(tag.ensure_tag(REPO, NAME, COMMIT, api), COMMIT)
        self.assertEqual(api.calls[1][1], f"/repos/{REPO}/git/tags/{TAG1}")
        self.assertEqual(api.calls[2][1], f"/repos/{REPO}/git/tags/{TAG2}")

    def test_annotated_cycle_and_excessive_chain_are_rejected(self) -> None:
        api = FakeAPI(
            [
                ref(TAG1, "tag"),
                {"sha": TAG1, "object": {"sha": TAG1, "type": "tag"}},
            ]
        )
        with self.assertRaisesRegex(tag.TagError, "cycle"):
            tag.ensure_tag(REPO, NAME, COMMIT, api)

    def test_only_initial_404_creates_and_permissions_or_other_errors_do_not(self) -> None:
        for error in (tag.HTTPError(403), tag.HTTPError(500)):
            api = FakeAPI([error])
            with self.assertRaises(tag.HTTPError):
                tag.ensure_tag(REPO, NAME, COMMIT, api)
            self.assertEqual(len(api.calls), 1)

        api = FakeAPI([http404(), tag.HTTPError(403)])
        with self.assertRaises(tag.HTTPError):
            tag.ensure_tag(REPO, NAME, COMMIT, api)
        self.assertEqual([call[0] for call in api.calls], ["GET", "POST"])

    def test_422_create_race_rechecks_the_existing_ref(self) -> None:
        api = FakeAPI([http404(), tag.HTTPError(422), ref(COMMIT)])
        self.assertEqual(tag.ensure_tag(REPO, NAME, COMMIT, api), COMMIT)
        self.assertEqual([call[0] for call in api.calls], ["GET", "POST", "GET"])

    def test_malformed_inputs_and_unexpected_ref_are_rejected(self) -> None:
        for repository, name, commit in (
            ("owner/é", NAME, COMMIT),
            ("owner/repo/extra", NAME, COMMIT),
            (REPO, "v1.2.3", COMMIT),
            (REPO, NAME, "g" * 40),
        ):
            with self.subTest(repository=repository, name=name, commit=commit):
                with self.assertRaises(tag.TagError):
                    tag.ensure_tag(repository, name, commit, FakeAPI([]))

        wrong_ref = {"ref": "refs/tags/other", "object": {"sha": COMMIT, "type": "commit"}}
        api = FakeAPI([wrong_ref])
        with self.assertRaises(tag.TagError):
            tag.ensure_tag(REPO, NAME, COMMIT, api)


if __name__ == "__main__":
    unittest.main()
