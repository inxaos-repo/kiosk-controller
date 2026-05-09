# syntax=docker/dockerfile:1.7

# =============================================================================
# kiosk-controller — HA-driven kiosk wake/reload daemon
# Single-file Python aiohttp daemon; Alpine base for small footprint.
# =============================================================================

FROM python:3.12-alpine AS runtime

# Build deps for cryptography (asyncssh C extension)
RUN apk add --no-cache \
    gcc \
    musl-dev \
    libffi-dev \
    openssl-dev

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY kiosk_controller/ ./kiosk_controller/

# Config and env are volume-mounted at runtime from /etc/kiosk-controller/
ENV KIOSK_CONFIG_PATH=/etc/kiosk-controller/kiosks.yaml
ENV KIOSK_CONTROLLER_PORT=18080
ENV KIOSK_CONTROLLER_HOST=0.0.0.0

EXPOSE 18080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO- http://localhost:18080/healthz || exit 1

CMD ["python", "-m", "kiosk_controller.main"]
