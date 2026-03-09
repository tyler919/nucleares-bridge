#!/usr/bin/env bash
# Nucleares Bridge — update script (Linux / WSL / macOS)
set -euo pipefail

echo ""
echo " ================================================"
echo "  Nucleares Bridge Updater"
echo " ================================================"
echo ""

# Show current version
echo " Current version:"
git log -1 --format="  %h  %s  (%ar)"
echo ""

# Pull latest code
echo " Pulling latest code from GitHub..."
git pull
echo ""

# Create variables.yaml from default if the user doesn't have one yet
if [ ! -f variables.yaml ]; then
    echo " No variables.yaml found — creating from defaults..."
    cp variables.default.yaml variables.yaml
    echo " Created variables.yaml. Edit it to customise which variables are polled."
    echo ""
fi

# Show what changed
echo " What changed in this update:"
git log --oneline ORIG_HEAD..HEAD 2>/dev/null || echo " (no previous baseline to compare)"
echo ""

# Restart service if running under systemd
if systemctl is-active --quiet nucleares-bridge 2>/dev/null; then
    echo " Restarting nucleares-bridge systemd service..."
    systemctl restart nucleares-bridge
    echo " [OK] Service restarted."
elif pgrep -f "bridge.py" > /dev/null 2>&1; then
    echo " [INFO] bridge.py is running as a standalone process."
    echo "        Stop and restart it manually to apply the update."
else
    echo " [INFO] Bridge does not appear to be running. Start it with:"
    echo "        python bridge.py"
fi

echo ""
echo " ================================================"
echo "  Update complete!"
echo " ================================================"
echo ""
