"""Shared utilities for Logto verification scripts."""

import os
import sys
import requests
from pathlib import Path
from dotenv import load_dotenv


# Load shared platform credentials, then repo .env overrides
_repo_root = Path(__file__).parent.parent.parent
_shared_env = Path(os.environ.get("SHARED_ENV_FILE", Path.home() / ".agentic-apps.env"))
load_dotenv(_shared_env)  # shared platform credentials (loaded first)
load_dotenv(_repo_root / ".env")  # local overrides (won't overwrite existing)


def get_env(key: str) -> str:
    """Get a required environment variable or exit with an error."""
    value = os.environ.get(key)
    if not value:
        print(f"FAIL: Missing required environment variable: {key}")
        print("  Copy .env.example to .env and fill in real values.")
        sys.exit(1)
    return value


# Common config loaded once
LOGTO_ENDPOINT = get_env("LOGTO_ENDPOINT")
M2M_CLIENT_ID = get_env("LOGTO_M2M_APP_ID")
M2M_CLIENT_SECRET = get_env("LOGTO_M2M_APP_SECRET")
API_RESOURCE_INDICATOR = get_env("LOGTO_API_RESOURCE")

# Derived URLs
TOKEN_URL = f"{LOGTO_ENDPOINT}/oidc/token"
JWKS_URL = f"{LOGTO_ENDPOINT}/oidc/jwks"

# HTTP session with timeouts
session = requests.Session()
session.timeout = (5, 10)  # (connect, read) in seconds


def acquire_token(
    client_id: str = M2M_CLIENT_ID,
    client_secret: str = M2M_CLIENT_SECRET,
    resource: str = API_RESOURCE_INDICATOR,
    scope: str = "read:data write:data",
) -> requests.Response:
    """Acquire an M2M token via client credentials grant. Returns the raw response."""
    return session.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "resource": resource,
            "scope": scope,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


# Test result tracking
_results: list[tuple[str, bool, str]] = []


def reset_results() -> None:
    """Clear accumulated test results. Call at the start of each script."""
    _results.clear()


def record(test_name: str, passed: bool, detail: str = "") -> None:
    """Record a test result."""
    _results.append((test_name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  [{status}] {test_name}"
    if detail:
        msg += f" — {detail}"
    print(msg)


def summary(script_name: str) -> int:
    """Print a summary and return exit code (0 = all pass, 1 = any fail)."""
    total = len(_results)
    passed = sum(1 for _, p, _ in _results if p)
    failed = total - passed
    print(f"\n{'=' * 50}")
    print(f"  {script_name}: {passed}/{total} passed, {failed} failed")
    print(f"{'=' * 50}")
    return 0 if failed == 0 else 1
