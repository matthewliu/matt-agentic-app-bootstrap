"""Tests for ``agentic_core.bootstrap.github`` — gh CLI wrapper.

Stubbing strategy: patch ``subprocess.run`` at the import site
(``agentic_core.bootstrap.github.subprocess``). Every test asserts on the
exact ``argv`` list passed and on the structured return / failure dict —
never on raw subprocess output.

The wrappers MUST NOT raise on ``TimeoutExpired`` — they return a structured
failure dict. (``CalledProcessError`` is unreachable in the current wrappers
because ``subprocess.run`` is not called with ``check=True``; non-zero exits
are handled via ``returncode != 0`` branches.) Letting tracebacks escape
would poison the ``--json`` stdout contract that agent callers consume.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from agentic_core.bootstrap import github as gh


# ---------------------------------------------------------------------------
# create_github_repo
# ---------------------------------------------------------------------------


class TestCreateGithubRepo:
    def test_create_private_repo_invokes_gh_with_expected_argv(self):
        """Happy path: private repo creation passes the right argv to gh."""
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="https://github.com/example-owner/foo\n",
            stderr="",
        )
        with patch.object(gh.subprocess, "run", return_value=completed) as mock_run:
            result = gh.create_github_repo(
                "example-owner/foo", description="A test app", private=True
            )

        # First positional arg is the argv list.
        argv = mock_run.call_args.args[0]
        assert argv[:2] == ["gh", "repo", "create"][:2] or argv[:3] == [
            "gh",
            "repo",
            "create",
        ]
        # The repo name must appear literally; --private and --description
        # must be passed through (order is implementation-defined but flags
        # must all be present).
        assert "example-owner/foo" in argv
        assert "--private" in argv
        assert "--description" in argv
        # Value immediately follows the flag.
        assert argv[argv.index("--description") + 1] == "A test app"
        # Timeout is set (R-style explicit; default 60s per task spec).
        assert mock_run.call_args.kwargs.get("timeout") == 60

        assert result["status"] == "success"
        assert result["repo"] == "example-owner/foo"
        assert result["url"] == "https://github.com/example-owner/foo"

    def test_existing_repo_is_adopted_not_failed(self):
        """Idempotent: an already-existing repo is adopted via `gh repo view`
        (adopt-on-resume), not reported as a failure."""
        create = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="GraphQL: Name already exists on this account (createRepository)",
        )
        view = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="https://github.com/example-owner/my-sample-notes-app\n",
            stderr="",
        )
        with patch.object(
            gh.subprocess, "run", side_effect=[create, view]
        ) as mock_run:
            result = gh.create_github_repo("example-owner/my-sample-notes-app")

        # Second call is the adopt view.
        assert mock_run.call_count == 2
        assert mock_run.call_args_list[1].args[0][:3] == ["gh", "repo", "view"]
        assert result["status"] == "success"
        assert result["adopted"] is True
        assert result["url"] == "https://github.com/example-owner/my-sample-notes-app"

    def test_genuine_error_still_fails(self):
        """A non-'already exists' error is still a failure (no adopt)."""
        create = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="gh: some other error"
        )
        with patch.object(gh.subprocess, "run", return_value=create):
            result = gh.create_github_repo("example-owner/foo")
        assert result["status"] == "failed"

    def test_create_public_repo_uses_public_flag(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="https://github.com/u/r\n", stderr=""
        )
        with patch.object(gh.subprocess, "run", return_value=completed) as mock_run:
            gh.create_github_repo("u/r", private=False)

        argv = mock_run.call_args.args[0]
        assert "--public" in argv
        assert "--private" not in argv

    def test_create_repo_omits_description_when_empty(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="https://github.com/u/r\n", stderr=""
        )
        with patch.object(gh.subprocess, "run", return_value=completed) as mock_run:
            gh.create_github_repo("u/r")

        argv = mock_run.call_args.args[0]
        assert "--description" not in argv

    def test_auth_failure_returns_structured_failure_dict(self):
        """gh exits non-zero on auth failure — wrapper must NOT raise."""
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="gh: To get started, run: gh auth login\n",
        )
        with patch.object(gh.subprocess, "run", return_value=completed):
            result = gh.create_github_repo("u/r")

        assert result["status"] == "failed"
        assert result["error_code"] in {"GH_CLI_ERROR", "GH_AUTH_FAILED"}
        # Auth-specific error_code if "auth" appears in stderr.
        assert result["error_code"] == "GH_AUTH_FAILED"
        assert result["exit_code"] == 1
        # Should never leak raw subprocess fields the caller didn't ask for.
        assert "error_message" in result

    def test_generic_failure_returns_structured_failure_dict(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="HTTP 422: Validation Failed\n",
        )
        with patch.object(gh.subprocess, "run", return_value=completed):
            result = gh.create_github_repo("u/r")

        assert result["status"] == "failed"
        assert result["error_code"] == "GH_CLI_ERROR"
        assert result["exit_code"] == 1

    def test_timeout_returns_structured_failure_not_raise(self):
        """TimeoutExpired must become a structured failure, not a traceback."""
        with patch.object(
            gh.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["gh"], timeout=60),
        ):
            result = gh.create_github_repo("u/r")

        assert result["status"] == "failed"
        assert result["error_code"] == "GH_TIMEOUT"
        assert "timeout" in result["error_message"].lower()

    def test_timeout_override_passed_to_subprocess(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="https://github.com/u/r\n", stderr=""
        )
        with patch.object(gh.subprocess, "run", return_value=completed) as mock_run:
            gh.create_github_repo("u/r", timeout=120)

        assert mock_run.call_args.kwargs.get("timeout") == 120


class TestInitializeAndPushRepo:
    def test_uses_https_remote_and_checks_every_git_command(self, tmp_path):
        ok = lambda: subprocess.CompletedProcess(  # noqa: E731
            args=[], returncode=0, stdout="", stderr=""
        )
        no_remote = subprocess.CompletedProcess(
            args=[], returncode=2, stdout="", stderr="no such remote"
        )
        staged_changes = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr=""
        )
        with patch.object(
            gh.subprocess,
            "run",
            side_effect=[
                ok(),  # init
                ok(),  # add
                staged_changes,
                ok(),  # commit
                ok(),  # branch
                no_remote,
                ok(),  # remote add
                ok(),  # push
            ],
        ) as mock_run:
            result = gh.initialize_and_push_repo(
                cwd=tmp_path,
                repo_url="https://github.com/example-owner/foo",
                commit_message="Initial scaffold of foo",
                env={"PATH": "/usr/bin"},
            )

        assert result["status"] == "success"
        argv = [call.args[0] for call in mock_run.call_args_list]
        assert [
            "git", "remote", "add", "origin", "https://github.com/example-owner/foo"
        ] in argv
        assert argv[-1] == ["git", "push", "-u", "origin", "main"]
        assert all(
            call.kwargs["env"]
            == {"PATH": "/usr/bin", "GIT_TERMINAL_PROMPT": "0"}
            for call in mock_run.call_args_list
        )

    def test_push_failure_is_not_marked_success(self, tmp_path):
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        staged_changes = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr=""
        )
        no_remote = subprocess.CompletedProcess(
            args=[], returncode=2, stdout="", stderr="no such remote"
        )
        push_failed = subprocess.CompletedProcess(
            args=[], returncode=128, stdout="", stderr="connection refused"
        )
        with patch.object(
            gh.subprocess,
            "run",
            side_effect=[
                ok, ok, staged_changes, ok, ok, no_remote, ok, push_failed
            ],
        ):
            result = gh.initialize_and_push_repo(
                cwd=tmp_path,
                repo_url="https://github.com/example-owner/foo",
                commit_message="Initial scaffold of foo",
                env={},
            )

        assert result["status"] == "failed"
        assert result["error_code"] == "GH_GIT_ERROR"
        assert result["exit_code"] == 128

    def test_normalizes_legacy_github_ssh_remote_to_https(self, tmp_path):
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        no_remote = subprocess.CompletedProcess(
            args=[], returncode=2, stdout="", stderr="no such remote"
        )
        staged_changes = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr=""
        )
        with patch.object(
            gh.subprocess,
            "run",
            side_effect=[ok, ok, staged_changes, ok, ok, no_remote, ok, ok],
        ) as mock_run:
            result = gh.initialize_and_push_repo(
                cwd=tmp_path,
                repo_url="git@github.com:example-owner/foo.git",
                commit_message="Initial scaffold of foo",
                env={},
            )

        assert result == {
            "status": "success",
            "repo_url": "https://github.com/example-owner/foo",
        }
        assert [
            "git",
            "remote",
            "add",
            "origin",
            "https://github.com/example-owner/foo",
        ] in [call.args[0] for call in mock_run.call_args_list]


# ---------------------------------------------------------------------------
# set_repo_secret
# ---------------------------------------------------------------------------


class TestSetRepoSecret:
    def test_sets_secret_invokes_gh_with_expected_argv(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch.object(gh.subprocess, "run", return_value=completed) as mock_run:
            result = gh.set_repo_secret("u/r", "MY_SECRET", "sek-ret-value-1234")

        argv = mock_run.call_args.args[0]
        assert argv[:3] == ["gh", "secret", "set"]
        assert "MY_SECRET" in argv
        # The secret VALUE must NOT be in argv — gh accepts it via --body or
        # stdin. Either way, never on the command line (visible in `ps`).
        assert "sek-ret-value-1234" not in argv
        # Repo specified explicitly.
        assert "--repo" in argv
        assert argv[argv.index("--repo") + 1] == "u/r"
        # Either passed via stdin or via --body-file/env; check one of the
        # safer paths is taken.
        kwargs = mock_run.call_args.kwargs
        passed_via_stdin = kwargs.get("input") == "sek-ret-value-1234"
        passed_via_env = (
            isinstance(kwargs.get("env"), dict)
            and "sek-ret-value-1234" in kwargs["env"].values()
        )
        assert passed_via_stdin or passed_via_env, (
            "secret value must be passed via stdin or env, never argv "
            f"(kwargs={kwargs})"
        )
        assert result["status"] == "success"

    def test_failure_returns_structured_failure(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="not found\n"
        )
        with patch.object(gh.subprocess, "run", return_value=completed):
            result = gh.set_repo_secret("u/r", "NAME", "val")

        assert result["status"] == "failed"
        assert result["error_code"] == "GH_CLI_ERROR"

    def test_timeout_returns_structured_failure(self):
        with patch.object(
            gh.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["gh"], timeout=60),
        ):
            result = gh.set_repo_secret("u/r", "NAME", "val")

        assert result["status"] == "failed"
        assert result["error_code"] == "GH_TIMEOUT"

    def test_secret_value_never_in_returned_dict(self):
        """The returned dict must not echo the secret value, ever."""
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch.object(gh.subprocess, "run", return_value=completed):
            result = gh.set_repo_secret("u/r", "NAME", "the-secret-value-9988")

        # Whole serialized dict must not contain the secret value.
        assert "the-secret-value-9988" not in json.dumps(result)


class TestOwnerHelpers:
    def test_repo_from_url(self):
        assert gh.repo_from_url("https://github.com/a-b/c") == "a-b/c"
        assert gh.repo_from_url("https://github.com/a/c.git") == "a/c"
        assert gh.repo_from_url("https://github.com/a/c/") == "a/c"
        assert gh.repo_from_url("") is None
        assert gh.repo_from_url("https://github.com/only-owner") is None
        assert gh.repo_from_url("git@github.com:a/c.git") is None

    def test_logged_in_login_returns_none_on_failure(self):
        failed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="not logged in"
        )
        with patch.object(gh.subprocess, "run", return_value=failed):
            assert gh.logged_in_login() is None
        with patch.object(gh.subprocess, "run", side_effect=FileNotFoundError):
            assert gh.logged_in_login() is None

    def test_logged_in_login_reads_the_login(self):
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="friend-user\n", stderr="")
        with patch.object(gh.subprocess, "run", return_value=ok) as run:
            assert gh.logged_in_login() == "friend-user"
        assert run.call_args.args[0] == ["gh", "api", "user", "--jq", ".login"]
