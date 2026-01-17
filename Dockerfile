# Multi-stage build for meshcore-bot
FROM python:3.11-slim AS builder

# Install build dependencies (with cache mount for apt)
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /build

# Copy dependency files
COPY requirements.txt pyproject.toml ./

# Install Python dependencies (with cache mount for pip)
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --user -r requirements.txt

# Final stage
FROM python:3.11-slim

# Install runtime dependencies (with cache mount for apt)
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    # For serial port access
    udev \
    # For BLE support (optional, but commonly needed)
    libbluetooth3 \
    # Cleanup
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd -m -u 1000 meshcore && \
    mkdir -p /app /data/config /data/databases /data/logs /data/backups && \
    chown -R meshcore:meshcore /app /data

# Copy Python dependencies from builder
COPY --from=builder /root/.local /home/meshcore/.local

# Set working directory
WORKDIR /app

# Copy application files
COPY --chown=meshcore:meshcore . /app/

# Set PATH to include user's local bin
ENV PATH=/home/meshcore/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Switch to non-root user
USER meshcore

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python3 -c "import sys; sys.exit(0)" || exit 1

# Default command
CMD ["python3", "meshcore_bot.py", "--config", "/data/config/config.ini"]
