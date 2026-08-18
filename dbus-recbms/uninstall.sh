#!/bin/sh
# dbus-recbms uninstaller — stops the service and removes the autostart hook.
# Leaves /data/dbus-recbms and the localsettings entries in place
# (instances 200/220 stay reserved; delete /data/dbus-recbms manually
# if you want it gone).

svc -d /service/dbus-recbms 2>/dev/null || true
rm -f /service/dbus-recbms

if [ -f /data/rc.local ]; then
    sed -i '/dbus-recbms/d' /data/rc.local
fi

echo "Uninstalled (files left in /data/dbus-recbms)."
