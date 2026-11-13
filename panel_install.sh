#!/usr/bin/env bash
# Prepare panel sources, then perform interactive setup on this server.
set -Eeuo pipefail
umask 077
base='https://raw.githubusercontent.com/1660667086/wg-hy2-installer/main'
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
tmp=$(mktemp -d /tmp/wg-hy2-panel-install.XXXXXXXX)
cleanup() { rm -rf -- "$tmp"; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -m 700 "$tmp/panel"
for file in panel_cli.py panel_backend.py panel_server.py wg_hy2_generate.py panel/index.html; do
    if [[ -f "$script_dir/$file" ]]; then
        cp -- "$script_dir/$file" "$tmp/$file"
    else
        curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
            --output "$tmp/$file" "$base/$file"
    fi
    chmod 600 "$tmp/$file"
done
python3 "$tmp/panel_cli.py" setup "$@"
