#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

FFMPEG_BIN="$(command -v ffmpeg || true)"
if [[ -z "$FFMPEG_BIN" ]]; then
  echo "Error: ffmpeg not found in PATH. Install ffmpeg first (e.g. brew install ffmpeg)."
  exit 1
fi

python3 -m pip install -r requirements.txt
python3 -m pip install pyinstaller

PYI_ARGS=(
  --noconfirm
  --windowed
  --name "VinylFlow"
  --hidden-import "backend.api"
  --add-binary "$FFMPEG_BIN:ffmpeg_bin"
  --add-data "backend/static:backend/static"
)

if [[ -d "config" ]]; then
  PYI_ARGS+=(--add-data "config:config")
fi

pyinstaller "${PYI_ARGS[@]}" desktop_launcher.py

echo "Build complete: dist/VinylFlow.app"
