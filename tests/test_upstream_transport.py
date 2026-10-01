from __future__ import annotations

import http.client
import sys
import unittest
from pathlib import Path
from unittest.mock import call, patch
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import upstream_watch as watch


def http_error(code: int) -> HTTPError:
    error = HTTPError("https://api.github.com/test", code, "failed", None, None)
    error.close()
    return error


def release_page(
    nodes: list[dict], more: bool = False, cursor: str | None = None
) -> dict:
    return {
        "data": {
            "repository": {
                "releases": {
                    "nodes": nodes,
                    "pageInfo": {"hasNextPage": more, "endCursor": cursor},
                }
            }
        }
    }


def release_node(tag: str, draft: bool = False, prerelease: bool = False) -> dict:
    return {"tagName": tag, "isDraft": draft, "isPrerelease": prerelease}


class ReleaseMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = watch.GitHub("unused", "example/repo")

    def test_cursor_pagination_fetches_complete_metadata_without_release_payloads(
        self,
    ) -> None:
        pages = [
            release_page(
                [
                    release_node("rust-v0.160.0"),
                    release_node("rust-v0.159.4", prerelease=True),
                ],
                True,
                "page-two",
            ),
            release_page(
                [
                    release_node("rust-v0.159.3"),
                    release_node("rust-v0.159.2", draft=True),
                ]
            ),
        ]
        with patch.object(self.client, "read_request", side_effect=pages) as request:
            releases = self.client.releases("openai/codex")
        self.assertEqual(
            releases,
            [
                {"tag_name": "rust-v0.160.0", "draft": False, "prerelease": False},
                {"tag_name": "rust-v0.159.4", "draft": False, "prerelease": True},
                {"tag_name": "rust-v0.159.3", "draft": False, "prerelease": False},
                {"tag_name": "rust-v0.159.2", "draft": True, "prerelease": False},
            ],
        )
        self.assertEqual(watch.stable_versions(releases), ["0.159.3", "0.160.0"])
        first, second = [item.args for item in request.call_args_list]
        self.assertEqual(first[0], "/graphql")
        self.assertEqual(first[2], "POST")
        self.assertEqual(
            first[1]["variables"], {"owner": "openai", "name": "codex", "cursor": None}
        )
        self.assertEqual(second[1]["variables"]["cursor"], "page-two")
        self.assertNotIn("assets", first[1]["query"])
        self.assertNotIn("body", first[1]["query"])
        self.assertNotIn("description", first[1]["query"])

    def test_graphql_errors_and_invalid_metadata_fail_closed(self) -> None:
        invalid = [
            {"errors": [{"message": "API rate limited"}]},
            {"data": {"repository": None}},
            release_page([{"tagName": "rust-v0.159.3"}]),
            release_page([release_node("rust-v0.159.3")])
            | {"errors": [{"message": "partial result"}]},
        ]
        for response in invalid:
            with (
                self.subTest(response=response),
                patch.object(
                    self.client, "read_request", return_value=response
                ) as request,
            ):
                with self.assertRaises(watch.ReleaseError):
                    self.client.releases("openai/codex")
                self.assertEqual(request.call_count, 1)

    def test_missing_or_repeated_next_cursor_stops_instead_of_looping(self) -> None:
        for cursor in (None, "", 123):
            with (
                self.subTest(cursor=cursor),
                patch.object(
                    self.client,
                    "read_request",
                    return_value=release_page([], True, cursor),
                ) as request,
            ):
                with self.assertRaises(watch.ReleaseError):
                    self.client.releases("openai/codex")
                self.assertEqual(request.call_count, 1)
        with patch.object(
            self.client, "read_request", return_value=release_page([], True, "same")
        ) as request:
            with self.assertRaises(watch.ReleaseError):
                self.client.releases("openai/codex")
            self.assertEqual(request.call_count, 2)


class ReadRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = watch.GitHub("unused", "example/repo")

    def test_transient_read_errors_retry_and_return_complete_response(self) -> None:
        failures = [
            http.client.IncompleteRead(b"partial"),
            URLError("reset"),
            TimeoutError("timeout"),
            http_error(429),
            http_error(500),
        ]
        for error in failures:
            with (
                self.subTest(error=error),
                patch.object(
                    self.client, "request", side_effect=[error, {"complete": True}]
                ) as request,
                patch("upstream_watch.time.sleep") as sleep,
            ):
                self.assertEqual(self.client.read_request("/test"), {"complete": True})
                self.assertEqual(
                    request.call_args_list, [call("/test", None, "GET")] * 2
                )
                sleep.assert_called_once_with(1)

    def test_persistent_truncation_stops_after_three_attempts(self) -> None:
        with (
            patch.object(
                self.client,
                "request",
                side_effect=http.client.IncompleteRead(b"partial"),
            ) as request,
            patch("upstream_watch.time.sleep") as sleep,
        ):
            with self.assertRaises(http.client.IncompleteRead):
                self.client.read_request("/test")
        self.assertEqual(request.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(1), call(2)])

    def test_forbidden_response_is_not_retried(self) -> None:
        with (
            patch.object(
                self.client, "request", side_effect=http_error(403)
            ) as request,
            patch("upstream_watch.time.sleep") as sleep,
        ):
            with self.assertRaises(HTTPError):
                self.client.read_request("/test")
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()

    def test_graphql_read_query_post_may_retry(self) -> None:
        query = {"query": "query { viewer { login } }"}
        with (
            patch.object(
                self.client,
                "request",
                side_effect=[http.client.IncompleteRead(b"partial"), {}],
            ) as request,
            patch("upstream_watch.time.sleep"),
        ):
            self.assertEqual(self.client.read_request("/graphql", query, "POST"), {})
        self.assertEqual(request.call_args_list, [call("/graphql", query, "POST")] * 2)

    def test_retry_helper_rejects_mutations_before_network_request(self) -> None:
        for endpoint, payload in (
            ("/graphql", {"query": "mutation { example }"}),
            ("/repos/example/repo/actions/workflows/test/dispatches", {}),
        ):
            with (
                self.subTest(endpoint=endpoint),
                patch.object(self.client, "request") as request,
            ):
                with self.assertRaises(ValueError):
                    self.client.read_request(endpoint, payload, "POST")
                request.assert_not_called()

    def test_save_and_dispatch_do_not_retry_uncertain_mutation_response(self) -> None:
        for operation in ("save", "dispatch"):
            with (
                self.subTest(operation=operation),
                patch(
                    "upstream_watch.urllib.request.urlopen",
                    side_effect=http.client.IncompleteRead(b"partial"),
                ) as open_url,
                patch("upstream_watch.time.sleep") as sleep,
            ):
                with self.assertRaises(http.client.IncompleteRead):
                    if operation == "save":
                        self.client.save({"schema_version": 1, "tasks": {}})
                    else:
                        self.client.dispatch(
                            "task", {"release_lock": {}, "source_sha": "a" * 40}
                        )
                self.assertEqual(open_url.call_count, 1)
                sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
