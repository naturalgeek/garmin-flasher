#!/usr/bin/env bash
#
# install_udev.sh -- install a udev rule so a non-root user can access the
# Garmin device over USB (vendor id 091e).
#
# Run as root:  sudo ./install_udev.sh
#
set -euo pipefail

RULE_FILE="/etc/udev/rules.d/99-garmin.rules"
RULE_CONTENT='SUBSYSTEM=="usb", ATTR{idVendor}=="091e", MODE="0666"'

if [ "$(id -u)" -ne 0 ]; then
    echo "This script must be run as root (try: sudo $0)" >&2
    exit 1
fi

echo "Writing $RULE_FILE"
printf '%s\n' "$RULE_CONTENT" > "$RULE_FILE"

echo "Reloading udev rules"
udevadm control --reload-rules && udevadm trigger

echo "Done. Unplug and replug the device for the rule to take effect."
