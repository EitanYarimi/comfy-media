#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ -f config.env ]]; then
  # shellcheck disable=SC1091
  source config.env
fi

# Default: Google Drive "My Drive" for this account
: "${MEDIA_ROOT:=$HOME/Library/CloudStorage/GoogleDrive-eitanyarimi@gmail.com/My Drive}"
: "${PORT:=8080}"

export MEDIA_ROOT

if [[ ! -d "$MEDIA_ROOT" ]]; then
  echo "MEDIA_ROOT not found: $MEDIA_ROOT"
  echo "Copy config.env.example to config.env and set your Google Drive path."
  exit 1
fi

if ! python3 -c "import PIL" 2>/dev/null; then
  echo "Installing Python dependencies..."
  python3 -m pip install -r requirements.txt
fi

echo "Starting media server on port $PORT"
echo "MEDIA_ROOT=$MEDIA_ROOT"
exec python3 video_server.py "$PORT"
