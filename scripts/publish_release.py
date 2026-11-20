#!/usr/bin/env python3
"""将通过预检的 Candidate 发布到 GitHub。"""

import argparse
import hashlib
import http.client
import json
import os
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

from release_lib import (
    Manifest,
    ReleaseError,
    formal_release_asset_names,
    github_releases,
    load_manifest,
    preflight_publish,
    release_asset_map,
    should_make_latest,
)
from release_tag import (
    API_ROOT,
    REPOSITORY_RE,
    SHA_RE,
    GitHub,
    HTTPError,
    TagError,
    ensure_tag,
    resolve_commit,
)

LOCK_BRANCH = "heads/automation/stable-release-lock"
LOCK_REF = f"refs/{LOCK_BRANCH}"
MAIN_BRANCH = "heads/main"
LOCK_WAIT_SECONDS = 120
REPORT_NAME = "k3-report.json"


class UnconfirmedPublication(ReleaseError):
    """GitHub 可能已接收发布请求，保留锁供维护者核对。"""


def _token() -> str:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    try:
        token = subprocess.check_output(
            ["gh", "auth", "token", "--hostname", "github.com"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
        ).strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise ReleaseError("请先配置具有仓库写权限的 GitHub 凭据") from error
    if not token:
        raise ReleaseError("GitHub 凭据为空")
    return token


def _request_url(
    method: str,
    url: str,
    body: bytes | BinaryIO | None,
    token: str,
    *,
    asset_size: int | None = None,
) -> Mapping[str, Any]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "codex-riscv64-release-tools",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if asset_size is not None:
        headers["Content-Type"] = "application/octet-stream"
        headers["Content-Length"] = str(asset_size)
    elif body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    request.add_unredirected_header("Authorization", f"Bearer {token}")
    try:
        timeout = 120 if asset_size is not None else 30
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = {} if response.status == 204 else json.load(response)
    except urllib.error.HTTPError as error:
        error.close()
        raise HTTPError(error.code) from error
    except (OSError, http.client.HTTPException, ValueError) as error:
        raise TagError(f"GitHub 请求结果无法确认：{method} {url}") from error
    if not isinstance(value, dict):
        raise TagError(f"GitHub 响应格式无效：{method} {url}")
    return value


def _request(
    method: str, path: str, body: bytes | None, token: str
) -> Mapping[str, Any]:
    return _request_url(method, API_ROOT + path, body, token)


def _write(
    api: GitHub, method: str, path: str, body: dict[str, Any]
) -> Mapping[str, Any]:
    return api.request(method, path, json.dumps(body).encode())


def _lock_matches(ref: Mapping[str, Any], head_sha: str) -> bool:
    obj = ref.get("object")
    return (
        ref.get("ref") == LOCK_REF
        and isinstance(obj, Mapping)
        and obj.get("type") == "commit"
        and obj.get("sha") == head_sha
    )


def _read_ref(api: GitHub, path: str) -> Mapping[str, Any] | None:
    try:
        return api.request("GET", path)
    except HTTPError as error:
        if error.status != 404:
            raise
        return None


def _main_head(api: GitHub) -> str:
    path = f"/repos/{api.repository}/git/ref/{MAIN_BRANCH}"
    try:
        ref = api.request("GET", path)
    except TagError as error:
        raise ReleaseError(f"读取远端 main 提交失败：{error}") from error
    obj = ref.get("object")
    if ref.get("ref") != f"refs/{MAIN_BRANCH}" or not isinstance(obj, Mapping):
        raise ReleaseError("GitHub 返回的 main 分支引用无效")
    sha = obj.get("sha")
    if (
        obj.get("type") != "commit"
        or not isinstance(sha, str)
        or not SHA_RE.fullmatch(sha)
    ):
        raise ReleaseError("GitHub 返回的 main 分支提交无效")
    return sha


@contextmanager
def _publication_lock(api: GitHub) -> Iterator[None]:
    refs_path = f"/repos/{api.repository}/git/refs"
    read_path = f"/repos/{api.repository}/git/ref/{LOCK_BRANCH}"
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    contention_seen = False
    while True:
        try:
            if _read_ref(api, read_path) is None:
                lock_sha = _main_head(api)
                created = _write(
                    api, "POST", refs_path, {"ref": LOCK_REF, "sha": lock_sha}
                )
                break
            contention_seen = True
        except HTTPError as error:
            if error.status not in (409, 422):
                raise
            if _read_ref(api, read_path) is None and contention_seen:
                raise ReleaseError(
                    f"无法在远端 main 创建发布锁 {LOCK_REF}：{error}"
                ) from error
            contention_seen = True
        except TagError as error:
            raise ReleaseError(f"无法确认发布锁是否创建，请检查 {LOCK_REF}") from error
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ReleaseError(f"发布锁仍被占用：{LOCK_REF}")
        time.sleep(min(5, remaining))
    if not _lock_matches(created, lock_sha):
        raise ReleaseError(f"GitHub 返回的发布锁与提交不匹配：{LOCK_REF}")

    keep_lock = False
    try:
        yield
    except UnconfirmedPublication:
        keep_lock = True
        raise
    finally:
        if not keep_lock:
            try:
                current = _read_ref(api, read_path)
                if current is not None and not _lock_matches(current, lock_sha):
                    raise ReleaseError(f"发布锁所有权已变化：{LOCK_REF}")
                if current is not None:
                    api.request("DELETE", f"{refs_path}/{LOCK_BRANCH}")
            except TagError as error:
                raise ReleaseError(
                    f"无法确认发布锁已释放，请检查 {LOCK_REF}"
                ) from error


def _metadata_record(path: Path, expected: Mapping[str, Any]) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ReleaseError(f"发布文件必须是普通文件：{path}")
    raw = path.read_bytes()
    try:
        actual = json.loads(raw)
    except ValueError as error:
        raise ReleaseError(f"发布文件已不是有效的 JSON：{path}") from error
    if actual != expected:
        raise ReleaseError(f"发布文件在预检后发生变化：{path}")
    return {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}


def _id(value: Mapping[str, Any]) -> int:
    identifier = value.get("id")
    if type(identifier) is not int or identifier <= 0:
        raise ReleaseError("GitHub 响应缺少有效的 ID")
    return identifier


def _verify_release(release: Mapping[str, Any], tag: str, *, draft: bool) -> None:
    if (
        release.get("tag_name") != tag
        or release.get("draft") is not draft
        or release.get("prerelease") is not False
    ):
        raise ReleaseError(f"GitHub Release 的标签或发布状态不符：{tag}")


def _verify_asset(
    name: str, asset: Mapping[str, Any], expected: Mapping[str, Any]
) -> None:
    if (
        asset.get("state") != "uploaded"
        or asset.get("name") != name
        or type(asset.get("size")) is not int
        or asset["size"] != expected["size"]
        or asset.get("digest") != f"sha256:{expected['sha256']}"
    ):
        raise ReleaseError(
            f"GitHub Release 资产的上传状态、大小或 SHA-256 不符：{name}"
        )


def _verify_assets(
    release: Mapping[str, Any],
    expected: Mapping[str, Any],
    *,
    resume: bool = False,
) -> tuple[set[str], list[int]]:
    assets = release_asset_map(release)
    extra = set(assets) - set(expected)
    missing = set(expected) - set(assets)
    if extra or (missing and not resume):
        raise ReleaseError(
            f"GitHub Release 资产清单不符，缺失 {sorted(missing)}，多出 {sorted(extra)}"
        )
    placeholders = []
    for name, asset in assets.items():
        if (
            resume
            and asset.get("state") == "starter"
            and type(asset.get("size")) is int
            and asset.get("size") == 0
            and asset.get("digest") is None
        ):
            placeholders.append(_id(asset))
            missing.add(name)
        else:
            _verify_asset(name, asset, expected[name])
    return missing, placeholders


def _upload(api: GitHub, release_id: int, name: str, path: Path, size: int) -> None:
    url = f"https://uploads.github.com/repos/{api.repository}/releases/{release_id}/assets"
    url = f"{url}?{urllib.parse.urlencode({'name': name})}"
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != size:
            raise ReleaseError(f"发布资产在预检后发生变化：{path}")
        _request_url("POST", url, handle, api.token, asset_size=size)


def _result(
    release: Mapping[str, Any], expected: Mapping[str, Any], *, published: bool
) -> dict[str, Any]:
    _verify_assets(release, expected)
    url = release.get("html_url")
    if not isinstance(url, str) or not url:
        raise ReleaseError("GitHub 响应缺少 Release URL")
    return {
        "release_tag": release["tag_name"],
        "release_url": url,
        "release_id": _id(release),
        "published": published,
        "asset_count": len(expected),
        "assets": expected,
    }


def _publish_draft(
    api: GitHub, release_path: str, tag: str, *, make_latest: bool
) -> Mapping[str, Any]:
    try:
        return _write(
            api,
            "PATCH",
            release_path,
            {"draft": False, "make_latest": "true" if make_latest else "false"},
        )
    except (TagError, KeyboardInterrupt) as error:
        if isinstance(error, HTTPError) and error.status < 500:
            raise
        try:
            published = api.request("GET", release_path)
            _verify_release(published, tag, draft=False)
        except (TagError, ReleaseError):
            raise UnconfirmedPublication(
                f"无法确认 {tag} 的发布结果，请核对 Release 后处理 {LOCK_REF}"
            ) from error
        return published


def publish_release(
    manifest: Manifest,
    candidate_dir: Path,
    report_path: Path,
    *,
    candidate: Mapping[str, Any],
    report: Mapping[str, Any],
) -> dict[str, Any]:
    """发布已通过预检的输入，同一份草稿可继续上传缺失的文件。"""
    repository = manifest.distribution.repository
    tag = manifest.release_tag
    head_sha = candidate.get("candidate_head_sha")
    if REPOSITORY_RE.fullmatch(repository) is None:
        raise ReleaseError("发布仓库必须采用 OWNER/REPO 格式")
    if not isinstance(head_sha, str) or SHA_RE.fullmatch(head_sha) is None:
        raise ReleaseError("Candidate 的 Git 提交无效")
    expected = {
        **candidate["assets"],
        "candidate.json": _metadata_record(candidate_dir / "candidate.json", candidate),
        REPORT_NAME: _metadata_record(report_path, report),
    }
    if set(expected) != set(formal_release_asset_names(manifest)):
        raise ReleaseError("正式发布的文件清单与策略不符")
    fingerprint = hashlib.sha256(
        json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    marker = f"<!-- codex-riscv64 release-assets sha256:{fingerprint} -->"
    primary_paths = {name: candidate_dir / name for name in expected}
    primary_paths[REPORT_NAME] = report_path

    token = _token()
    api = GitHub(token, repository, request=_request)
    releases_path = f"/repos/{repository}/releases"
    try:
        with _publication_lock(api):
            same_tag = [
                item
                for item in github_releases(repository, token=token)
                if item.get("tag_name") == tag
            ]
            if len(same_tag) > 1:
                raise ReleaseError(f"同一标签对应多个 GitHub Release：{tag}")
            release = same_tag[0] if same_tag else None
            if release is not None and release.get("draft") is False:
                _verify_release(release, tag, draft=False)
                resolve_commit(api, tag, head_sha)
                return _result(release, expected, published=False)

            if release is not None:
                _verify_release(release, tag, draft=True)
                body = release.get("body")
                if not isinstance(body, str) or marker not in body.splitlines():
                    raise ReleaseError(f"同一标签已有其他发布草稿：{tag}")
            ensure_tag(repository, tag, head_sha, api)
            if release is None:
                run_id = candidate.get("candidate_run_id")
                source = (
                    f"GitHub Actions 构建 [运行 {run_id}](https://github.com/"
                    f"{repository}/actions/runs/{run_id})"
                    if run_id is not None
                    else "维护者本地构建"
                )
                target = report.get("validation_target", "native-k3")
                body = (
                    f"Linux/riscv64 的非官方 Codex 构建。\n\n"
                    f"来源为{source}，已通过 `{target}` 验证和发布预检。\n\n{marker}\n"
                )
                release = _write(
                    api,
                    "POST",
                    releases_path,
                    {
                        "tag_name": tag,
                        "name": tag,
                        "target_commitish": head_sha,
                        "body": body,
                        "draft": True,
                        "prerelease": False,
                        "make_latest": "false",
                    },
                )
                _verify_release(release, tag, draft=True)

            release_id = _id(release)
            release_path = f"{releases_path}/{release_id}"
            missing, placeholders = _verify_assets(release, expected, resume=True)
            for identifier in placeholders:
                api.request("DELETE", f"{releases_path}/assets/{identifier}")
            for name in sorted(missing):
                _upload(
                    api, release_id, name, primary_paths[name], expected[name]["size"]
                )

            draft = api.request("GET", release_path)
            _verify_release(draft, tag, draft=True)
            _verify_assets(draft, expected)
            resolve_commit(api, tag, head_sha)
            make_latest = should_make_latest(manifest, token=token)
            published = _publish_draft(api, release_path, tag, make_latest=make_latest)
            _verify_release(published, tag, draft=False)
            return _result(published, expected, published=True)
    except (TagError, OSError, ValueError) as error:
        raise ReleaseError(f"发布 {tag} 失败：{error}") from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--release-lock", type=Path, required=True)
    parser.add_argument("--candidate-dir", type=Path, required=True)
    parser.add_argument("--k3-report", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    try:
        manifest = load_manifest(args.policy, args.release_lock)
        candidate, report = preflight_publish(
            manifest, args.candidate_dir, args.k3_report, expected_run_id=args.run_id
        )
        result = publish_release(
            manifest,
            args.candidate_dir,
            args.k3_report,
            candidate=candidate,
            report=report,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (ReleaseError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
