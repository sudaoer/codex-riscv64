#!/usr/bin/env python3
"""Safely hand off GitHub Actions runs and validation evidence to Publish."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "release" / "policy.toml"
API_ROOT = "https://api.github.com"
CANDIDATE_WORKFLOW = ".github/workflows/candidate-build.yml"
QEMU_WORKFLOW = ".github/workflows/qemu-validate.yml"
MAX_WAIT = 600.0
POLL_INTERVAL = 5.0
HTTP_TIMEOUT = 30.0
ID_RE = re.compile(r"^[0-9]+$", re.ASCII)
SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.ASCII)
BOT_RE = re.compile(r"\[bot\]$", re.ASCII | re.IGNORECASE)


class HandoffError(RuntimeError):
    """An input, GitHub identity, or evidence handoff invariant failed."""


class GitHubHTTPError(HandoffError):
    def __init__(self, status: int, *, retryable: bool = False) -> None:
        self.status = status
        self.retryable = retryable
        super().__init__(f"GitHub API returned HTTP {status}")


class GitHubNetworkError(HandoffError):
    """A network failure that can be retried until the operation deadline."""


def _positive_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value) or int(value) <= 0:
        raise HandoffError(f"{label} must be a positive ASCII decimal ID")
    return value


def _json_positive_id(value: Any, label: str) -> str:
    if isinstance(value, bool):
        raise HandoffError(f"{label} is not a valid positive ID")
    if isinstance(value, int) and value > 0:
        return str(value)
    if isinstance(value, str) and ID_RE.fullmatch(value) and int(value) > 0:
        return value
    raise HandoffError(f"{label} is not a valid positive ID")


def _head_sha(value: Any, label: str = "head_sha") -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise HandoffError(f"{label} is not a valid commit SHA")
    return value


def _workflow_path(value: Any, expected: str) -> bool:
    if not isinstance(value, str):
        return False
    return value == expected or (
        value.startswith(expected + "@") and len(value) > len(expected) + 1
    )


def _full_name(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        name = value.get("full_name")
        return name if isinstance(name, str) else None
    return None


def _run_identity(
    run: Mapping[str, Any],
    repository: str,
    *,
    expected_id: str,
    workflow: str,
    events: Sequence[str],
    require_head_repository: bool = True,
    require_status: bool = True,
) -> None:
    if _json_positive_id(run.get("id"), "run id") != expected_id:
        raise HandoffError("GitHub run ID does not match the requested run")
    if _full_name(run.get("repository")) != repository:
        raise HandoffError("GitHub run repository does not match this repository")
    if require_head_repository and _full_name(run.get("head_repository")) != repository:
        raise HandoffError("GitHub run head repository does not match this repository")
    if run.get("head_branch") != "main":
        raise HandoffError("GitHub run did not run from main")
    if not _workflow_path(run.get("path"), workflow):
        raise HandoffError("GitHub run workflow path is unexpected")
    if run.get("event") not in events:
        raise HandoffError("GitHub run event is unexpected")
    if require_status and (
        run.get("status") != "completed" or run.get("conclusion") != "success"
    ):
        raise HandoffError("GitHub run did not complete successfully")
    _head_sha(run.get("head_sha"))


def _policy_repository(path: Path = POLICY_PATH) -> str:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        repository = raw["distribution"]["repository"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as error:
        raise HandoffError(f"cannot read release policy: {error}") from error
    if not isinstance(repository, str) or repository.count("/") != 1:
        raise HandoffError("release policy has an invalid distribution.repository")
    return repository


def _environment(env: Mapping[str, str] | None = None) -> tuple[dict[str, str], str]:
    values = dict(os.environ if env is None else env)
    token = values.get("GH_TOKEN") or values.get("GITHUB_TOKEN")
    if not token:
        raise HandoffError("GH_TOKEN or GITHUB_TOKEN is required")
    configured = values.get("GITHUB_REPOSITORY")
    if not configured:
        raise HandoffError("GITHUB_REPOSITORY is required")
    expected = _policy_repository()
    if configured != expected:
        raise HandoffError("GITHUB_REPOSITORY does not match release policy")
    return values, expected


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _rate_limited_403(headers: Any, body: bytes) -> bool:
    normalized = {
        str(key).lower(): value for key, value in getattr(headers, "items", lambda: ())()
    }
    remaining = str(normalized.get("x-ratelimit-remaining", ""))
    retry_after = normalized.get("retry-after")
    text = body[:4096].decode("utf-8", "ignore").lower()
    return (
        remaining == "0"
        or retry_after is not None
        or "rate limit" in text
        or "secondary rate limit" in text
    )


class GitHubClient:
    """Small injectable GitHub API client with bounded per-call timeouts."""

    def __init__(
        self,
        token: str,
        repository: str,
        *,
        opener: Callable[..., Any] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.token = token
        self.repository = repository
        self.opener = opener or urllib.request.urlopen
        self.monotonic = monotonic
        self.sleeper = sleeper

    def request_json(self, endpoint: str, *, deadline: float) -> dict[str, Any]:
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            raise HandoffError("GitHub operation deadline exceeded")
        request = urllib.request.Request(
            API_ROOT + endpoint,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "codex-riscv64-workflow-handoff",
            },
        )
        try:
            with self.opener(request, timeout=min(HTTP_TIMEOUT, remaining)) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            try:
                body = error.read(4096)
            except OSError:
                body = b""
            finally:
                error.close()
            status = int(error.code)
            retryable = status == 404 or status == 429 or status >= 500
            if status == 403 and _rate_limited_403(error.headers or {}, body):
                retryable = True
            raise GitHubHTTPError(status, retryable=retryable) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise GitHubNetworkError("GitHub API network request failed") from error
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HandoffError("GitHub API returned invalid JSON") from error
        if not isinstance(value, dict):
            raise HandoffError("GitHub API returned a JSON value that is not an object")
        return value

    def retry_json(
        self,
        endpoint: str,
        *,
        deadline: float,
        retry_not_found: bool = True,
    ) -> dict[str, Any]:
        while True:
            try:
                return self.request_json(endpoint, deadline=deadline)
            except GitHubHTTPError as error:
                if not error.retryable or (error.status == 404 and not retry_not_found):
                    raise
            except GitHubNetworkError:
                pass
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise HandoffError("GitHub operation deadline exceeded")
            self.sleeper(min(POLL_INTERVAL, remaining))


def _candidate_is_pending(run: Mapping[str, Any]) -> bool:
    return run.get("status") != "completed"


def wait_candidate(
    run_id: str,
    output: Path,
    *,
    client: GitHubClient,
    monotonic: Callable[[], float] | None = None,
    sleeper: Callable[[float], None] | None = None,
    max_wait: float = MAX_WAIT,
) -> dict[str, Any]:
    run_id = _positive_id(run_id, "candidate run ID")
    now = monotonic or client.monotonic
    sleep = sleeper or client.sleeper
    deadline = now() + max_wait
    endpoint = f"/repos/{client.repository}/actions/runs/{run_id}"
    while True:
        if now() >= deadline:
            raise HandoffError("timed out waiting for candidate run")
        try:
            run = client.request_json(endpoint, deadline=deadline)
        except GitHubHTTPError as error:
            if not error.retryable:
                raise
            run = None
        except GitHubNetworkError:
            run = None
        if run is not None:
            _run_identity(
                run,
                client.repository,
                expected_id=run_id,
                workflow=CANDIDATE_WORKFLOW,
                events=("workflow_dispatch", "workflow_run"),
                require_status=False,
            )
            if not _candidate_is_pending(run):
                _run_identity(
                    run,
                    client.repository,
                    expected_id=run_id,
                    workflow=CANDIDATE_WORKFLOW,
                    events=("workflow_dispatch", "workflow_run"),
                )
                _write_atomic(output, _json_bytes(run))
                return run
        remaining = deadline - now()
        if remaining <= 0:
            raise HandoffError("timed out waiting for candidate run")
        sleep(min(POLL_INTERVAL, remaining))


def _download_artifact(
    run_id: str,
    repository: str,
    artifact: str,
    directory: Path,
    token: str,
    *,
    runner: Callable[..., Any] | None = None,
    timeout: float,
) -> None:
    child_env = os.environ.copy()
    child_env["GH_TOKEN"] = token
    run = runner or subprocess.run
    try:
        result = run(
            [
                "gh",
                "run",
                "download",
                run_id,
                "--repo",
                repository,
                "--name",
                artifact,
                "--dir",
                str(directory),
            ],
            env=child_env,
            capture_output=True,
            text=True,
            timeout=min(HTTP_TIMEOUT, timeout),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HandoffError("gh run download failed") from error
    if getattr(result, "returncode", 1) != 0:
        raise HandoffError("gh run download failed")


def _report_candidate_matches(value: Any, candidate_id: str) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0 and str(value) == str(int(candidate_id))
    return isinstance(value, str) and ID_RE.fullmatch(value) is not None and int(value) == int(candidate_id)


def _validate_report_bytes(raw: bytes, candidate_id: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HandoffError("QEMU validation report is invalid JSON") from error
    if not isinstance(value, dict):
        raise HandoffError("QEMU validation report must be a JSON object")
    if value.get("validation_target") != "qemu-system-riscv64":
        raise HandoffError("QEMU validation report target is unexpected")
    if not _report_candidate_matches(value.get("candidate_run_id"), candidate_id):
        raise HandoffError("QEMU validation report candidate run ID does not match")
    return value


def _check_validation_run(
    run: Mapping[str, Any], repository: str, validation_id: str, attempt: str
) -> None:
    _run_identity(
        run,
        repository,
        expected_id=validation_id,
        workflow=QEMU_WORKFLOW,
        events=("workflow_dispatch",),
    )
    if _json_positive_id(run.get("run_attempt"), "validation run attempt") != attempt:
        raise HandoffError("GitHub validation run attempt does not match")


def _wait_validation_attempt(
    validation_id: str,
    attempt: str,
    *,
    client: GitHubClient,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    max_wait: float = MAX_WAIT,
) -> dict[str, Any]:
    deadline = now() + max_wait
    endpoint = (
        f"/repos/{client.repository}/actions/runs/{validation_id}/attempts/{attempt}"
    )
    while True:
        if now() >= deadline:
            raise HandoffError("timed out waiting for validation run attempt")
        try:
            run = client.request_json(endpoint, deadline=deadline)
        except GitHubHTTPError as error:
            if not error.retryable:
                raise
            run = None
        except GitHubNetworkError:
            run = None
        if run is not None:
            _run_identity(
                run,
                client.repository,
                expected_id=validation_id,
                workflow=QEMU_WORKFLOW,
                events=("workflow_dispatch",),
                require_status=False,
            )
            if run.get("status") == "completed":
                _check_validation_run(run, client.repository, validation_id, attempt)
                return run
        remaining = deadline - now()
        if remaining <= 0:
            raise HandoffError("timed out waiting for validation run attempt")
        sleep(min(POLL_INTERVAL, remaining))


def _actor_is_bot(actor: str) -> bool:
    return not actor or BOT_RE.search(actor) is not None


def _check_manual_authorization(
    client: GitHubClient,
    env: Mapping[str, str],
    *,
    deadline: float,
) -> None:
    if env.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        raise HandoffError("manual validation report requires workflow_dispatch")
    actor = env.get("GITHUB_ACTOR", "")
    if _actor_is_bot(actor):
        raise HandoffError("manual validation report requires a human actor")
    actor_path = urllib.parse.quote(actor, safe="")
    permission = client.retry_json(
        f"/repos/{client.repository}/collaborators/{actor_path}/permission",
        deadline=deadline,
        retry_not_found=False,
    )
    level = permission.get("permission")
    if level not in {"write", "maintain", "admin"}:
        raise HandoffError("actor lacks required collaborator permission")
    user = permission.get("user")
    user_type = user.get("type") if isinstance(user, Mapping) else None
    if user_type is None:
        user = client.retry_json(
            f"/users/{actor_path}", deadline=deadline, retry_not_found=False
        )
        user_type = user.get("type")
    if user_type != "User":
        raise HandoffError("actor is not a human GitHub user")


def _decode_manual_report(value: str) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise HandoffError("manual validation report is not valid base64") from error
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HandoffError("manual validation report is invalid JSON") from error
    if not isinstance(decoded, dict):
        raise HandoffError("manual validation report must be a JSON object")
    return raw


def prepare_report(
    candidate_run_id: str,
    validation_run_id: str,
    validation_run_attempt: str,
    report_b64: str,
    output: Path,
    *,
    client: GitHubClient,
    env: Mapping[str, str],
    now: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    runner: Callable[..., Any] | None = None,
    max_wait: float = MAX_WAIT,
) -> bytes:
    candidate_run_id = _positive_id(candidate_run_id, "candidate run ID")
    auto_id = bool(validation_run_id)
    auto_attempt = bool(validation_run_attempt)
    if auto_id != auto_attempt:
        raise HandoffError("validation run ID and attempt must be supplied together")
    if auto_id and report_b64:
        raise HandoffError("choose automatic validation evidence or manual report")
    if not auto_id and not report_b64:
        raise HandoffError("one validation evidence source is required")
    if auto_id:
        validation_run_id = _positive_id(validation_run_id, "validation run ID")
        validation_run_attempt = _positive_id(
            validation_run_attempt, "validation run attempt"
        )
        now_fn = now or client.monotonic
        sleep_fn = sleep or client.sleeper
        started = now_fn()
        _wait_validation_attempt(
            validation_run_id,
            validation_run_attempt,
            client=client,
            now=now_fn,
            sleep=sleep_fn,
            max_wait=max_wait,
        )
        temporary = Path(tempfile.mkdtemp(prefix="codex-qemu-validation."))
        try:
            remaining = max_wait - (now_fn() - started)
            if remaining <= 0:
                raise HandoffError("GitHub operation deadline exceeded")
            _download_artifact(
                validation_run_id,
                client.repository,
                f"qemu-validation-{validation_run_id}-{validation_run_attempt}",
                temporary,
                client.token,
                runner=runner,
                timeout=remaining,
            )
            report_path = temporary / "qemu-report.json"
            if report_path.is_symlink() or not report_path.exists():
                raise HandoffError("QEMU validation artifact must contain qemu-report.json")
            if not stat.S_ISREG(report_path.stat().st_mode):
                raise HandoffError("QEMU validation report must be a regular file")
            raw = report_path.read_bytes()
            _validate_report_bytes(raw, candidate_run_id)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
    else:
        deadline = (now or client.monotonic)() + max_wait
        _check_manual_authorization(client, env, deadline=deadline)
        raw = _decode_manual_report(report_b64)
    _write_atomic(output, raw)
    return raw


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    candidate = commands.add_parser("wait-candidate")
    candidate.add_argument("--run-id", required=True)
    candidate.add_argument("--output", type=Path, required=True)
    report = commands.add_parser("prepare-report")
    report.add_argument("--candidate-run-id", required=True)
    report.add_argument("--validation-run-id", default="")
    report.add_argument("--validation-run-attempt", default="")
    report.add_argument("--report-b64", default="")
    report.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        values, repository = _environment()
        token = values.get("GH_TOKEN") or values["GITHUB_TOKEN"]
        client = GitHubClient(token, repository)
        if args.command == "wait-candidate":
            wait_candidate(args.run_id, args.output, client=client)
        else:
            prepare_report(
                args.candidate_run_id,
                args.validation_run_id,
                args.validation_run_attempt,
                args.report_b64,
                args.output,
                client=client,
                env=values,
            )
    except (HandoffError, OSError) as error:
        print(f"error: {error}", file=os.sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
