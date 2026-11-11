#!/usr/bin/env python3
"""Verify JWKS endpoint and JWT signature validation."""

import ssl
import sys
import certifi
import jwt
from jwt import PyJWKClient
from utils import (
    acquire_token,
    JWKS_URL,
    LOGTO_ENDPOINT,
    API_RESOURCE_INDICATOR,
    session,
    record,
    summary,
)

# PyJWKClient uses urllib (not requests), which needs CA certs explicitly on macOS
_ssl_context = ssl.create_default_context(cafile=certifi.where())


def test_jwks_fetch():
    """Verify the JWKS endpoint returns valid RSA signing keys."""
    resp = session.get(JWKS_URL)
    if resp.status_code != 200:
        record("test_jwks_fetch", False, f"HTTP {resp.status_code}")
        return

    data = resp.json()
    keys = data.get("keys", [])
    if not keys:
        record("test_jwks_fetch", False, "No keys in JWKS response")
        return

    # Check at least one key has the expected signing fields (RSA or EC)
    valid_key_found = False
    key_type = None
    for key in keys:
        if key.get("use") != "sig" or not key.get("kid"):
            continue
        kty = key.get("kty")
        if kty == "RSA" and key.get("n") and key.get("e"):
            valid_key_found = True
            key_type = "RSA"
            break
        if kty == "EC" and key.get("crv") and key.get("x") and key.get("y"):
            valid_key_found = True
            key_type = f"EC ({key.get('crv')})"
            break

    if valid_key_found:
        record("test_jwks_fetch", True, f"{len(keys)} key(s) found, type={key_type}")
    else:
        record(
            "test_jwks_fetch",
            False,
            f"{len(keys)} key(s) found but none have required sig fields",
        )


def test_token_signature_valid():
    """Acquire a token and validate its signature using JWKS public keys."""
    # Get a token
    resp = acquire_token()
    if resp.status_code != 200:
        record(
            "test_token_signature_valid",
            False,
            f"Could not acquire token: HTTP {resp.status_code}",
        )
        return

    access_token = resp.json().get("access_token")
    if not access_token:
        record("test_token_signature_valid", False, "No access_token in response")
        return

    try:
        # Use PyJWKClient to fetch and cache JWKS keys
        jwk_client = PyJWKClient(JWKS_URL, ssl_context=_ssl_context)
        signing_key = jwk_client.get_signing_key_from_jwt(access_token)

        # Decode and validate the token
        decoded = jwt.decode(
            access_token,
            signing_key.key,
            algorithms=["RS256", "ES256", "ES384", "EdDSA"],
            audience=API_RESOURCE_INDICATOR,
            issuer=f"{LOGTO_ENDPOINT}/oidc",
        )

        # Check we got some claims back
        if decoded.get("sub") and decoded.get("iss"):
            record("test_token_signature_valid", True, f"Signature valid, sub={decoded['sub']}")
        else:
            record("test_token_signature_valid", False, "Token decoded but missing expected claims")

    except jwt.exceptions.InvalidSignatureError:
        record("test_token_signature_valid", False, "Signature validation failed unexpectedly")
    except jwt.exceptions.PyJWTError as e:
        record("test_token_signature_valid", False, f"JWT error: {e}")


def test_token_signature_invalid():
    """Tamper with a token and verify signature validation fails."""
    # Get a valid token
    resp = acquire_token()
    if resp.status_code != 200:
        record(
            "test_token_signature_invalid",
            False,
            f"Could not acquire token: HTTP {resp.status_code}",
        )
        return

    access_token = resp.json().get("access_token")
    if not access_token:
        record("test_token_signature_invalid", False, "No access_token in response")
        return

    # Tamper with the signature segment to guarantee InvalidSignatureError
    parts = access_token.split(".")
    sig = parts[2]
    # Flip the first character of the signature
    tampered_char = "B" if sig[0] == "A" else "A"
    tampered_sig = tampered_char + sig[1:]
    tampered_token = f"{parts[0]}.{parts[1]}.{tampered_sig}"

    try:
        jwk_client = PyJWKClient(JWKS_URL, ssl_context=_ssl_context)
        signing_key = jwk_client.get_signing_key_from_jwt(tampered_token)

        jwt.decode(
            tampered_token,
            signing_key.key,
            algorithms=["RS256", "ES256", "ES384", "EdDSA"],
            audience=API_RESOURCE_INDICATOR,
            issuer=f"{LOGTO_ENDPOINT}/oidc",
        )
        # If we get here, the tampered token was accepted (bad!)
        record(
            "test_token_signature_invalid",
            False,
            "Tampered token was accepted — signature check failed",
        )
    except (jwt.exceptions.InvalidSignatureError, jwt.exceptions.DecodeError):
        record("test_token_signature_invalid", True, "Tampered token correctly rejected")


if __name__ == "__main__":
    print("=== verify_jwks.py ===\n")
    test_jwks_fetch()
    test_token_signature_valid()
    test_token_signature_invalid()
    sys.exit(summary("verify_jwks"))
