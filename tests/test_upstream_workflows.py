from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"


class UpstreamWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compat = (WORKFLOWS / "compat-check.yml").read_text()
        self.lock_bytes = b'{"schema_version": 1, "upstream": {"version": "0.159.3"}}\n'
        self.source_sha = "a" * 40
        self.task_id = hashlib.sha256(
            json.dumps(
                {"release_lock": json.loads(self.lock_bytes), "source_sha": self.source_sha},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    def run_lock_step(self, overrides: dict[str, str]) -> tuple[subprocess.CompletedProcess[str], bytes | None]:
        step = self.compat.split("- name: Resolve stable release lock\n", 1)[1]
        block = step.split("python3 - <<'PY'\n", 1)[1].split("\n          PY", 1)[0]
        script = "\n".join(line[10:] for line in block.splitlines())
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment.update(
                {
                    "TASK_ID": self.task_id,
                    "RELEASE_LOCK_B64": base64.b64encode(self.lock_bytes).decode(),
                    "DOWNSTREAM_SOURCE_SHA": self.source_sha,
                    "CONTINUE_CHAIN": "true",
                    "GITHUB_ACTOR": "github-actions[bot]",
                    "GITHUB_SHA": self.source_sha,
                    "RUNNER_TEMP": directory,
                }
            )
            environment.update(overrides)
            result = subprocess.run(
                [sys.executable, "-c", script], env=environment,
                capture_output=True, text=True, check=False,
            )
            lock_path = Path(directory) / "release-lock.json"
            return result, lock_path.read_bytes() if lock_path.exists() else None

    def test_bot_task_preserves_exact_lock_bytes(self) -> None:
        result, written = self.run_lock_step({})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(written, self.lock_bytes)

    def test_metadata_probe_uses_actions_token_and_only_reads_releases(self) -> None:
        name = "- name: Validate upstream release metadata access\n"
        step = self.compat.split(name, 1)[1].split("\n      - name:", 1)[0]
        self.assertIn("GITHUB_TOKEN: ${{ github.token }}", step)
        block = step.split("python3 - <<'PY'\n", 1)[1].split("\n          PY", 1)[0]
        script = "\n".join(line[10:] for line in block.splitlines())
        github = Mock()
        github.return_value.releases.return_value = [{"tag_name": "rust-v0.159.3"}]
        output = StringIO()
        with (
            patch.dict(os.environ, {"GITHUB_TOKEN": "installation-token", "GITHUB_REPOSITORY": "owner/downstream"}),
            patch.dict(sys.modules, {"upstream_watch": SimpleNamespace(GitHub=github)}),
            patch.object(sys, "path", sys.path.copy()),
            redirect_stdout(output),
        ):
            exec(compile(script, "metadata-probe", "exec"), {})
        github.assert_called_once_with("installation-token", "owner/downstream")
        github.return_value.releases.assert_called_once_with("openai/codex")
        self.assertEqual(len(github.mock_calls), 2)
        self.assertIn("Read 1 upstream release metadata records", output.getvalue())
        self.assertLess(self.compat.index("Validate downstream release tooling"), self.compat.index(name))
        self.assertLess(self.compat.index(name), self.compat.index("Resolve stable release lock"))

    def test_automatic_task_rejects_unauthorized_or_mismatched_inputs(self) -> None:
        cases = (
            ({"GITHUB_ACTOR": "maintainer"}, "bot-dispatched chain"),
            ({"CONTINUE_CHAIN": "false"}, "bot-dispatched chain"),
            ({"DOWNSTREAM_SOURCE_SHA": "b" * 40}, "dispatched revision"),
            ({"TASK_ID": "invalid"}, "invalid upstream task identity"),
            ({"TASK_ID": "b" * 64}, "does not match release inputs"),
            ({"RELEASE_LOCK_B64": ""}, "release lock and source SHA"),
            ({"DOWNSTREAM_SOURCE_SHA": ""}, "release lock and source SHA"),
            ({"RELEASE_LOCK_B64": "%%%"}, "Only base64 data is allowed"),
        )
        for overrides, message in cases:
            with self.subTest(overrides=overrides):
                result, written = self.run_lock_step(overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertIsNone(written)

    def test_manual_default_leaves_lock_resolution_to_release_tool(self) -> None:
        result, written = self.run_lock_step(
            {"TASK_ID": "", "RELEASE_LOCK_B64": "", "DOWNSTREAM_SOURCE_SHA": "", "GITHUB_ACTOR": "maintainer", "CONTINUE_CHAIN": "false"}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(written)
        self.assertIn('if [[ -z "$RELEASE_LOCK_B64" ]]; then', self.compat)
        self.assertIn("scripts/release.py resolve-latest", self.compat)

    def test_every_chain_stage_preserves_task_identity(self) -> None:
        stages = ("compat-check.yml", "v8-build.yml", "candidate-build.yml", "qemu-validate.yml", "publish.yml")
        for filename in stages:
            with self.subTest(filename=filename):
                workflow = (WORKFLOWS / filename).read_text()
                self.assertIn("run-name: ${{ inputs.task_id && format('upstream:{0}', inputs.task_id)", workflow)
                self.assertIn("      task_id:\n", workflow)
                if filename != "publish.yml":
                    dispatch = workflow.split("  continue-chain:\n", 1)[1]
                    self.assertEqual(dispatch.count("gh workflow run"), dispatch.count('-f task_id="$TASK_ID"'))

    def test_watcher_frequency_and_serialized_shared_resources(self) -> None:
        watcher = (WORKFLOWS / "upstream-watch.yml").read_text()
        self.assertIn('cron: "3-59/10 * * * *"', watcher)
        self.assertIn("contents: write", watcher)
        self.assertIn("actions: write", watcher)
        self.assertIn("if: github.ref == 'refs/heads/main'", watcher)
        self.assertIn("retry_version:", watcher)
        self.assertIn('scripts/upstream_watch.py --retry-version "$RETRY_VERSION"', watcher)
        self.assertNotIn("release-state", watcher)
        self.assertIn("group: compat-${{ inputs.task_id ||", self.compat)
        self.assertIn("cancel-in-progress: ${{ inputs.task_id == '' }}", self.compat)
        for filename, group in (("v8-build.yml", "v8-build"), ("publish.yml", "stable-release")):
            workflow = (WORKFLOWS / filename).read_text()
            self.assertIn(f"group: {group}\n  queue: max\n  cancel-in-progress: false", workflow)

    def test_build_source_and_attestation_workflow_revisions_are_preserved(self) -> None:
        for filename in ("v8-build.yml", "candidate-build.yml"):
            with self.subTest(filename=filename):
                workflow = (WORKFLOWS / filename).read_text()
                self.assertIn("DOWNSTREAM_SOURCE_SHA: ${{ steps.source.outputs.sha }}", workflow)
                self.assertNotIn("GITHUB_SHA: ${{ steps.source.outputs.sha }}", workflow)
                self.assertIn('builder.get("workflow_head_sha", builder["head_sha"])', workflow)
                self.assertIn('--source-digest "$builder_sha"', workflow)
        qemu = (WORKFLOWS / "qemu-validate.yml").read_text()
        self.assertIn("--candidate-policy", qemu)


if __name__ == "__main__":
    unittest.main()
