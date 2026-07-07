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
REPO_URL="https://github.com/Synendo/PlotterHub.git"
# The service runs as a specific non-root user. Normally that's whoever invokes
# this script; the self-update path runs the script as root inside a transient
# unit and passes the user in via SERVICE_USER (falling back to SUDO_USER).
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-${USER:-$(whoami)}}}"
MIN_PY_MAJOR=3
MIN_PY_MINOR=11

run_sudo() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif [ -n "${SUDO_PW:-}" ]; then
        echo "$SUDO_PW" | sudo -S "$@"
    else
        sudo "$@"
    fi
}

# Run a command as the service user. When this script is invoked as root (the
# self-update path) the venv and pip must not be created root-owned, so drop
# privileges; otherwise run directly.
as_user() {
    if [ "$(id -u)" -eq 0 ] && [ "$SERVICE_USER" != "root" ]; then
        runuser -u "$SERVICE_USER" -- "$@"
    else
        "$@"
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

# systemd as the active init. This installer manages the service exclusively
# through systemctl; on a system that boots a different init as PID 1 those
# calls may silently no-op and the service never actually starts, or something
# else ends up answering on the local box. Raspberry Pi OS is systemd-booted.
if [ "$(ps -p 1 -o comm= 2>/dev/null)" != "systemd" ]; then
    echo "    warning: PID 1 is not systemd; this installer requires a systemd-booted system."
    echo "             The service is managed via 'systemctl' and will not start under another init."
    echo "             Plotter Hub is only tested on Raspberry Pi OS."
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
    as_user python3 -m venv "$PROJECT_DIR/venv"
fi

echo ">>> Installing Python dependencies"
as_user "$PROJECT_DIR/venv/bin/pip" install --upgrade pip wheel
as_user "$PROJECT_DIR/venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

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
fi

if [ -n "${PLOTTER_MODEL:-}" ]; then
    echo ">>> Setting PLOTTER_MODEL=$PLOTTER_MODEL in unit"
    run_sudo sed -i "s/^Environment=PLOTTER_MODEL=.*/Environment=PLOTTER_MODEL=$PLOTTER_MODEL/" "$UNIT_DST"
fi

echo ">>> Installing sudoers rule for shutdown button"
SUDOERS_DST="/etc/sudoers.d/plotterhub-shutdown"
SUDOERS_TMP="$(mktemp)"
printf '%s ALL=(root) NOPASSWD: /sbin/shutdown\n' "$SERVICE_USER" > "$SUDOERS_TMP"
chmod 0440 "$SUDOERS_TMP"
run_sudo visudo -cf "$SUDOERS_TMP" >/dev/null
run_sudo install -m 0440 -o root -g root "$SUDOERS_TMP" "$SUDOERS_DST"
rm -f "$SUDOERS_TMP"

echo ">>> Installing self-update wrapper"
# Root-owned wrapper at a fixed path (so the NOPASSWD grant can't be widened by
# editing a repo file), with the project dir / user / repo baked in.
WRAPPER_DST="/usr/local/sbin/plotterhub-update"
WRAPPER_TMP="$(mktemp)"
sed -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    -e "s|__SERVICE_USER__|$SERVICE_USER|g" \
    -e "s|__REPO_URL__|$REPO_URL|g" \
    "$PROJECT_DIR/scripts/plotterhub-update.in" > "$WRAPPER_TMP"
run_sudo install -m 0755 -o root -g root "$WRAPPER_TMP" "$WRAPPER_DST"
rm -f "$WRAPPER_TMP"

echo ">>> Installing sudoers rule for self-update"
UPD_SUDOERS_DST="/etc/sudoers.d/plotterhub-update"
UPD_SUDOERS_TMP="$(mktemp)"
printf '%s ALL=(root) NOPASSWD: %s "", %s --dry-run\n' \
    "$SERVICE_USER" "$WRAPPER_DST" "$WRAPPER_DST" > "$UPD_SUDOERS_TMP"
chmod 0440 "$UPD_SUDOERS_TMP"
run_sudo visudo -cf "$UPD_SUDOERS_TMP" >/dev/null
run_sudo install -m 0440 -o root -g root "$UPD_SUDOERS_TMP" "$UPD_SUDOERS_DST"
rm -f "$UPD_SUDOERS_TMP"

run_sudo systemctl daemon-reload
run_sudo systemctl enable "$SERVICE_NAME"
run_sudo systemctl restart "$SERVICE_NAME"

echo ">>> Waiting for service to come up"
sleep 3
if run_sudo systemctl is-active --quiet "$SERVICE_NAME"; then
    echo ">>> Service is running"

    # Confirm the listener is actually reachable from the LAN and not just from
    # the local box. A socket bound to 127.0.0.1/::1 answers the machine's own
    # browser but is invisible to every other host — the classic "loads locally,
    # never loads from another machine" report. We bind to 0.0.0.0 in the unit,
    # so anything else here means a stale/overridden unit or a hand-started copy.
    #
    # Poll rather than check once: systemd marks the Type=simple unit active the
    # moment the process forks, but uvicorn's heavy imports (numpy/scipy/vpype)
    # can delay the actual socket bind by 10s+ on a Pi, so a single check races
    # startup and cries wolf.
    LISTEN=""
    for _ in $(seq 1 15); do
        LISTEN="$(ss -ltn 2>/dev/null | awk -v p=":$PORT\$" '$4 ~ p {print $4}')"
        [ -n "$LISTEN" ] && break
        sleep 2
    done
    if [ -z "$LISTEN" ]; then
        echo "!!! warning: nothing is listening on port $PORT even though the service is active."
        echo "             Inspect with: journalctl -u $SERVICE_NAME -n 50"
    elif ! printf '%s\n' "$LISTEN" | grep -qE '^(0\.0\.0\.0|\*|\[::\]):'"$PORT"'$'; then
        echo "!!! warning: port $PORT is bound to loopback only ($LISTEN)."
        echo "             It will load on this machine but NOT from other machines on the network."
        echo "             Expected 0.0.0.0:$PORT — check for a stale or overridden systemd unit."
    fi

    URL="http://$(hostname).local"
    [ "$PORT" != "80" ] && URL="$URL:$PORT"
    echo ">>> Open $URL from any machine on the network"

    # Also print the raw LAN IP as a fallback: '.local' is the address to use
    # (mDNS follows the Pi across DHCP lease changes, the IP may not), but if a
    # client can't resolve '.local' the IP still gets them in — and printing it
    # makes a port-80 -> 8080 fallback obvious too.
    LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
    if [ -n "$LAN_IP" ]; then
        IP_URL="http://$LAN_IP"
        [ "$PORT" != "80" ] && IP_URL="$IP_URL:$PORT"
        echo ">>> If a device can't resolve .local, use this Pi's IP instead: $IP_URL (may change on reboot)"
    fi
else
    echo "!!! Service failed to start; inspect with: journalctl -u $SERVICE_NAME -n 50"
    exit 1
fi
