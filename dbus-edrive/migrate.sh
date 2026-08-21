#!/bin/sh
# dbus-edrive migration — run ON the Cerbo, ONCE, when moving off the
# Node-RED "Greenline E-Drive" flow.
#
# The flow's two virtual motordrives hold device instances 210/211 through
# /Settings/Devices/virtual_gl6gk_<side>/ClassAndVrmInstance. While those
# entries exist localsettings will not hand the same numbers to this driver,
# and it falls back to whatever it is granted — so VRM would show the drives
# as brand new devices with no history.
#
# The flow also leaves a `candump` child process behind if its tab is deleted
# rather than disabled; this script kills the one it starts. The driver reads
# the CAN bus with kernel filters instead and starts no subprocess at all.
#
# Order matters:
#   1. In the Node-RED UI: disable the "Greenline E-Drive" tab and Deploy.
#      (This is the one step that cannot be scripted — see CLAUDE.md, flows
#      are deployed by hand.)
#   2. Run this script. It refuses to touch anything while a flow service is
#      still on the bus.
#   3. svc -t /service/dbus-edrive
#
# Retiring the entries is also what lets InstanceRegistry.json go: with no
# virtual_* devices left there is nothing for it to pin.

ENTRIES="virtual_gl6gk_port virtual_gl6gk_stbd"
LEAVES="ClassAndVrmInstance CustomName"

live=$(dbus -y 2>/dev/null | grep -c 'virtual_gl6gk_')
if [ "$live" != "0" ]; then
    echo "ERROR: $live Node-RED motordrive service(s) are still on the bus."
    echo "Disable the 'Greenline E-Drive' tab in Node-RED and Deploy first."
    echo
    echo "Disabling a tab does not always drop its virtual services — they can"
    echo "survive as zombies still holding their instances. If they linger:"
    echo "  svc -t /service/signalk-server"
    exit 1
fi

# The flow's capture, if it outlived its tab. Matches the exact command line
# in GreenlineEDriveFlow.json; the bracket keeps pkill from matching itself.
if pkill -f "[c]andump -L can0,18A:7FF" 2>/dev/null; then
    echo "stopped the flow's leftover candump"
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
echo "Done. Now: svc -t /service/dbus-edrive"
echo "Then check the driver logged 'registered ... (instance 210/211)':"
echo "  tail -n 40 /var/log/dbus-edrive/current | tai64nlocal"
