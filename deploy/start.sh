#!/usr/bin/env bash
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

# Write config from template, substituting env vars for secrets
mkdir -p "$HOME/.openjarvis"
python3 -c "
import os, re
with open('config/jarvis-config.toml') as f:
    content = f.read()
content = re.sub(r'\\\${(\w+)}', lambda m: os.environ.get(m.group(1), m.group(0)), content)
with open(os.path.expanduser('~/.openjarvis/config.toml'), 'w') as f:
    f.write(content)
print('Config written to ~/.openjarvis/config.toml')
"

# Start OpenJarvis on internal port 8001
echo "Starting OpenJarvis on internal port 8001..."
cd openjarvis
uv run jarvis serve --host 127.0.0.1 --port 8001 &
JARVIS_PID=$!

# Wait for OpenJarvis to be ready
echo "Waiting for OpenJarvis to start..."
for i in $(seq 1 30); do
    if curl -sf http://127.0.0.1:8001/health >/dev/null 2>&1; then
        echo "OpenJarvis is ready."
        break
    fi
    sleep 2
done

# Start proxy on external port (handles /call/me + forwards everything else)
echo "Starting proxy on port ${PORT:-8000}..."
cd ..
exec uv run --project openjarvis python deploy/proxy.py
