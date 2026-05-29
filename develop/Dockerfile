# =============================================================================
# hermes-neo Agent Container
# Base: python:3.14.5-trixie (Debian 13)
# Includes: GNOME Wayland, computer-use-linux, Camofox, Tailscale
# =============================================================================

# ---- Stage 1: Build tools ----
FROM ghcr.io/astral-sh/uv:0.11.6-python3.14-trixie AS uv_source
FROM node:22-bookworm-slim AS node_source

# ---- Stage 2: Runtime ----
FROM python:3.14.5-trixie

ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive

# -------------------------------------------------------------------------
# 1. System packages
# -------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Core tools
    ca-certificates curl wget gnupg2 \
    ripgrep ffmpeg procps htop \
    git openssh-client \
    gcc python3-dev libffi-dev pkg-config \
    # DBus (AT-SPI bus, desktop services)
    dbus dbus-x11 \
    # Wayland / GNOME desktop (computer-use-linux requirements)
    gnome-shell gnome-session mutter \
    at-spi2-core at-spi2-common libatspi2.0-0 \
    xdg-utils xdg-desktop-portal xdg-desktop-portal-gtk \
    # Accessibility (AT-SPI)
    libglib2.0-bin \
    # Misc
    xz-utils unzip jq \
    && rm -rf /var/lib/apt/lists/*

# -------------------------------------------------------------------------
# 2. Tailscale (agent connectivity — tailnet only)
# -------------------------------------------------------------------------
RUN curl -fsSL https://tailscale.com/install.sh | sh && \
    systemctl disable tailscaled  # managed by startup.sh

# -------------------------------------------------------------------------
# 3. Node.js 22 LTS
# -------------------------------------------------------------------------
COPY --chmod=0755 --from=node_source /usr/local/bin/node /usr/local/bin/
COPY --from=node_source /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
COPY --from=node_source /usr/local/lib/node_modules/corepack /usr/local/lib/node_modules/corepack
RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    ln -sf /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx && \
    ln -sf /usr/local/lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack

# -------------------------------------------------------------------------
# 4. UV (Python package manager)
# -------------------------------------------------------------------------
COPY --chmod=0755 --from=uv_source /usr/local/bin/uv /usr/local/bin/uvx /usr/local/bin/

# -------------------------------------------------------------------------
# 5. User setup (non-root)
# -------------------------------------------------------------------------
RUN useradd -m -d /home/hermes -s /bin/bash hermes && \
    mkdir -p /opt/data /opt/hermes-neo && \
    chown -R hermes:hermes /opt/data /opt/hermes-neo

WORKDIR /opt/hermes-neo

# -------------------------------------------------------------------------
# 6. Python dependencies (cached layer)
# -------------------------------------------------------------------------
COPY pyproject.toml uv.lock ./
RUN touch README.md
RUN uv sync --frozen --no-install-project \
    --extra all --extra messaging \
    --extra anthropic --extra bedrock \
    --extra voice --extra honcho

# -------------------------------------------------------------------------
# 7. Node.js dependencies (cached layer)
# -------------------------------------------------------------------------
COPY package.json package-lock.json ./
COPY web/package.json web/package-lock.json web/
COPY ui-tui/package.json ui-tui/package-lock.json ui-tui/
COPY ui-tui/packages/hermes-ink/ ui-tui/packages/hermes-ink/
RUN npm install --prefer-offline --no-audit && \
    (cd web && npm install --prefer-offline --no-audit) && \
    (cd ui-tui && npm install --prefer-offline --no-audit) && \
    npm cache clean --force

# -------------------------------------------------------------------------
# 8. Hermes-neo source
# -------------------------------------------------------------------------
COPY --chown=hermes:hermes . .

# Build dashboard and TUI
RUN cd web && npm run build && \
    cd ../ui-tui && npm run build

# Editable install (no deps — already resolved)
RUN uv pip install --no-cache-dir --no-deps -e "."

# -------------------------------------------------------------------------
# 9. computer-use-linux (MCP server for desktop control)
# -------------------------------------------------------------------------
# Install from npm (includes prebuilt Rust binary)
RUN npm install -g @agent-sh/computer-use-linux

# -------------------------------------------------------------------------
# 10. Camofox Browser (anti-detection Firefox)
# -------------------------------------------------------------------------
RUN npm install -g camofox-browser

# -------------------------------------------------------------------------
# 11. Permissions
# -------------------------------------------------------------------------
RUN chmod -R a+rX /opt/hermes-neo && \
    chown -R hermes:hermes /opt/hermes-neo/.venv \
    /opt/hermes-neo/ui-tui /opt/hermes-neo/node_modules

# -------------------------------------------------------------------------
# 12. Environment
# -------------------------------------------------------------------------
ENV HERMES_HOME=/opt/data
ENV HERMES_WEB_DIST=/opt/hermes-neo/hermes_cli/web_dist
ENV PATH="/opt/hermes-neo/.venv/bin:/opt/hermes-neo/bin:${PATH}"
# GNOME/Wayland
ENV XDG_RUNTIME_DIR=/tmp/xdg-runtime
ENV WAYLAND_DISPLAY=wayland-0
ENV DISPLAY=:99
# Tailscale
ENV TS_STATE_DIR=/var/lib/tailscale

VOLUME ["/opt/data"]

# -------------------------------------------------------------------------
# 13. Startup
# -------------------------------------------------------------------------
COPY --chmod=0755 develop/startup.sh /opt/hermes-neo/startup.sh

EXPOSE 9377

ENTRYPOINT ["/opt/hermes-neo/startup.sh"]
CMD ["hermes", "gateway", "run"]
