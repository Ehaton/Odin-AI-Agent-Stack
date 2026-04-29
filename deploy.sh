#!/bin/bash
# =============================================================================
# deploy.sh — BeanLab Odin Stack Deployment Script
# =============================================================================
# Run from /opt/Odin/ after copying updated files.
# Usage:
#   ./deploy.sh          — full deploy (build + start Docker container)
#   ./deploy.sh venv     — set up/update Python venv for dev mode only
#   ./deploy.sh restart  — restart Odin container without rebuild
#   ./deploy.sh dev      — run Odin directly in venv (dev mode)
# =============================================================================
set -euo pipefail

ODIN_DIR="/opt/Odin"
VENV_DIR="$ODIN_DIR/venv"
LOG_PREFIX="[deploy.sh]"

info()  { echo "$LOG_PREFIX ℹ️  $*"; }
ok()    { echo "$LOG_PREFIX ✅ $*"; }
warn()  { echo "$LOG_PREFIX ⚠️  $*"; }
error() { echo "$LOG_PREFIX ❌ $*" >&2; exit 1; }

cd "$ODIN_DIR" || error "Cannot cd to $ODIN_DIR"

# Load .env if present
if [ -f .env ]; then
    set -a; source .env; set +a
    info "Loaded .env"
fi

# ─── Sub-commands ─────────────────────────────────────────────────────────

setup_venv() {
    info "Setting up Python venv at $VENV_DIR..."
    if [ ! -d "$VENV_DIR" ]; then
        python3 -m venv "$VENV_DIR"
        ok "Venv created"
    fi
    "$VENV_DIR/bin/pip" install --upgrade pip -q
    "$VENV_DIR/bin/pip" install -r requirements.txt -q
    ok "Dependencies installed"
}

run_dev() {
    info "Starting Odin in dev mode (venv)..."
    warn "Using OLLAMA_HOST=http://localhost:11434 for dev"
    export OLLAMA_HOST="http://localhost:11434"
    export ODIN_ALLOW_NOAUTH="${ODIN_ALLOW_NOAUTH:-}"
    exec "$VENV_DIR/bin/python" Odin.py
}

restart_container() {
    info "Restarting Odin container..."
    docker compose restart odin
    ok "Container restarted"
    docker logs odin --tail 10
}

full_deploy() {
    info "Full deploy: building Odin Docker image..."

    # Ensure SSH keys exist for the container to use
    if [ ! -f /home/cfiaschetti/.ssh/id_ed25519 ] && [ ! -f /home/cfiaschetti/.ssh/id_rsa ]; then
        warn "No SSH keys found at /home/cfiaschetti/.ssh/ — Odin SSH tool will not work"
        warn "Generate with: ssh-keygen -t ed25519"
    fi

    # Rebuild just the Odin image
    docker compose build odin
    ok "Image built"

    # Start/recreate Odin container (leave other services untouched)
    docker compose up -d odin
    ok "Container started"

    # Wait for health
    info "Waiting for Odin to become healthy..."
    for i in {1..30}; do
        sleep 2
        status=$(docker inspect --format='{{.State.Health.Status}}' odin 2>/dev/null || echo "unknown")
        if [ "$status" = "healthy" ]; then
            ok "Odin is healthy!"
            break
        fi
        if [ "$i" -eq 30 ]; then
            warn "Odin health check timed out — check logs with: docker logs odin"
        fi
    done

    echo ""
    echo "  🌐 Odin is running at:"
    echo "     http://192.168.1.111:5050"
    echo "     https://ai-stack-420.tail7a9f9b.ts.net:5050  (if Tailscale cert present)"
    echo ""
    echo "  📋 Logs: docker logs -f odin"
    echo "  🔄 Restart: docker compose restart odin"
    echo "  🔁 Reload models (no restart): curl -X POST http://localhost:5050/api/models/reload"
}

# ─── Main ─────────────────────────────────────────────────────────────────

case "${1:-deploy}" in
    venv)
        setup_venv
        ;;
    dev)
        setup_venv
        run_dev
        ;;
    restart)
        restart_container
        ;;
    deploy|"")
        setup_venv   # Always keep venv in sync
        full_deploy
        ;;
    *)
        echo "Usage: $0 [deploy|venv|dev|restart]"
        exit 1
        ;;
esac
