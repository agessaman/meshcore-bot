# Home Assistant add-on image.
#
# The base image already contains the repository at /app and the Python
# packages installed with pip --user for uid 1000. The Supervisor's /data
# and /config are root-owned, so this image runs as root.
#
# BuildKit sets TARGETARCH to amd64 or arm64. Home Assistant calls arm64
# aarch64; the matching stage records that name in io.hass.arch.
ARG BASE_IMAGE=ghcr.io/agessaman/meshcore-bot:latest
ARG BUILD_VERSION=dev

FROM ${BASE_IMAGE} AS addon-amd64
ARG BUILD_VERSION
LABEL io.hass.type="addon" \
      io.hass.version="${BUILD_VERSION}" \
      io.hass.arch="amd64"

FROM ${BASE_IMAGE} AS addon-arm64
ARG BUILD_VERSION
LABEL io.hass.type="addon" \
      io.hass.version="${BUILD_VERSION}" \
      io.hass.arch="aarch64"

ARG TARGETARCH=amd64
FROM addon-${TARGETARCH}

# Python disables the per-user site directory for uid 0, so put the base
# image's --user packages on PYTHONPATH explicitly.
USER root
ENV PYTHONPATH=/home/meshcore/.local/lib/python3.11/site-packages
# The inherited healthcheck expands this in the shell. The add-on config is
# /config/config.ini. Docker starts the check from the image environment, so
# setting the variable inside ha_addon.py would not reach it.
ENV MESHCORE_BOT_CONFIG=/config/config.ini

CMD ["python3", "-m", "modules.ha_addon"]
