#!/usr/bin/env python3
"""Patch OpenJarvis cloud.py: fix temperature parameter for anthropic SDK 1.x."""
import sys

path = "src/openjarvis/engine/cloud.py"
with open(path) as f:
    content = f.read()

old = '            create_kwargs["extra_body"] = {"temperature": temperature}'
new = ''  # Anthropic SDK rejects temperature both as extra_body and as a direct kwarg

count = content.count(old)
if count == 0:
    print("Patch already applied or target not found, skipping.")
    sys.exit(0)

content = content.replace(old, new)
with open(path, "w") as f:
    f.write(content)

print(f"Patched {count} occurrence(s) in {path}")
