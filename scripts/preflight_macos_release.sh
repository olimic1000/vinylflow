#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

APP_PATH="${APP_PATH:-dist/VinylFlow.app}"
SIGN_IDENTITY="${MACOS_SIGN_IDENTITY:-}"
NOTARY_PROFILE="${APPLE_NOTARY_PROFILE:-}"

ok=true

check_cmd() {
  local cmd="$1"
  if command -v "$cmd" >/dev/null 2>&1; then
    echo "✓ Found command: $cmd"
  else
    echo "✗ Missing command: $cmd"
    ok=false
  fi
}

echo "== VinylFlow macOS release preflight =="

check_cmd codesign
check_cmd spctl
check_cmd xcrun
check_cmd python3

if xcrun --find notarytool >/dev/null 2>&1; then
  echo "✓ Found command: notarytool"
else
  echo "✗ Missing command: notarytool (install/update Xcode command line tools)"
  ok=false
fi

if [[ -d "$APP_PATH" ]]; then
  echo "✓ App bundle present: $APP_PATH"
else
  echo "! App bundle not found at $APP_PATH"
  echo "  Build first with: bash scripts/build_desktop_macos.sh"
fi

if [[ -n "$SIGN_IDENTITY" ]]; then
  if security find-identity -v -p codesigning | grep -F "$SIGN_IDENTITY" >/dev/null 2>&1; then
    echo "✓ Signing identity available in keychain"
  else
    echo "✗ MACOS_SIGN_IDENTITY is set, but not found in keychain"
    ok=false
  fi
else
  echo "! MACOS_SIGN_IDENTITY not set"
  echo "  export MACOS_SIGN_IDENTITY='Developer ID Application: YOUR NAME (TEAMID)'"
fi

if [[ -n "$NOTARY_PROFILE" ]]; then
  if xcrun notarytool history --keychain-profile "$NOTARY_PROFILE" >/dev/null 2>&1; then
    echo "✓ Notary profile is valid: $NOTARY_PROFILE"
  else
    echo "✗ APPLE_NOTARY_PROFILE is set, but not valid: $NOTARY_PROFILE"
    ok=false
  fi
else
  echo "! APPLE_NOTARY_PROFILE not set (notarization will be skipped)"
fi

if [[ "$ok" == true ]]; then
  echo "\nPreflight passed."
  exit 0
fi

echo "\nPreflight found blocking issues."
exit 1
