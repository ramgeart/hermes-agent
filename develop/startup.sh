#!/bin/bash
# =============================================================================
# hermes-neo Agent Startup
# Initializes: DBus, Wayland (mutter), ydotool, Tailscale, Camofox, Hermes
# =============================================================================

set -e

echo "[hermes-neo] Starting agent container..."

# -------------------------------------------------------------------------
# 1. XDG Runtime Directory
# -------------------------------------------------------------------------
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"

# -------------------------------------------------------------------------
# 2. DBus (system + session buses)
# -------------------------------------------------------------------------
echo "[hermes-neo] Starting DBus..."
if [ ! -d /run/dbus ]; then
    mkdir -p /run/dbus
fi
dbus-daemon --system --fork 2>/dev/null || true
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
dbus-daemon --session --address="$DBUS_SESSION_BUS_ADDRESS" --fork --nopidfile 2>/dev/null || true

# -------------------------------------------------------------------------
# 3. Wayland Display (mutter headless)
# -------------------------------------------------------------------------
echo "[hermes-neo] Starting mutter (Wayland headless)..."
# Start mutter as a Wayland compositor in headless mode
dbus-run-session -- mutter --wayland --headless &
MUTTER_PID=$!

# Wait for Wayland socket
for i in $(seq 1 30); do
    if [ -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" ]; then
        echo "[hermes-neo] Wayland display ready: $WAYLAND_DISPLAY"
        break
    fi
    sleep 0.5
done

# -------------------------------------------------------------------------
# 4. ydotool daemon (input simulation for Wayland)
# -------------------------------------------------------------------------
# ydotool not installed in this image — skip for now
echo "[hermes-neo] ydotoold: skipped (not installed)"


# -------------------------------------------------------------------------
# 5. Tailscale (connect to tailnet)
# -------------------------------------------------------------------------
echo "[hermes-neo] Starting Tailscale..."
if [ -n "${TS_AUTHKEY:-}" ]; then
    mkdir -p "$TS_STATE_DIR"
    tailscaled --state="$TS_STATE_DIR/tailscaled.state" --statedir="$TS_STATE_DIR" &
    TAILSCALED_PID=$!
    sleep 3
    tailscale up         --authkey="$TS_AUTHKEY"         --hostname="${TS_HOSTNAME:-hermes-neo-agent}"         --accept-routes         --accept-dns         2>&1 || echo "[hermes-neo] WARNING: Tailscale up failed (continuing without tailnet)"
    echo "[hermes-neo] Tailscale IP: $(tailscale ip -4 2>/dev/null || echo 'not connected')"
else
    echo "[hermes-neo] TS_AUTHKEY not set — skipping Tailscale"
fi

# -------------------------------------------------------------------------
# 6. Computer-use-linux setup (GNOME accessibility + extension)
# -------------------------------------------------------------------------
echo "[hermes-neo] Setting up computer-use-linux..."
# Enable AT-SPI toolkit accessibility
gsettings set org.gnome.desktop.interface toolkit-accessibility true 2>/dev/null || true
# Setup window targeting (GNOME Shell extension)
computer-use-linux setup-window-targeting 2>/dev/null || echo "[hermes-neo] GNOME extension setup deferred"
# Run doctor for diagnostics
computer-use-linux doctor 2>/dev/null | jq -r '.readiness.summary' 2>/dev/null || echo "[hermes-neo] computer-use-linux doctor pending"

# -------------------------------------------------------------------------
# 7. Camofox Browser (anti-detection Firefox)
# -------------------------------------------------------------------------
echo "[hermes-neo] Starting Camofox Browser..."
mkdir -p /opt/data/browser/camofox-profile
# Start Camofox as a REST API server
camofox server start --port 9377 &
CAMOFOX_PID=$!
sleep 3
echo "[hermes-neo] Camofox Browser API: http://localhost:9377"

# -------------------------------------------------------------------------
# 8. Hermes home initialization
# -------------------------------------------------------------------------
if [ ! -f "$HERMES_HOME/config.yaml" ]; then
    echo "[hermes-neo] First run — initializing HERMES_HOME..."
    mkdir -p "$HERMES_HOME"/{memories,skills,plugins,logs,cron,profiles}
fi

# -------------------------------------------------------------------------
# 9. Exec the main command (hermes gateway start or custom)
# -------------------------------------------------------------------------
echo "[hermes-neo] ==========================================="
echo "[hermes-neo] Agent container ready"
echo "[hermes-neo]   HERMES_HOME:  $HERMES_HOME"
echo "[hermes-neo]   Wayland:      $WAYLAND_DISPLAY"
echo "[hermes-neo]   Camofox:      http://localhost:9377"
echo "[hermes-neo]   Tailscale:    $(tailscale ip -4 2>/dev/null || echo 'N/A')"
echo "[hermes-neo] ==========================================="

# Hand off to hermes (or custom command)
exec "$@"
