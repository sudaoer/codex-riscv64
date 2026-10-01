from __future__ import annotations

import unittest
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PublishWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = (ROOT / ".github/workflows/publish.yml").read_text()
        self.preflight, self.publish = self.workflow.split("\n  publish:\n", 1)

    def test_only_main_can_enter_publication(self) -> None:
        self.assertIn("if: github.ref == 'refs/heads/main'", self.preflight)
        self.assertIn("needs: preflight", self.publish)
        self.assertIn("environment: release", self.publish)
        self.assertIn("group: stable-release", self.preflight)
        self.assertIn("queue: max", self.preflight)
        self.assertIn("cancel-in-progress: false", self.preflight)

    def test_evidence_and_attestation_are_checked_before_payload_is_staged(self) -> None:
        ordered_checks = (
            "scripts/workflow_handoff.py prepare-report",
            "scripts/workflow_handoff.py wait-candidate",
            "validate-run",
            "gh attestation verify",
            "--k3-report k3-report.json",
            "name: Stage exact promotion payload",
        )
        positions = [self.preflight.index(check) for check in ordered_checks]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('--validation-run-id "$VALIDATION_RUN_ID"', self.preflight)
        self.assertIn('--validation-run-attempt "$VALIDATION_RUN_ATTEMPT"', self.preflight)
        self.assertIn("--signer-workflow", self.preflight)
        self.assertNotIn("verify-latest", self.workflow)

    def test_failed_publish_rerun_uses_the_original_preflight_payload(self) -> None:
        self.assertIn(
            "name=verified-promotion-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}",
            self.preflight,
        )
        self.assertIn("promotion_artifact: ${{ steps.payload.outputs.name }}", self.preflight)
        self.assertIn("name: ${{ needs.preflight.outputs.promotion_artifact }}", self.publish)
        self.assertNotIn("github.run_attempt", self.publish)
        self.assertIn("--release-lock promotion/candidate/release-lock.json", self.publish)
        self.assertIn("--k3-report promotion/k3-report.json", self.publish)

    def test_existing_releases_are_never_replaced_by_the_workflow(self) -> None:
        refusal = self.publish.index("Release already exists and will not be overwritten")
        creation = self.publish.index("gh release create")
        self.assertLess(refusal, creation)
        self.assertLess(self.publish.index("scripts/release_tag.py"), creation)
        self.assertIn('--commit "$CANDIDATE_HEAD_SHA"', self.publish)
        self.assertIn("--verify-tag", self.publish)
        self.assertNotIn("--target", self.publish)
        self.assertNotIn("gh release delete", self.workflow)
        self.assertNotIn("--clobber", self.workflow)
        self.assertIn("--draft", self.publish)
        latest_state = self.publish.index("latest-state > latest-state.json")
        publish = self.publish.index('gh release edit "$RELEASE_TAG"')
        self.assertLess(self.publish.index("release asset mismatch"), latest_state)
        self.assertLess(latest_state, publish)
        self.assertIn('state["make_latest"]', self.publish)
        self.assertIn('--latest="$MAKE_LATEST"', self.publish)
        self.assertNotIn('--draft=false --latest\n', self.publish)

    def test_locked_policy_is_used_without_replacing_workflow_scripts(self) -> None:
        self.assertIn("ref: ${{ steps.source.outputs.sha }}", self.preflight)
        self.assertIn("ref: ${{ needs.preflight.outputs.candidate_head_sha }}", self.publish)
        self.assertEqual(self.workflow.count("path: locked-source"), 2)
        self.assertEqual(
            self.workflow.count("--policy locked-source/release/policy.toml"), 4,
        )
        source = self.preflight.index("Resolve locked candidate source revision")
        checkout = self.preflight.index("Check out locked candidate release policy")
        validate_run = self.preflight.index("Verify selected GitHub run")
        self.assertLess(source, checkout)
        self.assertLess(checkout, validate_run)
        self.assertIn('--source-digest "$(python3', self.preflight)
        self.assertIn('json.load(open("run.json"))["head_sha"]', self.preflight)

    def test_locked_source_output_accepts_only_exact_commit_sha(self) -> None:
        step = self.preflight.split("- name: Resolve locked candidate source revision\n", 1)[1]
        block = step.split("python3 - <<'PY'\n", 1)[1].split("\n          PY", 1)[0]
        script = "\n".join(line[10:] for line in block.splitlines())
        for source_sha in ("a" * 40, "a" * 39, "main", "a" * 40 + "\nsha=main", None, 123):
            with self.subTest(source_sha=source_sha), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "candidate").mkdir()
                (root / "candidate/candidate.json").write_text(json.dumps({"candidate_head_sha": source_sha}))
                output = root / "output"
                result = subprocess.run(
                    [sys.executable, "-c", script], cwd=root,
                    env={**os.environ, "GITHUB_OUTPUT": str(output)},
                    capture_output=True, text=True, check=False,
                )
                if source_sha == "a" * 40:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(output.read_text(), f"sha={source_sha}\n")
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("invalid locked candidate source SHA", result.stderr)
                    self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
