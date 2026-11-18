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

# Install Playwright MCP and browsers for agentic web actions
echo "Installing Playwright MCP..."
cd ..
npm install -g @playwright/mcp@latest
npx playwright install chromium --with-deps

# Install Python Playwright + PDF support for custom browser tools
echo "Installing Python browser tool deps..."
uv run --project openjarvis pip install playwright pypdf
uv run --project openjarvis python -m playwright install chromium

echo "=== Build complete ==="
