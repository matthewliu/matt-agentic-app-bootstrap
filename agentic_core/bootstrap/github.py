"""``gh`` CLI wrapper for the bootstrap orchestrator.

This module wraps the ``gh`` GitHub CLI in two narrow operations the
scaffold needs: ``create_github_repo`` and ``set_repo_secret``. Both
return a structured ``dict`` (success or failure envelope) and never
let a ``TimeoutExpired`` traceback escape — the orchestrator's ``--json``
stdout contract is one JSON object per run, and unhandled exceptions
would corrupt it. (``subprocess.run`` is not called with ``check=True``,
so non-zero exits surface via ``result.returncode`` rather than as
``CalledProcessError``.)

Design choices (load-bearing for agent callers):

- All argv passed to ``subprocess.run`` as a list — never ``shell=True``.
  Keeps the audit trail clean and forecloses shell-injection on slug
  inputs (slug regex is validated at Typer entry but defense-in-depth).
- Default timeout: 60s. The task spec says "default 60s for repo
  creation; document override" — callers pass ``timeout=N`` to widen.
- Secrets are NEVER passed via argv. ``gh secret set`` is invoked with
  ``--body -`` (stdin) so the value doesn't show up in ``ps``.
- Returned dicts never contain the secret value. Callers can log the
  whole dict safely.

Failure envelope shape (matches §R3 of the spec, scoped to this wrapper):

    {
      "status": "failed",
      "error_code": "GH_AUTH_FAILED" | "GH_CLI_ERROR" | "GH_TIMEOUT",
      "exit_code": <int or None>,
      "error_message": <stderr or exception summary, secrets redacted>,
    }
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

# Default subprocess timeout in seconds. The task spec calls out 60s for
# repo/project creation; secret-set is similarly bounded (network round-trip).
DEFAULT_TIMEOUT = 60


def _classify_gh_error(stderr: str) -> str:
    """Map stderr text to a stable error_code string.

    Right now there's exactly one specialised case (auth) — everything else
    is the generic ``GH_CLI_ERROR``. Adding more classifications later is
    additive (agent callers read the code string).
    """
    lower = stderr.lower()
    if "gh auth" in lower or "authentication" in lower or "not logged" in lower:
        return "GH_AUTH_FAILED"
    return "GH_CLI_ERROR"


def _fail(*, error_code: str, exit_code: int | None, message: str) -> dict[str, Any]:
    return {
        "status": "failed",
        "error_code": error_code,
        "exit_code": exit_code,
        "error_message": message,
    }


def logged_in_login(timeout: int = 15) -> str | None:
    """Return the login of the account ``gh`` uses, or ``None``.

    ``None`` covers every failure (no ``gh``, not logged in, timeout). The
    caller then passes a bare repo name, and ``gh`` creates the repo under
    the logged-in account anyway.
    """
    try:
        result = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    login = result.stdout.strip()
    return login if result.returncode == 0 and login else None


def repo_from_url(url: str) -> str | None:
    """Return ``owner/repo`` from ``https://github.com/owner/repo``, else ``None``."""
    prefix = "https://github.com/"
    if not url.startswith(prefix):
        return None
    parts = url[len(prefix):].rstrip("/").removesuffix(".git").split("/")
    return "/".join(parts) if len(parts) == 2 and all(parts) else None


def create_github_repo(
    name: str,
    description: str = "",
    private: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Create a GitHub repo via ``gh repo create``.

    Parameters
    ----------
    name : str
        Repo slug in ``owner/repo`` form (or bare ``repo`` if the gh CLI
        is logged in as the owner). Passed verbatim to gh.
    description : str
        Repo description. Empty string ⇒ ``--description`` flag is omitted
        entirely (gh accepts no description rather than an empty one).
    private : bool
        ``True`` ⇒ ``--private``; ``False`` ⇒ ``--public``. Visibility must
        always be set explicitly to avoid gh's interactive prompt.
    timeout : int
        Subprocess timeout in seconds. Default 60.

    Returns
    -------
    dict
        Success: ``{"status": "success", "repo": <name>, "url": <stdout url>}``.
        Failure: see module docstring failure envelope.
    """
    argv: list[str] = ["gh", "repo", "create", name]
    if private:
        argv.append("--private")
    else:
        argv.append("--public")
    if description:
        argv.extend(["--description", description])

    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _fail(
            error_code="GH_TIMEOUT",
            exit_code=None,
            message=f"gh repo create timeout after {timeout}s",
        )

    if result.returncode != 0:
        # Idempotent adopt-on-resume: if the repo already exists (a prior
        # partial run created it, or --resume after this step), adopt it
        # instead of failing — mirrors the Logto/Railway find-then-create
        # pattern so a mid-pipeline failure downstream of STEP_GITHUB is
        # recoverable without deleting the repo.
        if "already exists" in result.stderr.lower():
            try:
                view = subprocess.run(
                    ["gh", "repo", "view", name, "--json", "url", "--jq", ".url"],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                return _fail(
                    error_code="GH_TIMEOUT",
                    exit_code=None,
                    message=f"gh repo view timeout after {timeout}s",
                )
            if view.returncode == 0 and view.stdout.strip():
                return {
                    "status": "success",
                    "repo": name,
                    "url": view.stdout.strip().splitlines()[-1],
                    "adopted": True,
                }
            # Couldn't confirm the existing repo — fall through to failure.
        return _fail(
            error_code=_classify_gh_error(result.stderr),
            exit_code=result.returncode,
            message=result.stderr.strip(),
        )

    # gh prints the new repo URL on stdout on success.
    url = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    return {
        "status": "success",
        "repo": name,
        "url": url,
    }


def initialize_and_push_repo(
    *,
    cwd: str | Path,
    repo_url: str,
    commit_message: str,
    env: dict[str, str],
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Create the initial commit and push it through an HTTPS remote.

    ``gh repo create`` does not configure a remote unless ``--source`` is
    supplied. The old orchestrator added an SSH remote manually, ignored every
    Git exit code, and marked the step complete even when ``git push`` timed
    out. Use the HTTPS URL returned by GitHub and fail the bootstrap loudly on
    any Git error instead.

    The operation is resume-safe: an existing ``origin`` is updated, and an
    existing commit is reused when the working tree has nothing new to commit.
    """
    # State from an older bootstrap may still contain the SSH form. Normalize
    # the supported GitHub SSH spellings so resume cannot reintroduce an
    # ssh-agent prompt; reject unknown/non-HTTPS transports instead of trusting
    # them in an unattended pipeline.
    if repo_url.startswith("git@github.com:"):
        repo_url = "https://github.com/" + repo_url.removeprefix(
            "git@github.com:"
        ).removesuffix(".git")
    elif repo_url.startswith("ssh://git@github.com/"):
        repo_url = "https://github.com/" + repo_url.removeprefix(
            "ssh://git@github.com/"
        ).removesuffix(".git")
    elif not repo_url.startswith("https://"):
        return _fail(
            error_code="GH_GIT_UNSAFE_REMOTE",
            exit_code=None,
            message=f"refusing non-HTTPS Git remote: {repo_url}",
        )

    git_env = {**env, "GIT_TERMINAL_PROMPT": "0"}

    def _git(*args: str) -> subprocess.CompletedProcess[str] | dict[str, Any]:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=cwd,
                env=git_env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return _fail(
                error_code="GH_GIT_TIMEOUT",
                exit_code=None,
                message=f"git {' '.join(args)} timed out after {timeout}s",
            )
        if result.returncode != 0:
            return _fail(
                error_code="GH_GIT_ERROR",
                exit_code=result.returncode,
                message=(result.stderr or result.stdout).strip(),
            )
        return result

    for args in (("init",), ("add", "--all")):
        result = _git(*args)
        if isinstance(result, dict):
            return result

    try:
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=cwd,
            env=git_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _fail(
            error_code="GH_GIT_TIMEOUT",
            exit_code=None,
            message=f"git diff --cached --quiet timed out after {timeout}s",
        )
    if staged.returncode == 1:
        result = _git("commit", "-m", commit_message)
        if isinstance(result, dict):
            return result
    elif staged.returncode != 0:
        return _fail(
            error_code="GH_GIT_ERROR",
            exit_code=staged.returncode,
            message=(staged.stderr or staged.stdout).strip(),
        )
    else:
        # No staged changes is valid on resume only when a commit exists.
        result = _git("rev-parse", "--verify", "HEAD")
        if isinstance(result, dict):
            return result

    result = _git("branch", "-M", "main")
    if isinstance(result, dict):
        return result

    try:
        existing_remote = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=cwd,
            env=git_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _fail(
            error_code="GH_GIT_TIMEOUT",
            exit_code=None,
            message=f"git remote get-url origin timed out after {timeout}s",
        )
    remote_args = (
        ("remote", "set-url", "origin", repo_url)
        if existing_remote.returncode == 0
        else ("remote", "add", "origin", repo_url)
    )
    result = _git(*remote_args)
    if isinstance(result, dict):
        return result

    result = _git("push", "-u", "origin", "main")
    if isinstance(result, dict):
        return result
    return {"status": "success", "repo_url": repo_url}


def set_repo_secret(
    repo: str,
    name: str,
    value: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Set a repo secret via ``gh secret set``.

    The secret VALUE is passed via stdin (``--body -``), never argv,
    so it doesn't appear in ``ps`` listings or audit logs.

    Parameters
    ----------
    repo : str
        ``owner/repo`` slug.
    name : str
        Secret name (uppercase env-var convention).
    value : str
        Secret value. Never echoed back in the return dict.
    timeout : int
        Subprocess timeout in seconds.

    Returns
    -------
    dict
        Success: ``{"status": "success", "secret_name": <name>, "repo": <repo>}``.
        Failure: structured failure envelope (no secret value present).
    """
    argv: list[str] = [
        "gh",
        "secret",
        "set",
        name,
        "--repo",
        repo,
        # `--body -` tells gh to read the secret from stdin. This keeps the
        # secret out of argv and out of `ps`.
        "--body",
        "-",
    ]

    try:
        result = subprocess.run(
            argv,
            input=value,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _fail(
            error_code="GH_TIMEOUT",
            exit_code=None,
            message=f"gh secret set timeout after {timeout}s",
        )

    if result.returncode != 0:
        return _fail(
            error_code=_classify_gh_error(result.stderr),
            exit_code=result.returncode,
            message=result.stderr.strip(),
        )

    return {
        "status": "success",
        "secret_name": name,
        "repo": repo,
    }
