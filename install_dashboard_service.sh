#!/usr/bin/env bash
#
# install_dashboard_service.sh
# Instala hermes dashboard como servicio systemd a nivel de sistema.
# Lee (y crea si no existen) las variables desde ~/.hermes/.env
#
# Uso:
#   sudo ./install_dashboard_service.sh
#

set -euo pipefail

# Colores
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
ENV_FILE="$HERMES_HOME/.env"
SERVICE_NAME="hermes-dashboard"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

echo -e "${BLUE}=== Hermes Dashboard - Systemd Service Installer ===${NC}"
echo

# Verificar que se ejecuta como root
if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}Error: Este script debe ejecutarse con sudo o como root.${NC}"
    echo "Ejemplo: sudo ./install_dashboard_service.sh"
    exit 1
fi

# Detectar usuario real (el que invocó sudo)
REAL_USER="${SUDO_USER:-$USER}"
REAL_HOME=$(eval echo "~$REAL_USER")
HERMES_HOME="${HERMES_HOME:-$REAL_HOME/.hermes}"
ENV_FILE="$HERMES_HOME/.env"

echo -e "${YELLOW}Usuario real: $REAL_USER${NC}"
echo -e "${YELLOW}HERMES_HOME:  $HERMES_HOME${NC}"
echo -e "${YELLOW}Archivo .env: $ENV_FILE${NC}"
echo

# Crear directorio si no existe
mkdir -p "$HERMES_HOME"

# Función para agregar o actualizar variable en .env
add_or_update_env() {
    local key="$1"
    local value="$2"
    local comment="${3:-}"

    if [[ ! -f "$ENV_FILE" ]]; then
        touch "$ENV_FILE"
        chmod 600 "$ENV_FILE"
        chown "$REAL_USER:$REAL_USER" "$ENV_FILE"
    fi

    if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
        echo -e "  ${GREEN}✓${NC} $key ya existe en .env"
    else
        echo -e "  ${YELLOW}+${NC} Agregando $key=$value a .env"
        {
            echo ""
            if [[ -n "$comment" ]]; then
                echo "# $comment"
            fi
            echo "${key}=${value}"
        } >> "$ENV_FILE"
        chown "$REAL_USER:$REAL_USER" "$ENV_FILE"
    fi
}

echo -e "${BLUE}→ Configurando variables en $ENV_FILE${NC}"

# Agregar variables de configuración del dashboard si no existen
add_or_update_env "HERMES_DASHBOARD_HOST" "0.0.0.0" "Dashboard bind address"
add_or_update_env "HERMES_DASHBOARD_PORT" "4041" "Dashboard port"
add_or_update_env "HERMES_DASHBOARD_INSECURE" "true" "Allow binding to 0.0.0.0 (exposes API keys)"
add_or_update_env "HERMES_DASHBOARD_TUI" "true" "Enable embedded TUI chat tab"

echo

# Leer valores actuales del .env
if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    set -a
    source "$ENV_FILE"
    set +a
fi

DASHBOARD_HOST="${HERMES_DASHBOARD_HOST:-0.0.0.0}"
DASHBOARD_PORT="${HERMES_DASHBOARD_PORT:-4041}"
DASHBOARD_INSECURE="${HERMES_DASHBOARD_INSECURE:-true}"
DASHBOARD_TUI="${HERMES_DASHBOARD_TUI:-true}"

echo -e "${BLUE}→ Valores que se usarán:${NC}"
echo "   HERMES_DASHBOARD_HOST=$DASHBOARD_HOST"
echo "   HERMES_DASHBOARD_PORT=$DASHBOARD_PORT"
echo "   HERMES_DASHBOARD_INSECURE=$DASHBOARD_INSECURE"
echo "   HERMES_DASHBOARD_TUI=$DASHBOARD_TUI"
echo

# Construir flags del comando
DASHBOARD_ARGS="--host $DASHBOARD_HOST --port $DASHBOARD_PORT"

if [[ "$DASHBOARD_INSECURE" == "true" || "$DASHBOARD_INSECURE" == "1" ]]; then
    DASHBOARD_ARGS="$DASHBOARD_ARGS --insecure"
fi

if [[ "$DASHBOARD_TUI" == "true" || "$DASHBOARD_TUI" == "1" ]]; then
    DASHBOARD_ARGS="$DASHBOARD_ARGS --tui"
fi

echo -e "${BLUE}→ Comando que se ejecutará:${NC}"
echo "   hermes dashboard $DASHBOARD_ARGS"
echo

# Detectar ruta de hermes
HERMES_BIN="$(command -v hermes || echo "")"

if [[ -z "$HERMES_BIN" ]]; then
    # Intentar rutas comunes
    for candidate in \
        "$REAL_HOME/.local/bin/hermes" \
        "/usr/local/bin/hermes" \
        "/usr/bin/hermes"; do
        if [[ -x "$candidate" ]]; then
            HERMES_BIN="$candidate"
            break
        fi
    done
fi

if [[ -z "$HERMES_BIN" ]]; then
    echo -e "${RED}Error: No se encontró el binario 'hermes'.${NC}"
    echo "Instálalo primero o asegúrate de que esté en el PATH."
    exit 1
fi

echo -e "${GREEN}✓${NC} Hermes encontrado en: $HERMES_BIN"
echo

# Crear el archivo de servicio systemd
echo -e "${BLUE}→ Generando servicio systemd: $SERVICE_FILE${NC}"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Hermes Agent Dashboard
Documentation=https://hermes-agent.nousresearch.com
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$REAL_USER
Group=$(id -gn "$REAL_USER")
WorkingDirectory=$REAL_HOME
Environment="HOME=$REAL_HOME"
Environment="HERMES_HOME=$HERMES_HOME"
EnvironmentFile=-$ENV_FILE
ExecStart=$HERMES_BIN dashboard $DASHBOARD_ARGS
Restart=always
RestartSec=5
# Graceful shutdown
TimeoutStopSec=30
KillMode=mixed
KillSignal=SIGTERM

# Seguridad básica
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

chmod 644 "$SERVICE_FILE"

echo -e "${GREEN}✓${NC} Servicio creado: $SERVICE_FILE"
echo

# Recargar systemd y habilitar el servicio
echo -e "${BLUE}→ Recargando systemd...${NC}"
systemctl daemon-reload

echo -e "${BLUE}→ Habilitando servicio en arranque...${NC}"
systemctl enable "$SERVICE_NAME"

echo -e "${BLUE}→ Iniciando servicio...${NC}"
systemctl start "$SERVICE_NAME"

echo
echo -e "${GREEN}=== Servicio instalado correctamente ===${NC}"
echo
echo -e "${YELLOW}Comandos útiles:${NC}"
echo "  sudo systemctl status  $SERVICE_NAME"
echo "  sudo systemctl restart $SERVICE_NAME"
echo "  sudo systemctl stop    $SERVICE_NAME"
echo "  sudo journalctl -u     $SERVICE_NAME -f"
echo
echo -e "${YELLOW}Para desinstalar:${NC}"
echo "  sudo systemctl disable --now $SERVICE_NAME"
echo "  sudo rm $SERVICE_FILE"
echo "  sudo systemctl daemon-reload"
echo

exit 0
