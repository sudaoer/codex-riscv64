from __future__ import annotations

import unittest
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
        self.assertIn("cancel-in-progress: false", self.preflight)

    def test_evidence_and_attestation_are_checked_before_payload_is_staged(self) -> None:
        ordered_checks = (
            "scripts/workflow_handoff.py prepare-report",
            "scripts/workflow_handoff.py wait-candidate",
            "validate-run",
            "gh attestation verify",
            "verify-latest",
            "--k3-report k3-report.json",
            "name: Stage exact promotion payload",
        )
        positions = [self.preflight.index(check) for check in ordered_checks]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('--validation-run-id "$VALIDATION_RUN_ID"', self.preflight)
        self.assertIn('--validation-run-attempt "$VALIDATION_RUN_ATTEMPT"', self.preflight)
        self.assertIn("--signer-workflow", self.preflight)

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
        self.assertIn('gh release edit "$RELEASE_TAG" --draft=false --latest', self.publish)


if __name__ == "__main__":
    unittest.main()
