#!/usr/bin/env bash
set -euo pipefail

echo "=== Jarvis build ==="

# Clone OpenJarvis fresh from upstream
if [ ! -d openjarvis/.git ]; then
  echo "Cloning OpenJarvis..."
  git clone --depth 1 https://github.com/open-jarvis/OpenJarvis.git openjarvis
else
  echo "OpenJarvis already present, skipping clone."
fi

# Apply anthropic SDK compatibility patch
echo "Applying cloud.py patch..."
cd openjarvis
python3 ../deploy/patch_cloud.py

# Install Python deps
echo "Installing dependencies..."
uv sync --extra server --extra inference-cloud --no-dev

echo "=== Build complete ==="
