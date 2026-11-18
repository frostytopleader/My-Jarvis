#!/usr/bin/env bash
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

# Write config from template, substituting env vars for secrets
mkdir -p "$HOME/.openjarvis"

# Copy Andre's dossier to persistent volume so Jarvis can reference it
cp config/andre-dossier.md "$HOME/.openjarvis/andre-dossier.md"
echo "Dossier copied to ~/.openjarvis/andre-dossier.md"
python3 -c "
import os, re
with open('config/jarvis-config.toml') as f:
    content = f.read()
content = re.sub(r'\\\${(\w+)}', lambda m: os.environ.get(m.group(1), m.group(0)), content)
with open(os.path.expanduser('~/.openjarvis/config.toml'), 'w') as f:
    f.write(content)
print('Config written to ~/.openjarvis/config.toml')
"

# Start OSIRIS MCP bridge on port 8002 (must be up before OpenJarvis connects to it)
echo "Starting OSIRIS MCP bridge on port 8002..."
uv run --project openjarvis python deploy/osiris_bridge.py &

# Start custom tools MCP server on port 8003 (file read/write, calendar, trading, monitor)
echo "Starting custom tools server on port 8003..."
uv run --project openjarvis python deploy/tools_server.py &

# Start intel MCP server on port 8004 (OSINT, company intel, flight/maritime, satellite, patents)
echo "Starting intel server on port 8004..."
uv run --project openjarvis python deploy/intel_server.py &

# Start email server on port 8005 (Gmail: read inbox, send, search)
echo "Starting email server on port 8005..."
uv run --project openjarvis python deploy/email_server.py &

# Start Notion server on port 8006 (read/write Notion workspace)
echo "Starting Notion server on port 8006..."
uv run --project openjarvis python deploy/notion_server.py &

# Start browser server on port 8007 (real estate, job intel, gov portals, price alerts, LinkedIn)
echo "Starting browser server on port 8007..."
uv run --project openjarvis python deploy/browser_server.py &

# Start intel2 server on port 8008 (fear/greed, whale wallets, macro, space funding, exec moves, etc.)
echo "Starting intel2 server on port 8008..."
uv run --project openjarvis python deploy/intel2_server.py &

# Wait for each MCP server to be healthy before OpenJarvis connects
echo "Waiting for MCP servers to be ready..."
for port in 8002 8003 8004 8005 8006 8007 8008; do
    for i in $(seq 1 30); do
        if curl -sf "http://127.0.0.1:${port}/" \
               -H "Content-Type: application/json" \
               -d '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"healthcheck","version":"0.0.1"}},"id":0}' \
               >/dev/null 2>&1; then
            echo "  Port ${port} ready."
            break
        fi
        sleep 1
        if [ "$i" -eq 30 ]; then
            echo "  WARNING: port ${port} did not respond after 30s — continuing anyway."
        fi
    done
done

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
