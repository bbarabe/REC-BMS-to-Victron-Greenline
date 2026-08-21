#!/bin/sh
# dbus-recbms package uninstaller — stops a service and removes its autostart
# hook. Leaves /data/dbus-recbms and the localsettings entries in place
# (instances 200/220/221 stay reserved; delete /data/dbus-recbms manually if
# you want it gone).
#
#   sh uninstall.sh                 # dbus-recbms (the BMS) only
#   sh uninstall.sh solarpriority   # dbus-solarpriority only
#   sh uninstall.sh all             # both

remove_one() {
    svc -d "/service/$1" 2>/dev/null || true
    rm -f "/service/$1"
    if [ -f /data/rc.local ]; then
        sed -i "\#/service/$1#d" /data/rc.local
    fi
    echo "Uninstalled $1 (files left in /data/dbus-recbms)."
}

WHAT="${1:-recbms}"
case "$WHAT" in
    recbms)        remove_one dbus-recbms ;;
    solarpriority) remove_one dbus-solarpriority ;;
    all)           remove_one dbus-solarpriority; remove_one dbus-recbms ;;
    *) echo "usage: uninstall.sh [recbms|solarpriority|all]"; exit 1 ;;
esac
