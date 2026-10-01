from __future__ import annotations

import base64
import copy
import io
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import upstream_watch as watch

NOW = "2026-10-01T00:00:00+00:00"
LATER = "2026-10-01T00:10:00+00:00"
SOURCE = "a" * 40


class FakeResolver:
    revision = 1

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, version: str) -> dict:
        self.calls.append(version)
        return {"upstream": {"version": version, "commit_sha": "b" * 40}}


class FakeGitHub:
    def __init__(self, fail_dispatch: bool = False) -> None:
        self.saved: list[dict] = []
        self.dispatched: list[tuple[str, dict]] = []
        self.fail_dispatch = fail_dispatch

    def save(self, state: dict) -> None:
        self.saved.append(copy.deepcopy(state))

    def dispatch(self, task_id: str, task: dict) -> None:
        # Every request must already have a durable identity and dispatch marker.
        persisted = self.saved[-1]["tasks"][task_id]
        if persisted["status"] != "dispatching":
            raise AssertionError("task was not persisted before dispatch")
        self.dispatched.append((task_id, copy.deepcopy(task)))
        if self.fail_dispatch:
            raise URLError("response lost")


def state() -> dict:
    return {"schema_version": 1, "started_at": NOW, "tasks": {}}


def task(
    version: str = "0.159.3", status: str = "dispatching", source: str = SOURCE
) -> tuple[str, dict]:
    lock = FakeResolver()(version)
    identity = watch.task_identity(lock, source)
    return identity, {
        "version": version,
        "release_tag": f"riscv-v{version}-r1",
        "source_sha": source,
        "release_lock": lock,
        "status": status,
        "created_at": NOW,
        "dispatched_at": NOW,
        "runs": {},
    }


def run(
    task_id: str,
    run_id: int = 1,
    workflow: str = "compat-check.yml",
    status: str = "completed",
    conclusion: str | None = "success",
    updated_at: str = NOW,
) -> dict:
    return {
        "id": run_id,
        "display_title": f"upstream:{task_id}",
        "path": f".github/workflows/{workflow}",
        "status": status,
        "conclusion": conclusion,
        "run_attempt": 1,
        "updated_at": updated_at,
        "created_at": NOW,
        "event": "workflow_dispatch",
        "actor": {"login": "github-actions[bot]"},
    }


class DiscoveryTests(unittest.TestCase):
    def test_stable_versions_are_filtered_deduplicated_and_numerically_sorted(
        self,
    ) -> None:
        releases = [
            {"tag_name": tag}
            for tag in (
                "rust-v0.160.0",
                "rust-v0.159.10",
                "rust-v0.159.3",
                "rust-v0.159.2",
                "rust-v0.159.3",
                "rust-v0.159.4-alpha.1",
                "v0.159.4",
                "rust-v1.0.0",
            )
        ]
        releases.extend(
            [
                {"tag_name": "rust-v0.159.4", "draft": True},
                {"tag_name": "rust-v0.159.5", "prerelease": True},
            ]
        )
        self.assertEqual(
            watch.stable_versions(releases), ["0.159.3", "0.159.10", "0.160.0", "1.0.0"]
        )

    def test_formal_tags_ignore_drafts_prereleases_and_other_assets(self) -> None:
        releases = [
            {"tag_name": "riscv-v0.159.3-r1"},
            {"tag_name": "riscv-v0.159.4-r2", "draft": True},
            {"tag_name": "riscv-v0.159.5-r1", "prerelease": True},
            {"tag_name": "riscv-v8-build-123"},
            {"tag_name": "riscv-v0.159.3"},
        ]
        self.assertEqual(watch.formal_tags(releases), {"riscv-v0.159.3-r1"})

    def test_pagination_finds_releases_beyond_first_page(self) -> None:
        client = watch.GitHub("unused", "example/repo")
        page_one = [{"tag_name": f"rust-v0.159.{number}"} for number in range(3, 103)]
        with patch.object(
            client, "request", side_effect=[page_one, [{"tag_name": "rust-v0.160.0"}]]
        ) as request:
            releases = client.pages("/repos/openai/codex/releases")
        self.assertEqual(len(releases), 101)
        self.assertIn("0.160.0", watch.stable_versions(releases))
        self.assertEqual(
            request.call_args_list[1].args[0],
            "/repos/openai/codex/releases?per_page=100&page=2",
        )

    def test_run_pagination_preserves_existing_query(self) -> None:
        client = watch.GitHub("unused", "example/repo")
        with patch.object(
            client, "request", return_value={"workflow_runs": []}
        ) as request:
            self.assertEqual(
                client.pages("/actions/runs?branch=main", "workflow_runs"), []
            )
        request.assert_called_once_with(
            "/actions/runs?branch=main&per_page=100&page=1", None, "GET"
        )

    def test_task_identity_tracks_lock_and_source_but_ignores_mapping_order(
        self,
    ) -> None:
        original = watch.task_identity({"version": "0.159.3", "commit": "a"}, SOURCE)
        self.assertEqual(
            original, watch.task_identity({"commit": "a", "version": "0.159.3"}, SOURCE)
        )
        self.assertNotEqual(
            original, watch.task_identity({"version": "0.159.3", "commit": "b"}, SOURCE)
        )
        self.assertNotEqual(
            original,
            watch.task_identity({"version": "0.159.3", "commit": "a"}, "c" * 40),
        )

    def test_dispatch_carries_exact_lock_task_and_source(self) -> None:
        client = watch.GitHub("unused", "example/repo")
        task_id, record = task()
        with patch.object(client, "request") as request:
            client.dispatch(task_id, record)
        endpoint, payload, method = request.call_args.args
        self.assertEqual(
            endpoint,
            "/repos/example/repo/actions/workflows/compat-check.yml/dispatches",
        )
        self.assertEqual(method, "POST")
        self.assertEqual(payload["ref"], "main")
        inputs = payload["inputs"]
        self.assertEqual(inputs["task_id"], task_id)
        self.assertEqual(inputs["downstream_source_sha"], SOURCE)
        self.assertEqual(
            json.loads(base64.b64decode(inputs["release_lock_b64"])),
            record["release_lock"],
        )


class SchedulingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeGitHub()
        self.state = state()
        self.resolver = FakeResolver()

    def schedule(
        self,
        versions: list[str] | None = None,
        runs: list[dict] | None = None,
        published: set[str] | None = None,
        source: str = SOURCE,
        now: str = NOW,
        retry: str = "",
    ) -> None:
        watch.schedule(
            self.client,
            self.state,
            versions or ["0.159.3"],
            published or set(),
            runs or [],
            source,
            self.resolver,
            now,
            retry,
        )

    def test_capacity_two_dispatches_oldest_versions_and_preserves_remaining_queue(
        self,
    ) -> None:
        self.schedule(["0.160.0", "0.159.10", "0.159.3"])
        self.assertEqual(
            [record["version"] for _, record in self.client.dispatched],
            ["0.159.3", "0.159.10"],
        )
        queued = [
            record["version"]
            for record in self.state["tasks"].values()
            if record["status"] == "queued"
        ]
        self.assertEqual(queued, ["0.160.0"])

    def test_active_candidate_prevents_duplicate_even_when_source_changes(self) -> None:
        task_id, record = task()
        self.state["tasks"][task_id] = record
        self.schedule(
            runs=[
                run(
                    task_id,
                    workflow="candidate-build.yml",
                    status="in_progress",
                    conclusion=None,
                )
            ],
            source="c" * 40,
        )
        self.assertEqual(len(self.state["tasks"]), 1)
        self.assertEqual(record["status"], "running")
        self.assertEqual(self.client.dispatched, [])
        self.assertEqual(self.resolver.calls, [])

    def test_published_version_is_skipped_and_prior_task_marked_published(self) -> None:
        task_id, record = task()
        self.state["tasks"][task_id] = record
        self.schedule(published={"riscv-v0.159.3-r1"})
        self.assertEqual(record["status"], "published")
        self.assertEqual(self.client.dispatched, [])
        self.assertEqual(self.resolver.calls, [])

    def test_failed_same_inputs_remain_paused_across_polls(self) -> None:
        task_id, record = task(status="failed")
        self.state["tasks"][task_id] = record
        self.schedule()
        self.schedule(now=LATER)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(self.client.dispatched, [])

    def test_changed_source_retries_failed_version_and_retains_failure(self) -> None:
        task_id, record = task(status="failed")
        self.state["tasks"][task_id] = record
        self.schedule(source="c" * 40)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(len(self.state["tasks"]), 2)
        self.assertEqual(len(self.client.dispatched), 1)
        self.assertEqual(self.client.dispatched[0][1]["source_sha"], "c" * 40)

    def test_manual_retry_resets_failed_run_history_and_uses_same_identity(
        self,
    ) -> None:
        task_id, record = task(status="failed")
        record["runs"] = {"1": run(task_id, conclusion="failure")}
        self.state["tasks"][task_id] = record
        self.schedule(retry="0.159.3")
        self.assertEqual(
            [identity for identity, _ in self.client.dispatched], [task_id]
        )
        self.assertEqual(record["runs"], {})
        self.assertEqual(record["status"], "dispatching")

    def test_manual_retry_does_not_reingest_runs_from_previous_failed_attempt(
        self,
    ) -> None:
        task_id, record = task(status="failed")
        previous_run = run(task_id, conclusion="failure")
        record["runs"] = {"1": previous_run}
        self.state["tasks"][task_id] = record
        self.schedule(runs=[previous_run], now=LATER, retry="0.159.3")
        self.schedule(runs=[previous_run], now="2026-10-01T00:10:01+00:00")
        self.assertEqual(record["status"], "dispatching")
        self.assertEqual(len(self.client.dispatched), 1)

    def test_failed_old_version_does_not_block_next_version(self) -> None:
        task_id, record = task(status="failed")
        self.state["tasks"][task_id] = record
        self.schedule(["0.159.3", "0.159.4"])
        self.assertEqual(
            [record["version"] for _, record in self.client.dispatched], ["0.159.4"]
        )

    def test_resolution_failure_is_recorded_without_blocking_next_version(self) -> None:
        class PartiallyUnavailableResolver(FakeResolver):
            def __call__(self, version: str) -> dict:
                if version == "0.159.3":
                    raise watch.ReleaseError("upstream toolchain is unavailable")
                return super().__call__(version)

        self.resolver = PartiallyUnavailableResolver()
        self.schedule(["0.159.3", "0.159.4"])
        records = {record["version"]: record for record in self.state["tasks"].values()}
        self.assertEqual(records["0.159.3"]["status"], "failed")
        self.assertIn("unavailable", records["0.159.3"]["reason"])
        self.assertEqual(
            [record["version"] for _, record in self.client.dispatched], ["0.159.4"]
        )

    def test_queued_old_source_is_superseded_before_dispatch(self) -> None:
        task_id, record = task(status="queued")
        self.state["tasks"][task_id] = record
        self.schedule(source="c" * 40)
        self.assertEqual(record["status"], "superseded")
        self.assertEqual(len(self.client.dispatched), 1)
        self.assertEqual(self.client.dispatched[0][1]["source_sha"], "c" * 40)

    def test_uncertain_dispatch_is_saved_waited_and_never_automatically_resent(
        self,
    ) -> None:
        self.client.fail_dispatch = True
        self.schedule()
        record = next(iter(self.state["tasks"].values()))
        self.assertEqual(record["status"], "dispatching")
        self.assertIn(
            "uncertain",
            self.client.saved[-1]["tasks"][next(iter(self.state["tasks"]))]["reason"],
        )
        self.schedule(now="2026-10-01T00:09:59+00:00")
        self.assertEqual(record["status"], "dispatching")
        self.schedule(now=LATER)
        self.assertEqual(record["status"], "failed")
        self.schedule(now="2026-10-01T00:20:00+00:00")
        self.assertEqual(len(self.client.dispatched), 1)

    def test_successful_stage_waits_ten_minutes_then_records_broken_handoff(
        self,
    ) -> None:
        task_id, record = task()
        self.state["tasks"][task_id] = record
        self.schedule(runs=[run(task_id)], now="2026-10-01T00:09:59+00:00")
        self.assertEqual(record["status"], "running")
        self.schedule(now=LATER)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(self.client.dispatched, [])

    def test_next_active_stage_prevents_handoff_timeout(self) -> None:
        task_id, record = task()
        self.state["tasks"][task_id] = record
        self.schedule(
            runs=[
                run(task_id),
                run(
                    task_id,
                    run_id=2,
                    workflow="v8-build.yml",
                    status="queued",
                    conclusion=None,
                    updated_at=LATER,
                ),
            ],
            now=LATER,
        )
        self.assertEqual(record["status"], "running")
        self.assertEqual(set(record["runs"]), {"1", "2"})
        self.assertEqual(self.client.dispatched, [])

    def test_persisted_completed_failure_survives_missing_actions_history(self) -> None:
        task_id, record = task()
        self.state["tasks"][task_id] = record
        self.schedule(runs=[run(task_id, conclusion="failure")])
        restored = copy.deepcopy(self.client.saved[-1])
        watch.schedule(
            self.client, restored, ["0.159.3"], set(), [], SOURCE, self.resolver, LATER
        )
        self.assertEqual(restored["tasks"][task_id]["status"], "failed")
        self.assertEqual(
            restored["tasks"][task_id]["runs"]["1"]["conclusion"], "failure"
        )
        self.assertEqual(self.client.dispatched, [])

    def test_legacy_bot_chain_blocks_dispatch_until_completed_handoff_grace(
        self,
    ) -> None:
        legacy = run("old", status="in_progress", conclusion=None)
        legacy["display_title"] = "Compatibility check"
        self.schedule(runs=[legacy])
        self.assertEqual(self.client.dispatched, [])
        self.assertEqual(next(iter(self.state["tasks"].values()))["status"], "queued")
        legacy.update(status="completed", conclusion="success")
        self.schedule(runs=[legacy], now="2026-10-01T00:09:59+00:00")
        self.assertEqual(self.client.dispatched, [])
        self.schedule(runs=[legacy], now=LATER)
        self.assertEqual(len(self.client.dispatched), 1)


class RecoveryTests(unittest.TestCase):
    def test_main_refreshes_missing_active_run_by_id_and_retains_terminal_snapshot(
        self,
    ) -> None:
        task_id, record = task(status="running")
        record["runs"] = {
            "123": run(task_id, run_id=123, status="in_progress", conclusion=None)
        }
        persisted = state()
        persisted["tasks"][task_id] = record
        client = FakeGitHub()
        client.load = lambda now: persisted
        client.pages = lambda endpoint, key=None: []
        client.releases = lambda repository: []
        requested: list[str] = []

        def request(endpoint: str) -> dict:
            requested.append(endpoint)
            error = HTTPError(endpoint, 404, "expired", None, None)
            error.close()
            raise error

        client.request = request
        client.read_request = request
        resolver = FakeResolver()
        resolver.policy = SimpleNamespace(
            upstream_repository="openai/codex",
            distribution=SimpleNamespace(repository="example/repo"),
        )
        with (
            patch.object(watch, "GitHub", return_value=client),
            patch.object(watch, "Resolver", return_value=resolver),
            patch.object(watch, "timestamp", return_value=LATER),
            patch.dict(
                "os.environ",
                {
                    "GITHUB_REPOSITORY": "example/repo",
                    "GITHUB_SHA": SOURCE,
                    "GH_TOKEN": "unused",
                },
            ),
            patch.object(sys, "argv", ["upstream_watch.py"]),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(watch.main(), 0)
        self.assertEqual(requested, ["/repos/example/repo/actions/runs/123"])
        self.assertEqual(record["status"], "failed")
        self.assertEqual(
            client.saved[-1]["tasks"][task_id]["runs"]["123"]["conclusion"], "failure"
        )
        self.assertEqual(client.dispatched, [])


class StateBranchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = watch.GitHub("unused", "example/repo")
        self.prefix = "/repos/example/repo"

    def test_first_save_creates_orphan_commit_and_only_automation_branch(self) -> None:
        persisted = state()
        task_id, record = task(status="failed")
        persisted["tasks"][task_id] = record
        with patch.object(
            self.client,
            "request",
            side_effect=[
                {"sha": "new-tree"},
                {"sha": "new-commit"},
                None,
            ],
        ) as request:
            self.client.save(persisted)
        calls = request.call_args_list
        self.assertEqual(calls[0].args[0], self.prefix + "/git/trees")
        tree = calls[0].args[1]["tree"]
        self.assertEqual([entry["path"] for entry in tree], ["state.json"])
        self.assertEqual(json.loads(tree[0]["content"]), persisted)
        self.assertEqual(calls[1].args[0], self.prefix + "/git/commits")
        self.assertEqual(calls[1].args[1]["parents"], [])
        self.assertEqual(calls[1].args[1]["tree"], "new-tree")
        self.assertEqual(
            calls[2].args,
            (
                self.prefix + "/git/refs",
                {"ref": "refs/heads/automation/upstream-state", "sha": "new-commit"},
                "POST",
            ),
        )
        self.assertEqual(self.client.head, "new-commit")

    def test_subsequent_save_extends_loaded_commit_without_forcing_reference(
        self,
    ) -> None:
        self.client.head = "previous-commit"
        with patch.object(
            self.client,
            "request",
            side_effect=[
                {"sha": "new-tree"},
                {"sha": "new-commit"},
                None,
            ],
        ) as request:
            self.client.save(state())
        calls = request.call_args_list
        self.assertEqual(calls[1].args[1]["parents"], ["previous-commit"])
        self.assertEqual(
            calls[2].args,
            (
                self.prefix + "/git/refs/heads/automation/upstream-state",
                {"sha": "new-commit", "force": False},
                "PATCH",
            ),
        )
        self.assertEqual(self.client.head, "new-commit")

    def test_duplicate_save_does_not_create_another_commit(self) -> None:
        persisted = state()
        with patch.object(
            self.client,
            "request",
            side_effect=[
                {"sha": "new-tree"},
                {"sha": "new-commit"},
                None,
            ],
        ) as request:
            self.client.save(persisted)
            self.client.save(copy.deepcopy(persisted))
        self.assertEqual(request.call_count, 3)

    def test_load_reads_by_resolved_commit_and_preserves_failed_task(self) -> None:
        persisted = state()
        task_id, record = task(status="failed")
        record["reason"] = "workflow failed"
        persisted["tasks"][task_id] = record
        contents = base64.b64encode(json.dumps(persisted).encode()).decode()
        with patch.object(
            self.client,
            "request",
            side_effect=[
                {"object": {"sha": "resolved-commit"}},
                {"content": contents},
            ],
        ) as request:
            restored = self.client.load(LATER)
            self.client.save(restored)
        self.assertEqual(
            [call.args[0] for call in request.call_args_list],
            [
                self.prefix + "/git/ref/heads/automation/upstream-state",
                self.prefix + "/contents/state.json?ref=resolved-commit",
            ],
        )
        self.assertEqual(self.client.head, "resolved-commit")
        self.assertEqual(restored, persisted)
        self.assertEqual(restored["tasks"][task_id]["status"], "failed")

    def test_load_missing_branch_starts_empty_without_reading_main(self) -> None:
        endpoint = self.prefix + "/git/ref/heads/automation/upstream-state"
        error = HTTPError(endpoint, 404, "missing", None, None)
        error.close()
        with patch.object(self.client, "request", side_effect=error) as request:
            restored = self.client.load(NOW)
        request.assert_called_once_with(endpoint, None, "GET")
        self.assertEqual(restored, state())
        self.assertIsNone(self.client.head)

    def test_non_fast_forward_during_dispatch_persistence_prevents_dispatch(
        self,
    ) -> None:
        self.client.head = "old-commit"
        persisted = state()
        saved_count = 0
        dispatches: list[str] = []
        contents: list[dict] = []

        def request(endpoint: str, payload: dict, method: str) -> dict | None:
            nonlocal saved_count
            if endpoint.endswith("/git/trees"):
                contents.append(json.loads(payload["tree"][0]["content"]))
                return {"sha": "tree"}
            if endpoint.endswith("/git/commits"):
                return {"sha": f"commit-{saved_count + 1}"}
            if endpoint.endswith("/git/refs/heads/automation/upstream-state"):
                saved_count += 1
                if saved_count == 2:
                    error = HTTPError(endpoint, 422, "non-fast-forward", None, None)
                    error.close()
                    raise error
                return None
            if endpoint.endswith("/dispatches"):
                dispatches.append(endpoint)
                return None
            raise AssertionError(f"unexpected endpoint: {endpoint}")

        with patch.object(self.client, "request", side_effect=request):
            with self.assertRaises(HTTPError):
                watch.schedule(
                    self.client,
                    persisted,
                    ["0.159.3"],
                    set(),
                    [],
                    SOURCE,
                    FakeResolver(),
                    NOW,
                )
        self.assertEqual(dispatches, [])
        self.assertEqual(next(iter(contents[0]["tasks"].values()))["status"], "queued")
        self.assertEqual(
            next(iter(contents[1]["tasks"].values()))["status"], "dispatching"
        )
        self.assertEqual(self.client.head, "commit-1")


if __name__ == "__main__":
    unittest.main()
