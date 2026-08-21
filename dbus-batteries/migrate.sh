#!/bin/sh
# dbus-batteries migration — run ON the Cerbo, ONCE, when moving off the
# Node-RED "Batteries Forward" flow.
#
# The flow's five virtual batteries hold device instances 201-205 through
# /Settings/Devices/virtual_bat<N>_virtual/ClassAndVrmInstance. While those
# entries exist localsettings will not hand the same numbers to this driver,
# and it falls back to whatever it is granted — so VRM would show the
# batteries as brand new devices with no history.
#
# Order matters:
#   1. In the Node-RED UI: disable the "Batteries Forward" tab and Deploy.
#      (This is the one step that cannot be scripted — see CLAUDE.md, flows
#      are deployed by hand.)
#   2. Run this script. It refuses to touch anything while a flow service is
#      still on the bus.
#   3. svc -t /service/dbus-batteries
#
# Retiring the entries is also what lets InstanceRegistry.json go: with no
# virtual_* devices left there is nothing for it to pin.

ENTRIES="virtual_bat1_virtual virtual_bat2_virtual virtual_bat3_virtual
virtual_bat4_virtual virtual_bat5_virtual"
LEAVES="ClassAndVrmInstance CustomName"

live=$(dbus -y 2>/dev/null | grep -c 'virtual_bat[0-9]_virtual')
if [ "$live" != "0" ]; then
    echo "ERROR: $live Node-RED battery service(s) are still on the bus."
    echo "Disable the 'Batteries Forward' tab in Node-RED and Deploy first."
    echo
    echo "Disabling a tab does not always drop its virtual services — they can"
    echo "survive as zombies still holding their instances. If they linger:"
    echo "  svc -t /service/signalk-server"
    exit 1
fi

get() {
    dbus -y com.victronenergy.settings "/Settings/Devices/$1/$2" \
        GetValue 2>/dev/null
}

echo "Current entries:"
found=""
for e in $ENTRIES; do
    for leaf in $LEAVES; do
        v=$(get "$e" "$leaf")
        if [ -n "$v" ]; then
            echo "  $e/$leaf = $v"
            if [ -n "$found" ]; then found="$found,"; fi
            found="$found\"Devices/$e/$leaf\""
        fi
    done
done

if [ -z "$found" ]; then
    echo "  (none) — already clean."
    exit 0
fi

echo
echo "Removing them..."
# RemoveSettings takes the LEAF path, relative to the object it is called on.
# Handing it the GROUP ("Devices/<id>") returns -1 for every entry and changes
# nothing — silently. Verified on the boat 2026-08-21.
dbus -y com.victronenergy.settings /Settings RemoveSettings "%[$found]"

echo
echo "Left behind (should be none):"
left=""
for e in $ENTRIES; do
    for leaf in $LEAVES; do
        v=$(get "$e" "$leaf")
        if [ -n "$v" ]; then
            echo "  $e/$leaf = $v"
            left="yes"
        fi
    done
done
if [ -z "$left" ]; then
    echo "  (none)"
fi

echo
echo "Done. Now: svc -t /service/dbus-batteries"
echo "Then check the driver logged 'forwarding as ... (instance 201..205)':"
echo "  tail -n 40 /var/log/dbus-batteries/current | tai64nlocal"
