#!/usr/bin/env python3
"""Ensure a release tag exists and resolves to the requested commit."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping, Sequence
API_ROOT = "https://api.github.com"
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$", re.ASCII)
TAG_RE = re.compile(r"^riscv-v[0-9]+\.[0-9]+\.[0-9]+-r[0-9]+$", re.ASCII)
SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.ASCII)
class TagError(RuntimeError):
    """A tag input, API, or tag graph invariant failed."""


class HTTPError(TagError):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"GitHub API returned HTTP {status}")
def _validate(repository: str, tag: str, commit: str) -> str:
    if not REPOSITORY_RE.fullmatch(repository):
        raise TagError("repository must be an ASCII owner/name")
    if not TAG_RE.fullmatch(tag):
        raise TagError("tag must match riscv-vX.Y.Z-rN")
    if not isinstance(commit, str) or SHA_RE.fullmatch(commit.lower()) is None:
        raise TagError("commit must be a 40-hex SHA")
    return commit.lower()
class GitHub:
    def __init__(
        self,
        token: str,
        repository: str,
        *,
        request: Callable[[str, str, bytes | None, str], Mapping[str, Any]] | None = None,
    ) -> None:
        self.token = token
        self.repository = repository
        self._request_impl = request

    def request(
        self, method: str, path: str, body: bytes | None = None
    ) -> Mapping[str, Any]:
        if self._request_impl is not None:
            return self._request_impl(method, path, body, self.token)
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "codex-riscv64-release-tag",
        }
        if body is not None:
            headers["Content-Type"] = "application/vnd.github+json"
        request = urllib.request.Request(
            API_ROOT + path, data=body, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                value = json.loads(response.read())
        except urllib.error.HTTPError as error:
            raise HTTPError(int(error.code)) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise TagError("GitHub API request failed") from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TagError("GitHub API returned invalid JSON") from error
        if not isinstance(value, dict):
            raise TagError("GitHub API returned a JSON value that is not an object")
        return value
def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value.lower()) is None:
        raise TagError(f"{label} is not a valid SHA")
    return value.lower()
def _object(value: Any) -> tuple[str, str]:
    if not isinstance(value, Mapping):
        raise TagError("tag ref has no valid object")
    return _sha(value.get("sha"), "tag object SHA"), value.get("type", "")
def resolve_commit(
    api: GitHub, tag: str, expected: str, ref: Mapping[str, Any] | None = None
) -> str:
    encoded = urllib.parse.quote(tag, safe="")
    ref_path = f"/repos/{api.repository}/git/ref/tags/{encoded}"
    ref = ref if ref is not None else api.request("GET", ref_path)
    if ref.get("ref") != f"refs/tags/{tag}":
        raise TagError("GitHub returned an unexpected tag ref")
    current, kind = _object(ref.get("object"))
    seen: set[str] = set()
    annotated = 0
    while True:
        if current in seen:
            raise TagError("annotated tag graph contains a cycle")
        seen.add(current)
        if kind == "commit":
            if current != expected:
                raise TagError("tag does not resolve to the requested commit")
            return current
        if kind != "tag":
            raise TagError("tag ref does not resolve through commit or annotated tag")
        annotated += 1
        if annotated > 5:
            raise TagError("annotated tag chain exceeds five levels")
        tag_object = api.request("GET", f"/repos/{api.repository}/git/tags/{current}")
        if _sha(tag_object.get("sha"), "annotated tag SHA") != current:
            raise TagError("annotated tag SHA does not match the requested object")
        current, kind = _object(tag_object.get("object"))
def ensure_tag(repository: str, tag: str, commit: str, api: GitHub) -> str:
    expected = _validate(repository, tag, commit)
    encoded = urllib.parse.quote(tag, safe="")
    ref_path = f"/repos/{repository}/git/ref/tags/{encoded}"
    try:
        ref = api.request("GET", ref_path)
    except HTTPError as error:
        if error.status != 404:
            raise
        body = json.dumps({"ref": f"refs/tags/{tag}", "sha": expected}).encode()
        try:
            api.request("POST", f"/repos/{repository}/git/refs", body)
        except HTTPError as create_error:
            if create_error.status != 422:
                raise
    else:
        return resolve_commit(api, tag, expected, ref=ref)
    return resolve_commit(api, tag, expected)
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args(argv)
    try:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            raise TagError("GH_TOKEN or GITHUB_TOKEN is required")
        repository = args.repository
        api = GitHub(token, repository)
        verified = ensure_tag(repository, args.tag, args.commit, api)
        print(f"verified {args.tag} -> {verified}")
        return 0
    except (TagError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
if __name__ == "__main__":
    raise SystemExit(main())
