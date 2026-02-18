#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

APP_PATH="${APP_PATH:-dist/VinylFlow.app}"
SIGN_IDENTITY="${MACOS_SIGN_IDENTITY:-}"
NOTARY_PROFILE="${APPLE_NOTARY_PROFILE:-}"
ZIP_PATH="${ZIP_PATH:-dist/VinylFlow-notarize.zip}"

if [[ ! -d "$APP_PATH" ]]; then
  echo "Error: app bundle not found at $APP_PATH"
  echo "Build first with: bash scripts/build_desktop_macos.sh"
  exit 1
fi

if [[ -z "$SIGN_IDENTITY" ]]; then
  echo "Error: MACOS_SIGN_IDENTITY is required."
  echo "Example: export MACOS_SIGN_IDENTITY='Developer ID Application: Your Name (TEAMID)'"
  exit 1
fi

echo "Signing app: $APP_PATH"
codesign --force --deep --options runtime --timestamp --sign "$SIGN_IDENTITY" "$APP_PATH"

echo "Verifying signature"
codesign --verify --deep --strict --verbose=2 "$APP_PATH"
spctl --assess --type execute --verbose=2 "$APP_PATH"

if [[ -z "$NOTARY_PROFILE" ]]; then
  echo "Notarization skipped (APPLE_NOTARY_PROFILE not set)."
  echo "To notarize, configure notarytool credentials and set APPLE_NOTARY_PROFILE."
  exit 0
fi

echo "Creating notarization zip: $ZIP_PATH"
rm -f "$ZIP_PATH"
ditto -c -k --sequesterRsrc --keepParent "$APP_PATH" "$ZIP_PATH"

echo "Submitting for notarization"
xcrun notarytool submit "$ZIP_PATH" --keychain-profile "$NOTARY_PROFILE" --wait

echo "Stapling notarization ticket"
xcrun stapler staple "$APP_PATH"

echo "Final Gatekeeper assessment"
spctl --assess --type execute --verbose=2 "$APP_PATH"

echo "Done: signed + notarized app at $APP_PATH"
