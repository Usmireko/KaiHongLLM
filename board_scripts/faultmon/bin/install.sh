#!/bin/sh
#
# install.sh - deploy faultmon tooling to /data/faultmon/bin

set -eu

PATH=/bin:/system/bin:/usr/bin:/usr/local/bin
export PATH

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
BASE_DIR=/data/faultmon
BIN_DIR=$BASE_DIR/bin

mkdir -p "$BIN_DIR"
chmod 750 "$BIN_DIR"

copy_binary() {
    local src="$1" dst="$BIN_DIR/$(basename "$1")"
    if [ ! -f "$src" ]; then
        echo "install.sh: missing $src" >&2
        exit 1
    fi
    if [ "$src" = "$dst" ]; then
        echo "skip $(basename "$src") (already in $BIN_DIR)"
        return
    fi
    cp "$src" "$dst"
    chmod 750 "$dst"
    echo "installed $(basename "$src") -> $dst"
}

copy_binary "$SCRIPT_DIR/faultmon.sh"
copy_binary "$SCRIPT_DIR/faultmonctl"

if [ -x "$BIN_DIR/faultmon.sh" ]; then
    "$BIN_DIR/faultmon.sh" start || true
    "$BIN_DIR/faultmon.sh" status || true
fi

write_autostart() {
    local target_dir=""
    for dir in /etc/init /system/etc/init; do
        if [ -d "$dir" ] && [ -w "$dir" ]; then
            target_dir="$dir"
            break
        fi
    done
    if [ -n "$target_dir" ]; then
        local cfg="$target_dir/faultmon.cfg"
        cat <<EOF >"$cfg"
{
    "services": [
        {
            "name": "faultmon",
            "path": ["/data/faultmon/bin/faultmon.sh", "start"],
            "uid": 0,
            "gid": 0,
            "once": false
        }
    ]
}
EOF
        chmod 644 "$cfg" 2>/dev/null || true
        echo "autostart config written to $cfg"
    else
        echo "Please add 'faultmon.sh start' to your boot script"
    fi
}

write_autostart

echo "faultmon installation complete."
