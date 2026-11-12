#!/usr/bin/env bash
# GitHub-pulled bootstrap. The role directory is deliberately supplied
# separately because it contains private keys and is never stored in GitHub.
set -Eeuo pipefail

readonly RAW_BASE="https://raw.githubusercontent.com/1660667086/wg-hy2-installer/main"

die() {
    echo "error: $*" >&2
    exit 1
}

if (( $# < 1 )); then
    echo "Usage: curl -fsSL ${RAW_BASE}/bootstrap.sh | sudo bash -s -- ROLE_DIR [--no-enable] [--dry-run] [--sing-box PATH]" >&2
    exit 2
fi
[[ $EUID -eq 0 ]] || die "run through sudo or as root"
command -v curl >/dev/null 2>&1 || die "curl is required"
command -v python3 >/dev/null 2>&1 || die "python3 is required"

tmp=$(mktemp -d "${TMPDIR:-/tmp}/wg-hy2-github-bootstrap.XXXXXX")
cleanup() { rm -rf -- "$tmp"; }
trap cleanup EXIT INT TERM
chmod 700 "$tmp"

curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
    --output "$tmp/one_click_install.sh" "$RAW_BASE/one_click_install.sh"
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
    --output "$tmp/wg_hy2_install.py" "$RAW_BASE/wg_hy2_install.py"
chmod 700 "$tmp/one_click_install.sh" "$tmp/wg_hy2_install.py"

exec "$tmp/one_click_install.sh" "$@"
