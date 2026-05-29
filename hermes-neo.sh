#!/usr/bin/env bash
# hermes-neo: Run hermes commands inside the hermes-neo Docker container
set -euo pipefail

CONTAINER="hermes-neo"

ensure_running() {
    if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
        echo "⚠️  Container '$CONTAINER' is not running. Starting..."
        docker start "$CONTAINER" 2>/dev/null
        sleep 3
    fi
}

T=""
if [ -t 1 ]; then T="t"; fi

# No args → interactive REPL
if [[ $# -eq 0 ]]; then
    ensure_running
    exec docker exec -it "$CONTAINER" hermes chat
fi

case "$1" in
    bash|shell)
        ensure_running
        exec docker exec -it "$CONTAINER" bash
        ;;
    logs)
        shift
        exec docker logs "$CONTAINER" "$@"
        ;;
    restart)
        docker restart "$CONTAINER"
        echo "✅ Container restarted"
        ;;
    status)
        echo "=== Container ==="
        docker ps --filter "name=$CONTAINER" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" 2>&1
        echo ""
        echo "=== Hermes ==="
        docker exec -i${T} "$CONTAINER" hermes version 2>/dev/null || echo "(not running)"
        ;;
    chat|model|fallback|secrets|migrate|gateway|proxy|lsp|setup|kanban|hooks|doctor|security|dump|debug|backup|checkpoints|import|config|pairing|skills|bundles|plugins|curator|memory|tools|computer-use|mcp|sessions|insights|claw|version|update|uninstall|acp|profile|completion|dashboard|cron|webhook|portal|auth|send|login|logout)
        ensure_running
        exec docker exec -i${T} "$CONTAINER" hermes "$@"
        ;;
    *)
        # Treat as a one-shot chat message
        ensure_running
        exec docker exec -i${T} "$CONTAINER" hermes -z "$*" chat
        ;;
esac
