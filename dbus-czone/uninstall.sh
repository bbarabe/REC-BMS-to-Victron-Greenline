#!/bin/sh
# dbus-czone uninstaller — stops the service and removes the autostart hook.
# Leaves /data/dbus-czone and the localsettings entries in place
# (instance 224 and the discovered circuit table stay reserved; delete
# /data/dbus-czone manually if you want it gone).

svc -d /service/dbus-czone 2>/dev/null || true
rm -f /service/dbus-czone

if [ -f /data/rc.local ]; then
    sed -i '/dbus-czone/d' /data/rc.local
fi

echo "Uninstalled (files left in /data/dbus-czone)."
