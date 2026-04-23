#!/usr/bin/env bash
# Idempotent install script for Plotter Hub.
# Run from the project root on the Raspberry Pi: ./install.sh
# Safe to re-run after `git pull` to update dependencies and restart the service.
#
# Unattended installs:
#   SUDO_PW='your-password'   — pipe into sudo -S
#   PLOTTER_MODEL=<1-8>       — set axidraw model for this installation
#
# If port 80 is free the service binds there; otherwise it falls back to 8080.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="plotterhub"
UNIT_SRC="$PROJECT_DIR/systemd/$SERVICE_NAME.service"
UNIT_DST="/etc/systemd/system/$SERVICE_NAME.service"
SERVICE_USER="${USER:-$(whoami)}"
MIN_PY_MAJOR=3
MIN_PY_MINOR=11

run_sudo() {
    if [ -n "${SUDO_PW:-}" ]; then
        echo "$SUDO_PW" | sudo -S "$@"
    else
        sudo "$@"
    fi
}

fail() { echo "!!! $*" >&2; exit 1; }

echo ">>> Checking prerequisites"

# Python version
if ! command -v python3 >/dev/null 2>&1; then
    fail "python3 not found"
fi
PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PY_MAJOR="$(echo "$PY_VER" | cut -d. -f1)"
PY_MINOR="$(echo "$PY_VER" | cut -d. -f2)"
if [ "$PY_MAJOR" -lt "$MIN_PY_MAJOR" ] || \
   { [ "$PY_MAJOR" -eq "$MIN_PY_MAJOR" ] && [ "$PY_MINOR" -lt "$MIN_PY_MINOR" ]; }; then
    fail "python $MIN_PY_MAJOR.$MIN_PY_MINOR+ required, found $PY_VER"
fi
echo "    python $PY_VER"

# dialout group (needed for /dev/ttyACM* access)
if ! id -nG "$SERVICE_USER" | tr ' ' '\n' | grep -qx dialout; then
    fail "user '$SERVICE_USER' is not in the 'dialout' group.
    Fix:    sudo usermod -a -G dialout $SERVICE_USER
    Then log out and back in (or reboot) so the membership takes effect."
fi
echo "    '$SERVICE_USER' is in dialout group"

# avahi (for plotterstudio.local)
if systemctl is-active --quiet avahi-daemon; then
    echo "    avahi-daemon active ($(hostname).local should resolve)"
else
    echo "    warning: avahi-daemon is not running; '.local' hostname resolution may not work"
fi

# If a previous install is already running, stop it so its own port-80
# binding doesn't look like a conflict during the port probe below.
if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    echo "    $SERVICE_NAME already running; stopping for clean reinstall"
    run_sudo systemctl stop "$SERVICE_NAME"
fi

# Pick a free port
if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE '(^|:)80$'; then
    PORT=8080
    echo "    port 80 is taken; using $PORT instead"
else
    PORT=80
    echo "    port $PORT available"
fi

echo ">>> Installing system packages"
run_sudo apt-get update
run_sudo apt-get install -y python3 python3-venv python3-pip

echo ">>> Creating Python virtualenv"
if [ ! -d "$PROJECT_DIR/venv" ]; then
    python3 -m venv "$PROJECT_DIR/venv"
fi

echo ">>> Installing Python dependencies"
"$PROJECT_DIR/venv/bin/pip" install --upgrade pip wheel
"$PROJECT_DIR/venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

echo ">>> Installing systemd unit"
run_sudo cp "$UNIT_SRC" "$UNIT_DST"

# The repo's unit is a template; fill in the service user and the project
# path so it runs as the invoking user from wherever the repo was cloned.
run_sudo sed -i "s|__SERVICE_USER__|$SERVICE_USER|g" "$UNIT_DST"
run_sudo sed -i "s|__WORKDIR__|$PROJECT_DIR|g" "$UNIT_DST"

# Rewrite the port if we fell back.
if [ "$PORT" != "80" ]; then
    run_sudo sed -i "s|--port 80|--port $PORT|" "$UNIT_DST"
    # Low-port capability is only needed for ports <1024.
    run_sudo sed -i '/^AmbientCapabilities=/d' "$UNIT_DST"
    run_sudo sed -i '/^CapabilityBoundingSet=/d' "$UNIT_DST"
fi

if [ -n "${PLOTTER_MODEL:-}" ]; then
    echo ">>> Setting PLOTTER_MODEL=$PLOTTER_MODEL in unit"
    run_sudo sed -i "s/^Environment=PLOTTER_MODEL=.*/Environment=PLOTTER_MODEL=$PLOTTER_MODEL/" "$UNIT_DST"
fi

run_sudo systemctl daemon-reload
run_sudo systemctl enable "$SERVICE_NAME"
run_sudo systemctl restart "$SERVICE_NAME"

echo ">>> Waiting for service to come up"
sleep 3
if run_sudo systemctl is-active --quiet "$SERVICE_NAME"; then
    URL="http://$(hostname).local"
    [ "$PORT" != "80" ] && URL="$URL:$PORT"
    echo ">>> Service is running"
    echo ">>> Open $URL"
else
    echo "!!! Service failed to start; inspect with: journalctl -u $SERVICE_NAME -n 50"
    exit 1
fi
