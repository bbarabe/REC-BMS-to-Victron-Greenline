#!/bin/sh
# dbus-recbms package installer — run ON the Cerbo, after copying this folder
# to /data/dbus-recbms. Creates the daemontools services and makes them
# survive Venus OS firmware updates via /data/rc.local.
#
#   sh install.sh                 # dbus-recbms (the BMS) only
#   sh install.sh solarpriority   # + dbus-solarpriority (Solar Priority)
#   sh install.sh all             # both
#
# Re-running is harmless. An already-installed service only needs the copy
# and `svc -t /service/<name>`.
set -e

DIR=/data/dbus-recbms
RCLOCAL=/data/rc.local

if [ ! -f "$DIR/dbus_recbms.py" ]; then
    echo "ERROR: copy this folder to $DIR first (found no $DIR/dbus_recbms.py)"
    exit 1
fi

chmod 755 "$DIR"/*.py "$DIR"/*.sh "$DIR"/service/run "$DIR"/service/log/run \
          "$DIR"/service-solarpriority/run "$DIR"/service-solarpriority/log/run 2>/dev/null || true

touch "$RCLOCAL"
chmod 755 "$RCLOCAL"

install_one() {
    # $1 = service name, $2 = service dir
    ln -sfn "$2" "/service/$1"
    if ! grep -q "/service/$1" "$RCLOCAL"; then
        echo "ln -sfn $2 /service/$1" >> "$RCLOCAL"
    fi
    echo "Installed $1 (starts within ~5 seconds)."
    echo "  status:  svstat /service/$1"
    echo "  logs:    tail -f /var/log/$1/current | tai64nlocal"
    echo "  restart: svc -t /service/$1"
}

WHAT="${1:-recbms}"
case "$WHAT" in
    recbms)        install_one dbus-recbms "$DIR/service" ;;
    solarpriority) install_one dbus-solarpriority "$DIR/service-solarpriority" ;;
    all)           install_one dbus-recbms "$DIR/service"
                   install_one dbus-solarpriority "$DIR/service-solarpriority" ;;
    *) echo "usage: install.sh [recbms|solarpriority|all]"; exit 1 ;;
esac
