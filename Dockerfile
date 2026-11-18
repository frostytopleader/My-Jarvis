FROM python:3.11-slim

WORKDIR /app

# System deps: git, curl, Node.js (for Playwright MCP via npx)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

# Copy repo
COPY . .

# Build: clone OpenJarvis, patch, sync deps
RUN bash deploy/build.sh

EXPOSE 8000

CMD ["bash", "deploy/start.sh"]
