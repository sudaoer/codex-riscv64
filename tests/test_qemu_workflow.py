import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = (ROOT / ".github/workflows/candidate-build.yml").read_text()
QEMU = (ROOT / ".github/workflows/qemu-validate.yml").read_text()


class QemuWorkflowTests(unittest.TestCase):
    @staticmethod
    def continue_chain_script() -> str:
        block = QEMU[QEMU.index("      - name: Dispatch validated publish\n") :]
        run = block[block.index("        run: |\n") + len("        run: |\n") :]
        return textwrap.dedent(run)

    def test_candidate_exports_build_decision_and_dispatches_only_new_candidates(self) -> None:
        self.assertIn(
            "build_required: ${{ steps.decide.outputs.build_required }}", CANDIDATE
        )
        self.assertIn(
            "Force a rebuild but do not overwrite an existing formal release",
            CANDIDATE,
        )
        handoff = CANDIDATE[CANDIDATE.index("  continue-chain:\n") :]
        self.assertIn("needs.build.result == 'success'", handoff)
        self.assertIn("needs.build.outputs.build_required == 'true'", handoff)
        self.assertIn("github.ref == 'refs/heads/main'", handoff)
        self.assertIn("actions: write", handoff)
        self.assertIn("timeout-minutes: 5", handoff)
        self.assertIn("gh workflow run qemu-validate.yml", handoff)
        self.assertIn('-f candidate_run_id="$GITHUB_RUN_ID"', handoff)

    def test_qemu_validation_is_explicitly_dispatched_and_publishes_after_success(self) -> None:
        self.assertIn("workflow_dispatch:", QEMU)
        self.assertNotIn("workflow_run:", QEMU)
        self.assertIn("candidate_run_id:", QEMU)
        self.assertIn("group: qemu-${{ inputs.candidate_run_id }}", QEMU)
        self.assertIn("cancel-in-progress: false", QEMU)
        self.assertIn("if: github.ref == 'refs/heads/main'", QEMU)
        self.assertIn("timeout-minutes: 150", QEMU)
        validation = QEMU.split("\n  continue-chain:\n", 1)[0]
        self.assertIn("runs-on: ubuntu-26.04", validation)
        self.assertIn("qemu-system-riscv qemu-utils qemu-efi-riscv64", validation)
        validate = QEMU[: QEMU.index("  continue-chain:\n")]
        self.assertIn("actions: read", validate)
        self.assertNotIn("actions: write", validate)
        self.assertIn("attestations: read", QEMU)
        self.assertIn(
            "actions/cache@55cc8345863c7cc4c66a329aec7e433d2d1c52a9", QEMU
        )
        self.assertIn("path: .work/qemu", QEMU)
        self.assertIn("id: qemu_image", QEMU)
        self.assertIn("steps.qemu_image.outputs.sha256", QEMU)
        self.assertIn("workflow_handoff.py wait-candidate", QEMU)
        self.assertIn('--run-id "$CANDIDATE_RUN_ID"', QEMU)
        self.assertIn("--target qemu", QEMU)
        self.assertIn(
            '--output "$RUNNER_TEMP/qemu-validation/qemu-report.json"', QEMU
        )
        attempt = QEMU.index("id: validation_attempt")
        self.assertIn(
            'validation_attempt: ${{ steps.validation_attempt.outputs.attempt }}',
            validate,
        )
        self.assertLess(attempt, QEMU.index("name: Write validation summary"))
        self.assertLess(attempt, QEMU.index("name: Upload QEMU report and logs"))
        self.assertIn("if: always()", QEMU)
        self.assertIn(
            "qemu-validation-${{ github.run_id }}-${{ steps.validation_attempt.outputs.attempt }}",
            QEMU,
        )
        publish = QEMU[QEMU.index("  continue-chain:\n") :]
        self.assertIn("needs.validate.result == 'success'", publish)
        self.assertIn("timeout-minutes: 5", publish)
        self.assertIn(
            'ORIGINAL_VALIDATION_ATTEMPT: ${{ needs.validate.outputs.validation_attempt }}',
            publish,
        )
        self.assertIn("gh workflow run publish.yml", publish)
        rerun_guard = 'if [[ "$ORIGINAL_VALIDATION_ATTEMPT" != "$GITHUB_RUN_ATTEMPT" ]]'
        self.assertIn(rerun_guard, publish)
        qemu_dispatch = publish.index("gh workflow run qemu-validate.yml")
        publish_dispatch = publish.index("gh workflow run publish.yml")
        self.assertLess(qemu_dispatch, publish_dispatch)
        self.assertIn('candidate_run_id="$CANDIDATE_RUN_ID"', publish)
        self.assertIn("exit 0", publish)
        self.assertIn('CANDIDATE_RUN_ID: ${{ inputs.candidate_run_id }}', publish)
        self.assertIn('candidate_run_id="$CANDIDATE_RUN_ID"', publish)
        self.assertIn('validation_run_id="$GITHUB_RUN_ID"', publish)
        self.assertIn('validation_run_attempt="$ORIGINAL_VALIDATION_ATTEMPT"', publish)

    def run_continue_chain(
        self, *, current_attempt: str, original_attempt: str, gh_exit: str = "0"
    ) -> tuple[subprocess.CompletedProcess[str], str, str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            args_path = root / "gh-args"
            fake_gh = bin_dir / "gh"
            fake_gh.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$@\" > \"$GH_ARGS_PATH\"\n"
                "exit \"${GH_EXIT:-0}\"\n"
            )
            fake_gh.chmod(0o755)
            summary_path = root / "summary"
            environment = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "GH_ARGS_PATH": str(args_path),
                "GH_EXIT": gh_exit,
                "GITHUB_STEP_SUMMARY": str(summary_path),
                "GITHUB_RUN_ATTEMPT": current_attempt,
                "ORIGINAL_VALIDATION_ATTEMPT": original_attempt,
                "CANDIDATE_RUN_ID": "42",
                "GITHUB_REPOSITORY": "example/repository",
                "GITHUB_RUN_ID": "100",
            }
            result = subprocess.run(
                ["bash", "-e"],
                input=self.continue_chain_script(),
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )
            args = args_path.read_text() if args_path.exists() else ""
            summary = summary_path.read_text() if summary_path.exists() else ""
            return result, args, summary

    def test_same_attempt_dispatches_publish_with_matching_validation_attempt(self) -> None:
        result, args, _summary = self.run_continue_chain(
            current_attempt="2", original_attempt="2"
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("publish.yml", args)
        self.assertNotIn("qemu-validate.yml", args)
        self.assertIn("validation_run_attempt=2", args)

    def test_rerun_dispatches_fresh_qemu_validation_and_exits_successfully(self) -> None:
        result, args, summary = self.run_continue_chain(
            current_attempt="3", original_attempt="2"
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("qemu-validate.yml", args)
        self.assertNotIn("publish.yml", args)
        self.assertIn("Re-dispatching QEMU validation", summary)

    def test_failed_rerun_qemu_dispatch_is_not_hidden_by_exit_zero(self) -> None:
        result, _args, _summary = self.run_continue_chain(
            current_attempt="3", original_attempt="2", gh_exit="7"
        )
        self.assertEqual(result.returncode, 7)
