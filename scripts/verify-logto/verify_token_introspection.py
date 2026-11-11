#!/usr/bin/env python3
"""Verify JWT token claims (issuer, audience, expiry, required fields)."""

import sys
import time
import jwt
from utils import (
    acquire_token,
    LOGTO_ENDPOINT,
    API_RESOURCE_INDICATOR,
    record,
    summary,
)


def _get_token_and_response():
    """Helper: acquire a token and return (access_token, response_data) or (None, None)."""
    resp = acquire_token()
    if resp.status_code != 200:
        return None, None
    data = resp.json()
    return data.get("access_token"), data


def test_claims_present():
    """Verify all required claims are present in the JWT payload."""
    access_token, _ = _get_token_and_response()
    if not access_token:
        record("test_claims_present", False, "Could not acquire token")
        return

    # Decode without verification (signature tested separately in verify_jwks.py)
    try:
        payload = jwt.decode(access_token, options={"verify_signature": False})
    except jwt.exceptions.PyJWTError as e:
        record("test_claims_present", False, f"Could not decode token: {e}")
        return

    required_claims = ["jti", "sub", "iat", "exp", "client_id", "iss", "aud"]
    missing = [c for c in required_claims if c not in payload]

    if missing:
        record("test_claims_present", False, f"Missing claims: {', '.join(missing)}")
    else:
        record("test_claims_present", True, f"All {len(required_claims)} required claims present")


def test_token_expiry():
    """Verify token expiry is in the future and duration matches expires_in."""
    access_token, response_data = _get_token_and_response()
    if not access_token or not response_data:
        record("test_token_expiry", False, "Could not acquire token")
        return

    try:
        payload = jwt.decode(access_token, options={"verify_signature": False})
    except jwt.exceptions.PyJWTError as e:
        record("test_token_expiry", False, f"Could not decode token: {e}")
        return

    now = int(time.time())
    exp = payload.get("exp", 0)
    iat = payload.get("iat", 0)
    expires_in = response_data.get("expires_in", 0)

    if exp <= now:
        record("test_token_expiry", False, f"Token already expired: exp={exp}, now={now}")
        return

    if iat <= 0:
        record("test_token_expiry", False, f"Invalid iat claim: {iat}")
        return

    # Check duration matches (allow 2-second tolerance for clock skew)
    actual_duration = exp - iat
    if abs(actual_duration - expires_in) <= 2:
        record("test_token_expiry", True, f"exp-iat={actual_duration}s, expires_in={expires_in}s")
    else:
        record(
            "test_token_expiry",
            False,
            f"Duration mismatch: exp-iat={actual_duration}, expires_in={expires_in}",
        )


def test_issuer_audience():
    """Verify iss matches Logto OIDC issuer and aud matches the API Resource."""
    access_token, _ = _get_token_and_response()
    if not access_token:
        record("test_issuer_audience", False, "Could not acquire token")
        return

    try:
        payload = jwt.decode(access_token, options={"verify_signature": False})
    except jwt.exceptions.PyJWTError as e:
        record("test_issuer_audience", False, f"Could not decode token: {e}")
        return

    expected_issuer = f"{LOGTO_ENDPOINT}/oidc"
    actual_issuer = payload.get("iss", "")
    actual_audience = payload.get("aud", "")

    issuer_ok = actual_issuer == expected_issuer
    audience_ok = actual_audience == API_RESOURCE_INDICATOR

    if issuer_ok and audience_ok:
        record("test_issuer_audience", True, f"iss={actual_issuer}, aud={actual_audience}")
    else:
        details = []
        if not issuer_ok:
            details.append(f"iss: expected {expected_issuer}, got {actual_issuer}")
        if not audience_ok:
            details.append(f"aud: expected {API_RESOURCE_INDICATOR}, got {actual_audience}")
        record("test_issuer_audience", False, "; ".join(details))


if __name__ == "__main__":
    print("=== verify_token_introspection.py ===\n")
    test_claims_present()
    test_token_expiry()
    test_issuer_audience()
    sys.exit(summary("verify_token_introspection"))
