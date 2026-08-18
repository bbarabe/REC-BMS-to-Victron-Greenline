#!/bin/sh
# dbus-czone installer — run ON the Cerbo, after copying this folder to
# /data/dbus-czone. Creates the daemontools service and makes it survive
# Venus OS firmware updates via /data/rc.local.
set -e

DIR=/data/dbus-czone

if [ ! -f "$DIR/dbus_czone.py" ]; then
    echo "ERROR: copy this folder to $DIR first (found no $DIR/dbus_czone.py)"
    exit 1
fi

chmod 755 "$DIR/dbus_czone.py" "$DIR/service/run" "$DIR/service/log/run" \
          "$DIR/install.sh" "$DIR/uninstall.sh" 2>/dev/null || true

# Start now: svscan picks up the /service symlink within ~5s
ln -sfn "$DIR/service" /service/dbus-czone

# Survive firmware updates (rootfs is replaced; /data persists)
RCLOCAL=/data/rc.local
touch "$RCLOCAL"
chmod 755 "$RCLOCAL"
if ! grep -q "dbus-czone" "$RCLOCAL"; then
    echo "ln -sfn $DIR/service /service/dbus-czone" >> "$RCLOCAL"
fi

echo "Installed. The service starts within ~5 seconds."
echo "  status:  svstat /service/dbus-czone"
echo "  logs:    tail -f /var/log/dbus-czone/current | tai64nlocal"
echo "  restart: svc -t /service/dbus-czone"
