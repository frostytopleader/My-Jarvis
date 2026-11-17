#!/usr/bin/env bash
set -euo pipefail

echo "=== Jarvis build ==="

# Init submodule (upstream OpenJarvis)
git submodule update --init --recursive

# Apply anthropic SDK compatibility patch
echo "Applying cloud.py patch..."
cd openjarvis
python3 ../deploy/patch_cloud.py

# Install Python deps
echo "Installing dependencies..."
uv sync --extra server --extra inference-cloud --no-dev

echo "=== Build complete ==="
