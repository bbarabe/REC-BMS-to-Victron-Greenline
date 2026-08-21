#!/bin/sh
# dbus-batteries installer — run ON the Cerbo, after copying this folder to
# /data/dbus-batteries. Creates the daemontools service and makes it survive
# Venus OS firmware updates via /data/rc.local.
set -e

DIR=/data/dbus-batteries

if [ ! -f "$DIR/dbus_batteries.py" ]; then
    echo "ERROR: copy this folder to $DIR first (found no $DIR/dbus_batteries.py)"
    exit 1
fi

chmod 755 "$DIR/dbus_batteries.py" "$DIR/service/run" "$DIR/service/log/run"           "$DIR/install.sh" "$DIR/uninstall.sh" "$DIR/migrate.sh" 2>/dev/null || true

# Start now: svscan picks up the /service symlink within ~5s
ln -sfn "$DIR/service" /service/dbus-batteries

# Survive firmware updates (rootfs is replaced; /data persists)
RCLOCAL=/data/rc.local
touch "$RCLOCAL"
chmod 755 "$RCLOCAL"
if ! grep -q "dbus-batteries" "$RCLOCAL"; then
    echo "ln -sfn $DIR/service /service/dbus-batteries" >> "$RCLOCAL"
fi

echo "Installed. The service starts within ~5 seconds."
echo "  status:  svstat /service/dbus-batteries"
echo "  logs:    tail -f /var/log/dbus-batteries/current | tai64nlocal"
echo "  restart: svc -t /service/dbus-batteries"
