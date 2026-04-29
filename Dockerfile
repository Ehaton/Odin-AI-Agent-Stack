FROM python:3.12-slim

LABEL maintainer="Chad Fiaschetti"
LABEL description="Odin — BeanLab AI Assistant"

# System deps: ssh client for paramiko, ping for host checks, openssl for certs
RUN apt-get update && apt-get install -y \
    openssh-client \
    iputils-ping \
    curl \
    openssl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY Odin.py .
COPY model_registry.py .
COPY models.yaml .
COPY hosts.json .
COPY orchestrating_engine.py .

# Copy tool modules
COPY tools/ ./tools/

# Copy Odin's identity/self directory
COPY Odins_Self/ ./Odins_Self/

# Copy TLS certs for Tailscale HTTPS
COPY *.crt *.key ./

# Data directories (will be bind-mounted or volume-mounted in prod)
RUN mkdir -p /app/generated_images /tmp/odinlogs

# Non-root user for safety (SSH keys need to be passed via volume)
RUN useradd -m -u 1000 odin && chown -R odin:odin /app
USER odin

EXPOSE 5050

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f -u "${ODIN_USER}:${ODIN_PASS}" http://localhost:5050/ || exit 1

CMD ["python", "Odin.py"]
