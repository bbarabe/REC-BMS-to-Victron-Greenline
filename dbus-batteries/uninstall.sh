#!/bin/sh
# dbus-batteries uninstaller — stops the service and removes the autostart hook.
# Leaves /data/dbus-batteries and the localsettings entries in place, so the
# device instances stay reserved and a re-install comes back identical.
# Delete /data/dbus-batteries manually if you want it gone.

svc -d /service/dbus-batteries 2>/dev/null || true
rm -f /service/dbus-batteries

if [ -f /data/rc.local ]; then
    sed -i '/dbus-batteries/d' /data/rc.local
fi

echo "Uninstalled (files left in /data/dbus-batteries)."
