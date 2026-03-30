#!/usr/bin/env bash
# setup.sh - Install and configure GPS-disciplined NTP server
# Target: Ubuntu 24.04 LTS
set -euo pipefail

INSTALL_DIR="/opt/gpssat"
SERVICE_USER="gpssat"

echo "=== GPS-Disciplined NTP Server Setup ==="
echo ""

# Must be root
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script must be run as root (sudo $0)"
    exit 1
fi

# ---------------------------------------------------------------------------
# 1. Install system packages
# ---------------------------------------------------------------------------
echo "[1/7] Installing system packages..."
apt-get update -qq
apt-get install -y -qq \
    gpsd gpsd-clients \
    chrony \
    python3 python3-venv python3-pip \
    pps-tools \
    > /dev/null

echo "  Installed: gpsd, chrony, python3, pps-tools"

# ---------------------------------------------------------------------------
# 2. Configure gpsd
# ---------------------------------------------------------------------------
echo "[2/7] Configuring gpsd..."
cp config/gpsd.conf /etc/default/gpsd
# Ensure gpsd socket activation is enabled
systemctl enable gpsd.socket
systemctl enable gpsd.service

# Ensure the gpsd user can access PPS device
if ! getent group dialout | grep -q gpsd 2>/dev/null; then
    usermod -aG dialout gpsd 2>/dev/null || true
fi

echo "  gpsd configured with /dev/ttyACM0 + /dev/pps0"

# ---------------------------------------------------------------------------
# 3. Configure chrony
# ---------------------------------------------------------------------------
echo "[3/7] Configuring chrony..."

# Backup existing config
if [[ -f /etc/chrony/chrony.conf ]]; then
    cp /etc/chrony/chrony.conf /etc/chrony/chrony.conf.bak.$(date +%s)
fi

cp config/chrony.conf /etc/chrony/chrony.conf

echo "  Chrony configured with GPS SHM + PPS + NTP pool fallback"

# ---------------------------------------------------------------------------
# 4. Create service user
# ---------------------------------------------------------------------------
echo "[4/7] Setting up service user..."
if ! id -u "$SERVICE_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    echo "  Created user: $SERVICE_USER"
else
    echo "  User $SERVICE_USER already exists"
fi

# ---------------------------------------------------------------------------
# 5. Install application
# ---------------------------------------------------------------------------
echo "[5/7] Installing GPS NTP Monitor application..."
mkdir -p "$INSTALL_DIR"
cp -r gpssat "$INSTALL_DIR/"
cp requirements.txt "$INSTALL_DIR/"

# Create virtual environment
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

echo "  Application installed to $INSTALL_DIR"

# ---------------------------------------------------------------------------
# 6. Install systemd service
# ---------------------------------------------------------------------------
echo "[6/7] Installing systemd service..."
cp config/gpssat.service /etc/systemd/system/gpssat.service
systemctl daemon-reload
systemctl enable gpssat.service

echo "  Service installed and enabled"

# ---------------------------------------------------------------------------
# 7. Start services
# ---------------------------------------------------------------------------
echo "[7/7] Starting services..."
systemctl restart gpsd
systemctl restart chrony
systemctl restart gpssat

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Services:"
echo "  gpsd     : $(systemctl is-active gpsd)"
echo "  chrony   : $(systemctl is-active chrony)"
echo "  gpssat   : $(systemctl is-active gpssat)"
echo ""
echo "Web UI:  http://$(hostname -I | awk '{print $1}'):5000"
echo ""
echo "Useful commands:"
echo "  cgps                    - Terminal GPS viewer"
echo "  chronyc sources -v      - View NTP sources"
echo "  chronyc tracking        - View NTP tracking"
echo "  journalctl -u gpssat -f - View monitor logs"
echo "  sudo ppstest /dev/pps0  - Test PPS signal"
