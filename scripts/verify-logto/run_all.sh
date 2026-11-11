#!/usr/bin/env bash
# Run all Logto verification scripts. Exits non-zero on any failure.
# set -e intentionally omitted: all scripts must run to accumulate the full FAILED count

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Activate venv if it exists
if [ -d ".venv" ]; then
    source .venv/bin/activate
fi

echo "Running Logto verification scripts..."
echo ""

FAILED=0

python3 verify_m2m_token.py || FAILED=1
echo ""
python3 verify_jwks.py || FAILED=1
echo ""
python3 verify_token_introspection.py || FAILED=1

echo ""
if [ "$FAILED" -eq 0 ]; then
    echo "ALL VERIFICATION SCRIPTS PASSED"
else
    echo "SOME VERIFICATION SCRIPTS FAILED"
    exit 1
fi
