#!/usr/bin/env bash
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

# Write config from template, substituting env vars for secrets
mkdir -p "$HOME/.openjarvis"

# Use python to do env substitution (more portable than envsubst)
python3 -c "
import os, re, sys
with open('config/jarvis-config.toml') as f:
    content = f.read()
content = re.sub(r'\\\${(\\w+)}', lambda m: os.environ.get(m.group(1), m.group(0)), content)
with open(os.path.expanduser('~/.openjarvis/config.toml'), 'w') as f:
    f.write(content)
print('Config written to ~/.openjarvis/config.toml')
"

echo "Starting Jarvis on port ${PORT:-8000}..."
cd openjarvis
exec uv run jarvis serve --host 0.0.0.0 --port "${PORT:-8000}"
