from __future__ import annotations

import base64
import io
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import workflow_handoff as handoff  # noqa: E402


REPOSITORY = "sudaoer/codex-riscv64"
SHA = "a" * 40


class Clock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class Response:
    def __init__(self, value: object) -> None:
        self.raw = json.dumps(value).encode()

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.raw


class Opener:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, float]] = []

    def __call__(self, request: urllib.request.Request, *, timeout: float) -> Response:
        self.calls.append((request.full_url, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return Response(response)


def run_payload(
    run_id: int | str = 42,
    *,
    workflow: str = handoff.CANDIDATE_WORKFLOW,
    event: str = "workflow_dispatch",
    status: str = "completed",
    conclusion: str | None = "success",
    repository: str = REPOSITORY,
    head_repository: str | None = REPOSITORY,
    attempt: int = 1,
    head_sha: str = SHA,
) -> dict[str, object]:
    result: dict[str, object] = {
        "id": run_id,
        "repository": {"full_name": repository},
        "head_branch": "main",
        "path": workflow,
        "event": event,
        "status": status,
        "conclusion": conclusion,
        "head_sha": head_sha,
        "run_attempt": attempt,
    }
    if head_repository is not None:
        result["head_repository"] = {"full_name": head_repository}
    return result


def client(
    opener: Opener, clock: Clock | None = None
) -> handoff.GitHubClient:
    clock = clock or Clock()
    return handoff.GitHubClient(
        "secret-token", REPOSITORY, opener=opener, monotonic=clock.now, sleeper=clock.sleep
    )


class WorkflowHandoffTests(unittest.TestCase):
    def test_pending_then_success_waits_and_writes_candidate(self) -> None:
        clock = Clock()
        opener = Opener(
            [
                run_payload(status="queued", conclusion=None),
                run_payload(),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run.json"
            result = handoff.wait_candidate(
                "42", output, client=client(opener, clock), max_wait=20
            )
            self.assertEqual(result["id"], 42)
            self.assertEqual(json.loads(output.read_text())["head_sha"], SHA)
            self.assertEqual(clock.sleeps, [5.0])

    def test_candidate_identity_is_rejected(self) -> None:
        for payload in (
            run_payload(repository="other/repo"),
            run_payload(head_repository="other/repo"),
            run_payload(workflow=".github/workflows/other.yml"),
            run_payload(event="push"),
            run_payload(head_sha="bad"),
        ):
            with self.subTest(payload=payload):
                opener = Opener([payload])
                with tempfile.TemporaryDirectory() as directory:
                    with self.assertRaises(handoff.HandoffError):
                        handoff.wait_candidate(
                            "42",
                            Path(directory) / "run.json",
                            client=client(opener),
                        )

    def test_candidate_failure_and_cancel_are_terminal(self) -> None:
        for conclusion in ("failure", "cancelled"):
            with self.subTest(conclusion=conclusion):
                opener = Opener([run_payload(conclusion=conclusion)])
                with tempfile.TemporaryDirectory() as directory:
                    with self.assertRaises(handoff.HandoffError):
                        handoff.wait_candidate(
                            "42",
                            Path(directory) / "run.json",
                            client=client(opener),
                        )

    def test_pending_candidate_identity_is_rejected_without_waiting(self) -> None:
        clock = Clock()
        for payload in (
            run_payload(status="queued", conclusion=None, repository="other/repo"),
            run_payload(status="in_progress", conclusion=None, head_repository="other/repo"),
            run_payload(status="queued", conclusion=None, workflow=".github/workflows/other.yml"),
            run_payload(status="queued", conclusion=None, head_sha="bad"),
        ):
            with self.subTest(payload=payload):
                opener = Opener([payload])
                with tempfile.TemporaryDirectory() as directory:
                    with self.assertRaises(handoff.HandoffError):
                        handoff.wait_candidate(
                            "42", Path(directory) / "run.json", client=client(opener, clock), max_wait=600
                        )
                self.assertEqual(clock.value, 0.0)

    def test_candidate_transient_http_is_retried_and_timeout_is_bounded(self) -> None:
        headers = {"X-RateLimit-Remaining": "0"}
        limited = urllib.error.HTTPError(
            "https://api.github.com", 403, "rate", headers, io.BytesIO(b"rate limit")
        )
        clock = Clock()
        opener = Opener([limited, run_payload()])
        with tempfile.TemporaryDirectory() as directory:
            handoff.wait_candidate(
                "42", Path(directory) / "run.json", client=client(opener, clock), max_wait=20
            )
        self.assertEqual(clock.sleeps, [5.0])
        self.assertLessEqual(opener.calls[0][1], 30.0)

        clock = Clock()
        opener = Opener([urllib.error.URLError("offline")] * 4)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(handoff.HandoffError, "timed out"):
                handoff.wait_candidate(
                    "42", Path(directory) / "run.json", client=client(opener, clock), max_wait=10
                )
        self.assertEqual(clock.value, 10.0)

    def test_terminal_http_is_not_retried(self) -> None:
        for status in (401, 403):
            opener = Opener(
                [urllib.error.HTTPError("https://api.github.com", status, "denied", {}, io.BytesIO())]
            )
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(handoff.GitHubHTTPError):
                    handoff.wait_candidate(
                        "42", Path(directory) / "run.json", client=client(opener)
                    )
            self.assertEqual(len(opener.calls), 1)

    def test_validation_exact_attempt_and_report_are_checked(self) -> None:
        clock = Clock()
        validation = run_payload(
            run_id=99,
            workflow=handoff.QEMU_WORKFLOW,
            event="workflow_dispatch",
            attempt=2,
            head_repository=REPOSITORY,
        )
        opener = Opener([validation])
        report = {
            "validation_target": "qemu-system-riscv64",
            "candidate_run_id": "42",
        }
        seen: dict[str, object] = {}

        def download(args: list[str], **kwargs: object) -> SimpleNamespace:
            seen["args"] = args
            target = Path(args[args.index("--dir") + 1]) / "qemu-report.json"
            target.write_bytes(json.dumps(report, separators=(",", ":")).encode())
            return SimpleNamespace(returncode=0)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            raw = handoff.prepare_report(
                "42",
                "99",
                "2",
                "",
                output,
                client=client(opener, clock),
                env={"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_ACTOR": "x"},
                now=clock.now,
                sleep=clock.sleep,
                runner=download,
            )
            self.assertEqual(raw, output.read_bytes())
            self.assertIn("qemu-validation-99-2", seen["args"])

    def test_validation_attempt_mismatch_is_rejected(self) -> None:
        payload = run_payload(
            run_id=99,
            workflow=handoff.QEMU_WORKFLOW,
            event="workflow_dispatch",
            attempt=1,
            head_repository=REPOSITORY,
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(handoff.HandoffError):
                handoff.prepare_report(
                    "42",
                    "99",
                    "2",
                    "",
                    Path(directory) / "report.json",
                    client=client(Opener([payload])),
                    env={},
                )

    def test_auto_validation_does_not_fall_back_to_manual_report(self) -> None:
        opener = Opener([])
        encoded = base64.b64encode(b'{"manual":true}').decode()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(handoff.HandoffError):
                handoff.prepare_report(
                    "42",
                    "99",
                    "1",
                    encoded,
                    Path(directory) / "report.json",
                    client=client(opener),
                    env={"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_ACTOR": "alice"},
                )
        self.assertEqual(opener.calls, [])

    def test_artifact_target_candidate_and_symlink_are_rejected(self) -> None:
        validation = run_payload(
            run_id=99, workflow=handoff.QEMU_WORKFLOW, head_repository=REPOSITORY
        )

        for report in (
            {"validation_target": "native-k3", "candidate_run_id": "42"},
            {"validation_target": "qemu-system-riscv64", "candidate_run_id": "41"},
        ):
            def wrong_download(args: list[str], **kwargs: object) -> SimpleNamespace:
                target = Path(args[args.index("--dir") + 1]) / "qemu-report.json"
                target.write_text(json.dumps(report))
                return SimpleNamespace(returncode=0)

            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(handoff.HandoffError):
                    handoff.prepare_report(
                        "42", "99", "1", "", Path(directory) / "out.json",
                        client=client(Opener([validation])), env={}, runner=wrong_download
                    )

        def symlink_download(args: list[str], **kwargs: object) -> SimpleNamespace:
            directory = Path(args[args.index("--dir") + 1])
            target = directory / "qemu-report.json"
            target.symlink_to(directory / "elsewhere")
            return SimpleNamespace(returncode=0)

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(handoff.HandoffError):
                handoff.prepare_report(
                    "42", "99", "1", "", Path(directory) / "out.json",
                    client=client(Opener([validation])), env={}, runner=symlink_download
                )

    def test_manual_report_requires_exactly_one_source_and_human_permission(self) -> None:
        raw = b'{"validation_target":"native-k3"}'
        encoded = base64.b64encode(raw).decode()
        permission = {"permission": "write", "user": {"type": "User"}}
        env = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_ACTOR": "alice"}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            result = handoff.prepare_report(
                "42", "", "", encoded, output,
                client=client(Opener([permission])), env=env
            )
            self.assertEqual(result, raw)
            self.assertEqual(output.read_bytes(), raw)
            for values in (("1", ""), ("", "1")):
                with self.assertRaises(handoff.HandoffError):
                    handoff.prepare_report(
                        "42", values[0], values[1], encoded, output,
                        client=client(Opener([permission])), env=env
                    )

    def test_manual_report_checks_event_actor_type_and_permission(self) -> None:
        encoded = base64.b64encode(b"{}").decode()
        cases = (
            ({"GITHUB_EVENT_NAME": "push", "GITHUB_ACTOR": "alice"}, [{"permission": "write", "user": {"type": "User"}}]),
            ({"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_ACTOR": "actions[bot]"}, []),
            ({"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_ACTOR": "alice"}, [{"permission": "read", "user": {"type": "User"}}]),
            ({"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_ACTOR": "alice"}, [{"permission": "write", "user": {"type": "Bot"}}]),
        )
        for env, responses in cases:
            with self.subTest(env=env):
                with tempfile.TemporaryDirectory() as directory:
                    with self.assertRaises(handoff.HandoffError):
                        handoff.prepare_report(
                            "42", "", "", encoded, Path(directory) / "out.json",
                            client=client(Opener(responses)), env=env
                        )

    def test_manual_permission_without_type_uses_user_endpoint(self) -> None:
        opener = Opener([{"permission": "maintain", "user": {}}, {"type": "User"}])
        encoded = base64.b64encode(b"{}").decode()
        with tempfile.TemporaryDirectory() as directory:
            handoff.prepare_report(
                "42", "", "", encoded, Path(directory) / "out.json",
                client=client(opener),
                env={"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_ACTOR": "alice"},
            )
        self.assertIn("/users/alice", opener.calls[1][0])


if __name__ == "__main__":
    unittest.main()
