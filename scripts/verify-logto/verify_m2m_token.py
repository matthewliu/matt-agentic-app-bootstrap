#!/usr/bin/env python3
"""Verify M2M token acquisition via Logto's OAuth 2.0 client credentials flow."""

import sys
from utils import (
    acquire_token,
    record,
    summary,
)


def test_token_acquisition():
    """Happy path: acquire a token with valid credentials."""
    resp = acquire_token()
    if resp.status_code != 200:
        is_json = resp.headers.get("content-type", "").startswith("application/json")
        error_msg = (
            resp.json().get("error", "unknown")
            if is_json
            else f"HTTP {resp.status_code}"
        )
        record("test_token_acquisition", False, f"HTTP {resp.status_code}: {error_msg}")
        return None

    data = resp.json()

    # Check required fields
    access_token = data.get("access_token")
    token_type = data.get("token_type")
    expires_in = data.get("expires_in")

    if not access_token:
        record("test_token_acquisition", False, "Missing access_token in response")
        return None

    if token_type != "Bearer":
        record("test_token_acquisition", False, f"Expected token_type=Bearer, got {token_type}")
        return None

    if not isinstance(expires_in, int) or expires_in <= 0:
        record("test_token_acquisition", False, f"Invalid expires_in: {expires_in}")
        return None

    # Verify token looks like a JWT (3 dot-separated segments)
    parts = access_token.split(".")
    if len(parts) != 3:
        record(
            "test_token_acquisition",
            False,
            f"Token is not a valid JWT (has {len(parts)} parts)",
        )
        return None

    record("test_token_acquisition", True, f"token_type=Bearer, expires_in={expires_in}")
    return access_token


def test_invalid_credentials():
    """Error path: wrong client_secret should return 401."""
    resp = acquire_token(client_secret="wrong-secret-value")

    if resp.status_code == 401:
        data = resp.json()
        error = data.get("error", "")
        if error == "invalid_client":
            record("test_invalid_credentials", True, "401 with error=invalid_client")
        else:
            record(
                "test_invalid_credentials",
                False,
                f"401 but error={error}, expected invalid_client",
            )
    else:
        record(
            "test_invalid_credentials",
            False,
            f"Expected 401, got {resp.status_code}: {resp.text}",
        )


def test_invalid_resource():
    """Error path: non-existent resource indicator should return 400."""
    resp = acquire_token(resource="https://nonexistent.example.com/api")

    if resp.status_code == 400:
        data = resp.json()
        error = data.get("error", "")
        if error == "invalid_target":
            record("test_invalid_resource", True, "400 with error=invalid_target")
        else:
            record(
                "test_invalid_resource",
                False,
                f"400 but error={error}, expected invalid_target",
            )
    else:
        record("test_invalid_resource", False, f"Expected 400, got {resp.status_code}: {resp.text}")


if __name__ == "__main__":
    print("=== verify_m2m_token.py ===\n")
    test_token_acquisition()
    test_invalid_credentials()
    test_invalid_resource()
    sys.exit(summary("verify_m2m_token"))
