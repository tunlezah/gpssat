#!/usr/bin/env bash
# uninstall.sh - Remove GPS-disciplined NTP server configuration
# Removes services, configs, and application files installed by setup.sh.
# Leaves system packages (gpsd, chrony, python3, pps-tools) installed.
set -euo pipefail

INSTALL_DIR="/opt/gpssat"
SERVICE_USER="gpssat"

echo "=== GPS-Disciplined NTP Server Uninstall ==="
echo ""

# Must be root
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script must be run as root (sudo $0)"
    exit 1
fi

# ---------------------------------------------------------------------------
# 1. Stop and remove gpssat service
# ---------------------------------------------------------------------------
echo "[1/5] Removing gpssat service..."
if systemctl is-active --quiet gpssat 2>/dev/null; then
    systemctl stop gpssat
    echo "  Stopped gpssat service"
fi
if systemctl is-enabled --quiet gpssat 2>/dev/null; then
    systemctl disable gpssat
    echo "  Disabled gpssat service"
fi
if [[ -f /etc/systemd/system/gpssat.service ]]; then
    rm /etc/systemd/system/gpssat.service
    systemctl daemon-reload
    echo "  Removed gpssat.service unit file"
else
    echo "  gpssat.service not found, skipping"
fi

# ---------------------------------------------------------------------------
# 2. Restore gpsd configuration
# ---------------------------------------------------------------------------
echo "[2/5] Restoring gpsd configuration..."
systemctl stop gpsd 2>/dev/null || true
systemctl stop gpsd.socket 2>/dev/null || true

# Remove our gpsd config and let the package default remain
if [[ -f /etc/default/gpsd ]]; then
    # Reinstall the package default config
    if dpkg -L gpsd 2>/dev/null | grep -q /etc/default/gpsd; then
        apt-get -o Dpkg::Options::="--force-confmiss" install --reinstall -y -qq gpsd > /dev/null 2>&1 || true
        echo "  Restored default gpsd configuration"
    else
        rm /etc/default/gpsd
        echo "  Removed /etc/default/gpsd"
    fi
fi

# Remove gpsd user from dialout group (only if we added it)
if getent group dialout | grep -q gpsd 2>/dev/null; then
    gpasswd -d gpsd dialout 2>/dev/null || true
    echo "  Removed gpsd from dialout group"
fi

# ---------------------------------------------------------------------------
# 3. Restore chrony configuration
# ---------------------------------------------------------------------------
echo "[3/5] Restoring chrony configuration..."
systemctl stop chrony 2>/dev/null || true

# Find the most recent backup made by setup.sh
LATEST_BACKUP=""
for f in /etc/chrony/chrony.conf.bak.*; do
    [[ -f "$f" ]] && LATEST_BACKUP="$f"
done

if [[ -n "$LATEST_BACKUP" ]]; then
    cp "$LATEST_BACKUP" /etc/chrony/chrony.conf
    # Clean up all backups
    rm -f /etc/chrony/chrony.conf.bak.*
    echo "  Restored chrony.conf from backup ($LATEST_BACKUP)"
else
    # No backup found; reinstall package default
    apt-get -o Dpkg::Options::="--force-confmiss" install --reinstall -y -qq chrony > /dev/null 2>&1 || true
    echo "  No backup found, restored default chrony configuration"
fi

# ---------------------------------------------------------------------------
# 4. Remove application directory
# ---------------------------------------------------------------------------
echo "[4/5] Removing application files..."
if [[ -d "$INSTALL_DIR" ]]; then
    rm -rf "$INSTALL_DIR"
    echo "  Removed $INSTALL_DIR"
else
    echo "  $INSTALL_DIR not found, skipping"
fi

# ---------------------------------------------------------------------------
# 5. Remove service user
# ---------------------------------------------------------------------------
echo "[5/5] Removing service user..."
if id -u "$SERVICE_USER" &>/dev/null; then
    userdel "$SERVICE_USER" 2>/dev/null || true
    echo "  Removed user: $SERVICE_USER"
else
    echo "  User $SERVICE_USER not found, skipping"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "=== Uninstall Complete ==="
echo ""
echo "The following were removed:"
echo "  - gpssat systemd service"
echo "  - gpsd custom configuration (default restored)"
echo "  - chrony custom configuration (backup restored)"
echo "  - $INSTALL_DIR application directory"
echo "  - $SERVICE_USER system user"
echo ""
echo "The following were left in place:"
echo "  - System packages (gpsd, chrony, python3, pps-tools)"
echo ""
echo "To also remove system packages:"
echo "  sudo apt-get remove gpsd gpsd-clients python3-gps chrony pps-tools"
