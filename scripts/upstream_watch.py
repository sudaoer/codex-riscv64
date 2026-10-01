#!/usr/bin/env python3
"""Discover stable upstream releases and reconcile persistent build tasks."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from release_lib import TAG_RE, ReleaseError, load_policy, resolve_stable_manifest

ROOT = Path(__file__).resolve().parents[1]
STATE_BRANCH = "automation/upstream-state"
MINIMUM_VERSION = (0, 159, 3)
MAX_ACTIVE = 2
GRACE_SECONDS = 600
WORKFLOWS = (
    "compat-check.yml",
    "v8-build.yml",
    "candidate-build.yml",
    "qemu-validate.yml",
    "publish.yml",
)


def timestamp() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def age(value: str, now: str) -> float:
    return (
        dt.datetime.fromisoformat(now) - dt.datetime.fromisoformat(value)
    ).total_seconds()


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


class GitHub:
    def __init__(self, token: str, repository: str) -> None:
        self.token = token
        self.repository = repository
        self.head: str | None = None
        self.saved_state: str | None = None

    def request(self, endpoint: str, data: Any = None, method: str = "GET") -> Any:
        raw = None if data is None else json.dumps(data).encode()
        request = urllib.request.Request(
            f"https://api.github.com{endpoint}",
            data=raw,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "codex-riscv64-upstream-watcher",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            content = response.read()
        return json.loads(content) if content else None

    def pages(self, endpoint: str, key: str | None = None) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        separator = "&" if "?" in endpoint else "?"
        page = 1
        while True:
            result = self.read_request(f"{endpoint}{separator}per_page=100&page={page}")
            items = result[key] if key else result
            if not isinstance(items, list) or any(
                not isinstance(x, dict) for x in items
            ):
                raise ReleaseError("GitHub returned an invalid paginated list")
            values.extend(items)
            if len(items) < 100:
                return values
            page += 1

    def read_request(self, endpoint: str, data: Any = None, method: str = "GET") -> Any:
        if method != "GET" and not (
            endpoint == "/graphql"
            and method == "POST"
            and isinstance(data, dict)
            and data.get("query", "").lstrip().startswith("query ")
        ):
            raise ValueError("only read requests may be retried")
        for attempt in range(3):
            try:
                return self.request(endpoint, data, method)
            except urllib.error.HTTPError as error:
                if error.code != 429 and not 500 <= error.code < 600:
                    raise
                if attempt == 2:
                    raise
            except (
                http.client.IncompleteRead,
                urllib.error.URLError,
                TimeoutError,
                ConnectionError,
                json.JSONDecodeError,
            ):
                if attempt == 2:
                    raise
            time.sleep(attempt + 1)
        raise AssertionError("read retry attempts exhausted")

    def releases(self, repository: str) -> list[dict[str, Any]]:
        owner, name = repository.split("/")
        query = """query ($owner: String!, $name: String!, $cursor: String) {
          repository(owner: $owner, name: $name) {
            releases(first: 100, after: $cursor,
                     orderBy: {field: CREATED_AT, direction: DESC}) {
              nodes { tagName isDraft isPrerelease }
              pageInfo { hasNextPage endCursor }
            }
          }
        }"""
        cursor = None
        seen = set()
        releases = []
        while True:
            result = self.read_request(
                "/graphql",
                {
                    "query": query,
                    "variables": {"owner": owner, "name": name, "cursor": cursor},
                },
                "POST",
            )
            if not isinstance(result, dict) or result.get("errors"):
                raise ReleaseError("GitHub release metadata query failed")
            try:
                connection = result["data"]["repository"]["releases"]
                nodes = connection["nodes"]
                info = connection["pageInfo"]
            except (KeyError, TypeError) as error:
                raise ReleaseError(
                    "invalid GitHub release metadata response"
                ) from error
            if not isinstance(nodes, list) or not isinstance(info, dict):
                raise ReleaseError("invalid GitHub release metadata page")
            for node in nodes:
                if (
                    not isinstance(node, dict)
                    or not isinstance(node.get("tagName"), str)
                    or not isinstance(node.get("isDraft"), bool)
                    or not isinstance(node.get("isPrerelease"), bool)
                ):
                    raise ReleaseError("invalid GitHub release metadata node")
                releases.append(
                    {
                        "tag_name": node["tagName"],
                        "draft": node["isDraft"],
                        "prerelease": node["isPrerelease"],
                    }
                )
            if not isinstance(info.get("hasNextPage"), bool):
                raise ReleaseError("invalid GitHub release metadata pagination")
            if not info["hasNextPage"]:
                return releases
            cursor = info.get("endCursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise ReleaseError("invalid GitHub release metadata cursor")
            seen.add(cursor)

    def load(self, now: str) -> dict[str, Any]:
        prefix = f"/repos/{self.repository}"
        try:
            reference = self.read_request(f"{prefix}/git/ref/heads/{STATE_BRANCH}")
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            return {"schema_version": 1, "started_at": now, "tasks": {}}
        self.head = reference["object"]["sha"]
        value = self.read_request(f"{prefix}/contents/state.json?ref={self.head}")
        state = json.loads(base64.b64decode(value["content"]))
        if state.get("schema_version") != 1 or not isinstance(state.get("tasks"), dict):
            raise ReleaseError("unsupported upstream watcher state")
        self.saved_state = json.dumps(state, sort_keys=True, indent=2) + "\n"
        return state

    def save(self, state: dict[str, Any]) -> None:
        content = json.dumps(state, sort_keys=True, indent=2) + "\n"
        if content == self.saved_state:
            return
        prefix = f"/repos/{self.repository}"
        tree = self.request(
            f"{prefix}/git/trees",
            {
                "tree": [
                    {
                        "path": "state.json",
                        "mode": "100644",
                        "type": "blob",
                        "content": content,
                    }
                ],
            },
            "POST",
        )
        commit = self.request(
            f"{prefix}/git/commits",
            {
                "message": "Update upstream release tasks",
                "tree": tree["sha"],
                "parents": [self.head] if self.head else [],
            },
            "POST",
        )
        if self.head:
            self.request(
                f"{prefix}/git/refs/heads/{STATE_BRANCH}",
                {
                    "sha": commit["sha"],
                    "force": False,
                },
                "PATCH",
            )
        else:
            self.request(
                f"{prefix}/git/refs",
                {
                    "ref": f"refs/heads/{STATE_BRANCH}",
                    "sha": commit["sha"],
                },
                "POST",
            )
        self.head = commit["sha"]
        self.saved_state = content

    def dispatch(self, task_id: str, task: dict[str, Any]) -> None:
        lock = base64.b64encode(json.dumps(task["release_lock"]).encode()).decode()
        self.request(
            f"/repos/{self.repository}/actions/workflows/compat-check.yml/dispatches",
            {
                "ref": "main",
                "inputs": {
                    "continue_chain": "true",
                    "task_id": task_id,
                    "release_lock_b64": lock,
                    "downstream_source_sha": task["source_sha"],
                },
            },
            "POST",
        )


def stable_versions(releases: list[dict[str, Any]]) -> list[str]:
    versions = set()
    for release in releases:
        match = TAG_RE.fullmatch(release.get("tag_name", ""))
        if match and not release.get("draft") and not release.get("prerelease"):
            version = match.group(1)
            if version_key(version) >= MINIMUM_VERSION:
                versions.add(version)
    return sorted(versions, key=version_key)


def formal_tags(releases: list[dict[str, Any]]) -> set[str]:
    return {
        release["tag_name"]
        for release in releases
        if not release.get("draft")
        and not release.get("prerelease")
        and re.fullmatch(
            r"riscv-v[0-9]+\.[0-9]+\.[0-9]+-r[0-9]+", release.get("tag_name", "")
        )
    }


def task_identity(lock: dict[str, Any], source_sha: str) -> str:
    value = json.dumps(
        {"release_lock": lock, "source_sha": source_sha},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(value).hexdigest()


def reconcile(
    task: dict[str, Any], runs: list[dict[str, Any]], published: set[str], now: str
) -> None:
    if task["release_tag"] in published:
        task.update(status="published", reason="formal release exists")
        return
    remembered = task.setdefault("runs", {})
    for run in runs:
        if task.get("dispatched_at"):
            cutoff = (
                dt.datetime.fromisoformat(task["dispatched_at"])
                .replace(microsecond=0)
                .isoformat()
            )
            observed_at = run.get("created_at") or run["updated_at"]
            if int(run.get("run_attempt", 1)) > 1:
                observed_at = run["updated_at"]
            if age(cutoff, observed_at) < 0:
                continue
        remembered[str(run["id"])] = {
            key: run.get(key)
            for key in (
                "id",
                "path",
                "status",
                "conclusion",
                "run_attempt",
                "updated_at",
            )
        }
    latest = list(remembered.values())
    if any(run["status"] != "completed" for run in latest):
        task.update(status="running", reason="workflow is active")
        return
    if latest:
        last = max(latest, key=lambda x: (x["updated_at"] or "", x["id"]))
        if last["conclusion"] != "success":
            task.update(
                status="failed", reason=f"workflow {last['id']}: {last['conclusion']}"
            )
        elif age(last["updated_at"], now) >= GRACE_SECONDS:
            task.update(
                status="failed", reason="next stage or formal release did not appear"
            )
        else:
            task.update(status="running", reason="waiting for next stage")
    elif (
        task["status"] == "dispatching"
        and age(task["dispatched_at"], now) >= GRACE_SECONDS
    ):
        task.update(status="failed", reason="dispatch could not be confirmed")


def schedule(
    client: GitHub,
    state: dict[str, Any],
    versions: list[str],
    published: set[str],
    runs: list[dict[str, Any]],
    source_sha: str,
    resolve: Any,
    now: str,
    retry_version: str = "",
) -> None:
    tasks = state["tasks"]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        title = run.get("display_title", "")
        if title.startswith("upstream:"):
            grouped.setdefault(title.removeprefix("upstream:"), []).append(run)
    for task_id, task in tasks.items():
        reconcile(task, grouped.get(task_id, []), published, now)
        if task["status"] == "queued" and task["source_sha"] != source_sha:
            task.update(status="superseded", reason="downstream inputs changed")
    for version in versions:
        release_tag = f"riscv-v{version}-r{resolve.revision}"
        if release_tag in published:
            continue
        if any(
            task["release_tag"] == release_tag
            and task["status"] in {"running", "dispatching"}
            for task in tasks.values()
        ):
            continue
        existing = next(
            (
                (key, task)
                for key, task in tasks.items()
                if task["release_tag"] == release_tag
                and task["source_sha"] == source_sha
            ),
            None,
        )
        if existing:
            task_id, task = existing
            if retry_version == version and task["status"] == "failed":
                if "release_lock" in task:
                    task.update(status="queued", reason="manual retry", runs={})
                    continue
                del tasks[task_id]
            else:
                continue
        try:
            lock = resolve(version)
        except (ReleaseError, OSError, ValueError) as error:
            unresolved = {"upstream_tag": f"rust-v{version}", "source_sha": source_sha}
            task_id = hashlib.sha256(
                json.dumps(unresolved, sort_keys=True).encode()
            ).hexdigest()
            tasks[task_id] = {
                "version": version,
                "release_tag": release_tag,
                "source_sha": source_sha,
                "status": "failed",
                "created_at": now,
                "runs": {},
                "reason": f"release resolution failed: {error}",
            }
            continue
        task_id = task_identity(lock, source_sha)
        tasks[task_id] = {
            "version": version,
            "release_tag": release_tag,
            "source_sha": source_sha,
            "release_lock": lock,
            "status": "queued",
            "created_at": now,
            "runs": {},
        }
    client.save(state)
    legacy = [
        run
        for run in runs
        if not run.get("display_title", "").startswith("upstream:")
        and run.get("event") == "workflow_dispatch"
        and run.get("actor", {}).get("login") == "github-actions[bot]"
    ]
    if any(
        run["status"] != "completed" or age(run["updated_at"], now) < GRACE_SECONDS
        for run in legacy
    ):
        return
    active = sum(
        task["status"] in {"running", "dispatching"} for task in tasks.values()
    )
    queued = sorted(
        ((key, task) for key, task in tasks.items() if task["status"] == "queued"),
        key=lambda item: version_key(item[1]["version"]),
    )
    for task_id, task in queued:
        if active >= MAX_ACTIVE:
            break
        task.update(
            status="dispatching", dispatched_at=now, reason="dispatch requested"
        )
        client.save(state)
        try:
            client.dispatch(task_id, task)
        except (OSError, ValueError) as error:
            task["reason"] = f"dispatch response uncertain: {type(error).__name__}"
            client.save(state)
        active += 1


class Resolver:
    def __init__(self, token: str) -> None:
        self.policy = load_policy(ROOT / "release/policy.toml")
        self.token = token
        self.revision = self.policy.distribution.revision

    def __call__(self, version: str) -> dict[str, Any]:
        return resolve_stable_manifest(
            self.policy, f"rust-v{version}", token=self.token
        ).release_lock()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retry-version", default="")
    args = parser.parse_args()
    if args.retry_version and not re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+", args.retry_version
    ):
        parser.error("retry-version must be X.Y.Z")
    repository = os.environ["GITHUB_REPOSITORY"]
    source_sha = os.environ["GITHUB_SHA"]
    token = os.environ.get("GH_TOKEN") or os.environ["GITHUB_TOKEN"]
    resolver = Resolver(token)
    if repository != resolver.policy.distribution.repository:
        raise ReleaseError("watcher repository does not match release policy")
    client = GitHub(token, repository)
    now = timestamp()
    state = client.load(now)
    upstream = client.releases(resolver.policy.upstream_repository)
    published = formal_tags(client.pages(f"/repos/{repository}/releases"))
    since = dt.datetime.fromisoformat(now) - dt.timedelta(days=14)
    created = urllib.parse.quote(">=" + since.isoformat(), safe="")
    runs = client.pages(
        f"/repos/{repository}/actions/runs?branch=main&created={created}",
        "workflow_runs",
    )
    runs = [run for run in runs if run.get("path", "").split("/")[-1] in WORKFLOWS]
    present = {str(run["id"]) for run in runs}
    for task_id, task in state["tasks"].items():
        if (
            task["status"] in {"published", "superseded"}
            or task["release_tag"] in published
        ):
            continue
        for run_id, remembered in list(task.get("runs", {}).items()):
            if run_id in present:
                continue
            try:
                run = client.read_request(f"/repos/{repository}/actions/runs/{run_id}")
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
                if remembered["status"] != "completed":
                    remembered.update(
                        status="completed", conclusion="failure", updated_at=now
                    )
                continue
            run["display_title"] = f"upstream:{task_id}"
            runs.append(run)
    schedule(
        client,
        state,
        stable_versions(upstream),
        published,
        runs,
        source_sha,
        resolver,
        now,
        args.retry_version,
    )
    client.save(state)
    summary = "\n".join(
        f"- {task['release_tag']}: {task['status']} ({task.get('reason', '')})"
        for task in state["tasks"].values()
    )
    print(summary or "All monitored stable releases are published.")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write("## Upstream release tasks\n\n" + summary + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
