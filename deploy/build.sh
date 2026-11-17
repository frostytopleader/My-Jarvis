#!/usr/bin/env bash
set -euo pipefail

echo "=== Jarvis build ==="

# Install uv if not present
if ! command -v uv &>/dev/null; then
  echo "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

# Install gettext for envsubst (used in start.sh)
if ! command -v envsubst &>/dev/null; then
  echo "Installing gettext..."
  apt-get install -y gettext-base 2>/dev/null || true
fi

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
