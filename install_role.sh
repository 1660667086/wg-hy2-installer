#!/bin/sh
# Convenience wrapper: run once on each server with its own role directory.
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ "$#" -lt 1 ]; then
    echo "Usage: sudo $0 ROLE_DIR [--no-enable] [--dry-run] [--name NAME] [--sing-box PATH]" >&2
    exit 2
fi

bundle=$1
shift
if [ "${SING_BOX+x}" = x ]; then
    exec python3 "$script_dir/wg_hy2_install.py" --bundle "$bundle" \
        --sing-box "$SING_BOX" "$@"
fi
exec python3 "$script_dir/wg_hy2_install.py" --bundle "$bundle" "$@"
