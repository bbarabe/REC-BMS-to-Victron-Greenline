#!/bin/sh
# dbus-edrive installer — run ON the Cerbo, after copying this folder to
# /data/dbus-edrive. Creates the daemontools service and makes it survive
# Venus OS firmware updates via /data/rc.local.
set -e

DIR=/data/dbus-edrive

if [ ! -f "$DIR/dbus_edrive.py" ]; then
    echo "ERROR: copy this folder to $DIR first (found no $DIR/dbus_edrive.py)"
    exit 1
fi

chmod 755 "$DIR/dbus_edrive.py" "$DIR/service/run" "$DIR/service/log/run"           "$DIR/install.sh" "$DIR/uninstall.sh" "$DIR/migrate.sh" 2>/dev/null || true

# Start now: svscan picks up the /service symlink within ~5s
ln -sfn "$DIR/service" /service/dbus-edrive

# Survive firmware updates (rootfs is replaced; /data persists)
RCLOCAL=/data/rc.local
touch "$RCLOCAL"
chmod 755 "$RCLOCAL"
if ! grep -q "dbus-edrive" "$RCLOCAL"; then
    echo "ln -sfn $DIR/service /service/dbus-edrive" >> "$RCLOCAL"
fi

echo "Installed. The service starts within ~5 seconds."
echo "  status:  svstat /service/dbus-edrive"
echo "  logs:    tail -f /var/log/dbus-edrive/current | tai64nlocal"
echo "  restart: svc -t /service/dbus-edrive"
