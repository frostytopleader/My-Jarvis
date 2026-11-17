#!/usr/bin/env bash
# Start Jarvis locally -- injects credentials and launches the server
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CREDS="$HOME/.openjarvis/credentials.toml"

if [ ! -f "$CREDS" ]; then
  echo "ERROR: credentials file not found at $CREDS"
  exit 1
fi

eval "$(python3 -c "
import tomllib, os
with open('$CREDS', 'rb') as f:
    creds = tomllib.load(f)
for section in creds.values():
    if isinstance(section, dict):
        for k, v in section.items():
            if isinstance(v, str) and v:
                print(f'export {k}={repr(v)}')
")"

echo "Starting Jarvis server on port 8000..."
cd "$SCRIPT_DIR"
exec uv run --project openjarvis jarvis serve --host 0.0.0.0 --port 8000
