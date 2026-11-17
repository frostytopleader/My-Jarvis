#!/usr/bin/env bash
set -euo pipefail

# Write config from template, substituting env vars for secrets
mkdir -p "$HOME/.openjarvis"
envsubst < config/jarvis-config.toml > "$HOME/.openjarvis/config.toml"

echo "Starting Jarvis on port ${PORT:-8000}..."
cd openjarvis
exec uv run jarvis serve --host 0.0.0.0 --port "${PORT:-8000}"
