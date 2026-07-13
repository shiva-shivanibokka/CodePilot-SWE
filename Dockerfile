# Main application image for the SWE Agent API + UI
FROM python:3.11-slim

# Install Docker CLI (for Docker-in-Docker sandbox spawning)
RUN apt-get update && apt-get install -y --no-install-recommends \
    docker.io \
    git \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer cache)
COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]"

# Copy source code
COPY . .

# Expose API + Prometheus metrics ports
EXPOSE 8000 9090

# Single worker: the in-memory task store isn't shared across workers.
# Scale out with Redis before raising this.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
