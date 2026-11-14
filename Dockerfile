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
    libffi-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY requirements.txt pyproject.toml constraints-armv7.txt ./

# Pip cache is scoped per-architecture.
#
# The arm/v7 leg additionally pulls from piwheels. Ten dependencies publish no armv7 wheel
# on PyPI (PyNaCl, pycryptodome, ephem, brotli, cffi, MarkupSafe, librt, backports.zstd,
# sgmllib3k) and would otherwise compile from source under QEMU on every build. piwheels
# supplies prebuilt armv7 wheels; constraints-armv7.txt closes the last two version-skew
# gaps so the leg resolves entirely to wheels. See constraints-armv7.txt for measurements.
#
# Scoped to linux/arm/v7 deliberately: amd64 and arm64 keep resolving from PyPI alone, so
# neither the extra index nor the two held-back versions touch those images.
RUN --mount=type=cache,target=/root/.cache/pip,id=pip-$TARGETARCH \
    if [ "$TARGETPLATFORM" = "linux/arm/v7" ]; then \
        pip install --user \
            --extra-index-url https://www.piwheels.org/simple \
            -c constraints-armv7.txt \
            -r requirements.txt; \
    else \
        pip install --user -r requirements.txt; \
    fi

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
    libffi8 \
    && rm -rf /var/lib/apt/lists/*

# Non-root user with dialout group for serial port access.
RUN useradd -m -u 1000 -G dialout,tty meshcore && \
    mkdir -p /app /data/config /data/databases /data/logs /data/backups && \
    chown -R meshcore:meshcore /app /data

COPY --from=builder --chown=meshcore:meshcore /root/.local /home/meshcore/.local

WORKDIR /app

# Version label for web viewer footer (passed via --build-arg in CI).
ARG MESHCORE_BOT_VERSION
ENV MESHCORE_BOT_VERSION=${MESHCORE_BOT_VERSION}

COPY --chown=meshcore:meshcore . /app/

ENV PATH=/home/meshcore/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MESHCORE_BOT_CONFIG=/data/config/config.ini

# OCI image labels for supply-chain transparency.
LABEL org.opencontainers.image.title="meshcore-bot" \
      org.opencontainers.image.description="MeshCore Bot for mesh radio networks" \
      org.opencontainers.image.source="https://github.com/agessaman/meshcore-bot"

USER meshcore

# Liveness is a heartbeat the event loop refreshes. kill -0 only proved
# PID 1 existed, so a wedged loop stayed healthy. Radio loss is not a
# failure here. Shell form, so MESHCORE_BOT_CONFIG is expanded when the check
# runs and the add-on image can override it.
# start-period covers slow construction on a Pi, before start() first touches.
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD python3 -m modules.healthcheck --config "${MESHCORE_BOT_CONFIG:-/data/config/config.ini}"

CMD ["python3", "meshcore_bot.py", "--config", "/data/config/config.ini"]
