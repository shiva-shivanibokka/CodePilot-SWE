# The image the benchmark's Docker backend uses when no official SWE-bench image
# is given (`--backend docker` without `--image official`).
#
# From Autonomous-SWE-Agent's sandbox/Dockerfile.sandbox. Kept: the build
# toolchain and libraries old commits of popular Python projects need to
# compile their extensions, and pytest. Changed: the checkout is no longer
# copied in — it lives on the host and is bind-mounted at /workspace, and git
# runs on the host — so the /repo directory, the sweagent user and the git
# safe.directory setup are gone. On POSIX hosts the container runs as the host
# user (codepilot/sandbox/docker.py), so files it writes stay yours.
#
#   docker build -f deploy/bench.Dockerfile -t codepilot-bench:latest .
#
# The container starts with a network for setup (pip install) and is taken off
# every network before the agent's first command.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    coreutils \
    build-essential \
    libssl-dev \
    libffi-dev \
    libxml2-dev \
    libxslt1-dev \
    zlib1g-dev \
    libbz2-dev \
    libreadline-dev \
    libsqlite3-dev \
    liblzma-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir pytest setuptools wheel

WORKDIR /workspace
