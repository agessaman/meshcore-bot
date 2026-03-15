# Multi-stage build for meshcore-bot
# Supports: linux/amd64, linux/arm64 (RPi 4/5, 64-bit OS), linux/arm/v7 (RPi 3, 32-bit OS)

# ── builder stage ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

# TARGETPLATFORM is injected by BuildKit for each platform in the matrix.
# Useful for platform-specific build steps if needed in future.
ARG TARGETPLATFORM
ARG TARGETARCH

# Install build dependencies.
# apt cache mounts are scoped per-architecture to avoid cross-contamination.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked,id=apt-$TARGETARCH \
    --mount=type=cache,target=/var/lib/apt,sharing=locked,id=apt-lib-$TARGETARCH \
    apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY requirements.txt pyproject.toml ./

# Pip cache is scoped per-architecture.
RUN --mount=type=cache,target=/root/.cache/pip,id=pip-$TARGETARCH \
    pip install --user -r requirements.txt

# ── runtime stage ──────────────────────────────────────────────────────────
FROM python:3.11-slim

ARG TARGETARCH

# Runtime system packages.
# libbluetooth3 is available on amd64, arm64, and armhf (arm/v7).
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked,id=apt-$TARGETARCH \
    --mount=type=cache,target=/var/lib/apt,sharing=locked,id=apt-lib-$TARGETARCH \
    apt-get update && apt-get install -y --no-install-recommends \
    udev \
    libbluetooth3 \
    && rm -rf /var/lib/apt/lists/*

# Non-root user with dialout group for serial port access.
RUN useradd -m -u 1000 -G dialout,tty meshcore && \
    mkdir -p /app /data/config /data/databases /data/logs /data/backups && \
    chown -R meshcore:meshcore /app /data

COPY --from=builder /root/.local /home/meshcore/.local

WORKDIR /app

# Version label for web viewer footer (passed via --build-arg in CI).
ARG MESHCORE_BOT_VERSION
ENV MESHCORE_BOT_VERSION=${MESHCORE_BOT_VERSION}

COPY --chown=meshcore:meshcore . /app/

ENV PATH=/home/meshcore/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER meshcore

# OCI image labels for supply-chain transparency.
LABEL org.opencontainers.image.title="meshcore-bot" \
      org.opencontainers.image.description="MeshCore Bot for mesh radio networks" \
      org.opencontainers.image.source="https://github.com/agessaman/meshcore-bot"

# Health check: verify PID 1 (the bot process) is still alive.
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD ["sh", "-c", "kill -0 1"]

CMD ["python3", "meshcore_bot.py", "--config", "/data/config/config.ini"]
