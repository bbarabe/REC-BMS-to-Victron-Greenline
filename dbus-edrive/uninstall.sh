#!/bin/sh
# dbus-edrive uninstaller — stops the service and removes the autostart hook.
# Leaves /data/dbus-edrive and the localsettings entries in place, so the
# device instances stay reserved and a re-install comes back identical.
# Delete /data/dbus-edrive manually if you want it gone.

svc -d /service/dbus-edrive 2>/dev/null || true
rm -f /service/dbus-edrive

if [ -f /data/rc.local ]; then
    sed -i '/dbus-edrive/d' /data/rc.local
fi

echo "Uninstalled (files left in /data/dbus-edrive)."
