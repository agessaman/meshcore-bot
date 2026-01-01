# ============================================
# MeshCore Bot Dockerfile
# Supports: Serial, TCP, and BLE connections
# ============================================

# Build stage - install dependencies
FROM python:3.11-slim-bookworm AS builder

WORKDIR /build

# Install build dependencies for compiled packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# ============================================
# Runtime stage
# ============================================
FROM python:3.11-slim-bookworm AS runtime

# Labels for image metadata
LABEL org.opencontainers.image.title="MeshCore Bot"
LABEL org.opencontainers.image.description="Python bot for MeshCore mesh networks"
LABEL org.opencontainers.image.source="https://github.com/agessaman/meshcore-bot"

# Install runtime dependencies
# - libglib2.0-0: Required for BLE (bleak)
# - bluez: Bluetooth stack for BLE connections
# - dbus: D-Bus for BlueZ communication
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    bluez \
    dbus \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Set working directory
WORKDIR /app

# Copy Python packages from builder
COPY --from=builder /root/.local /root/.local

# Copy application files
COPY meshcore_bot.py .
COPY modules/ ./modules/
COPY translations/ ./translations/
COPY config.ini.example .
COPY backup_database.py .

# Copy entrypoint script
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Create directories for persistent data
RUN mkdir -p /app/data /app/logs

# Add Python user packages to PATH
ENV PATH=/root/.local/bin:$PATH
ENV PYTHONUNBUFFERED=1

# Default environment variables (can be overridden)
ENV MESHCORE_CONNECTION_TYPE=serial
ENV MESHCORE_SERIAL_PORT=/dev/ttyUSB0
ENV MESHCORE_TCP_HOST=
ENV MESHCORE_TCP_PORT=5000
ENV MESHCORE_WEB_ENABLED=true
ENV MESHCORE_WEB_HOST=0.0.0.0
ENV MESHCORE_WEB_PORT=8080
ENV MESHCORE_WEB_AUTOSTART=true

# Expose web viewer port
EXPOSE 8080

# Volume mount points
VOLUME ["/app/data", "/app/logs"]

# Health check - verify Python process is running
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD pgrep -f meshcore_bot.py || exit 1

# Set entrypoint
ENTRYPOINT ["/entrypoint.sh"]

# Default command
CMD ["python", "meshcore_bot.py"]
