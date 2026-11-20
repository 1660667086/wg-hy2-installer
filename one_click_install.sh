#!/usr/bin/env bash
# One-click server bootstrap for a generated role directory.
# It downloads only the pinned official sing-box release when no compatible
# binary is already available, verifies the pinned SHA-256, then delegates to
# the transactional Python installer.
set -Eeuo pipefail

readonly VERSION="1.14.4"
readonly RELEASE_BASE="https://github.com/SagerNet/sing-box/releases/download/v${VERSION}"

die() {
    echo "error: $*" >&2
    exit 1
}

usage() {
    echo "Usage: sudo $0 ROLE_DIR [--no-enable] [--dry-run] [--name NAME] [--sing-box PATH]" >&2
    echo "       SING_BOX=/path/to/sing-box sudo $0 ROLE_DIR [options]" >&2
}

[[ $EUID -eq 0 ]] || die "run as root (for example: sudo $0 ...)"
(( $# >= 1 )) || { usage; exit 2; }
[[ $(uname -s) == "Linux" ]] || die "this server installer supports Linux only"

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
bundle=$1
shift
[[ -d "$bundle" ]] || die "role directory does not exist: $bundle"
command -v python3 >/dev/null 2>&1 || die "python3 is required"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' \
    || die "Python 3.8 or newer is required (found: $(python3 -V 2>&1))"

manual_binary=""
dry_run=0
installer_args=("$@")
for ((i = 0; i < ${#installer_args[@]}; i++)); do
    case "${installer_args[i]}" in
        --sing-box)
            ((i + 1 < ${#installer_args[@]})) || die "--sing-box needs a path"
            manual_binary=${installer_args[i + 1]}
            i=$((i + 1))
            ;;
        --sing-box=*) manual_binary=${installer_args[i]#--sing-box=} ;;
        --dry-run) dry_run=1 ;;
    esac
done

compatible_binary() {
    local candidate=$1 info
    [[ -x "$candidate" ]] || return 1
    info=$("$candidate" version 2>/dev/null || true)
    grep -Eq "^sing-box version ${VERSION}([[:space:]]|$)" <<<"$info" || return 1
    grep -q 'with_quic' <<<"$info" || return 1
    grep -q 'with_wireguard' <<<"$info" || return 1
    grep -q 'with_gvisor' <<<"$info" || return 1
}

asset_for_arch() {
    local machine
    machine=$(uname -m)
    case "$machine" in
        x86_64|amd64)
            asset="sing-box-${VERSION}-linux-amd64.tar.gz"
            digest="699a1b510aa4396b2176d32d1181920399ebf1e267835da6f3b598d0f7c24968"
            arch="amd64" ;;
        aarch64|arm64)
            asset="sing-box-${VERSION}-linux-arm64.tar.gz"
            digest="436441f76ee2f7fe21b5b2d426d3cb261a480455a704865789ad533e4273ec26"
            arch="arm64" ;;
        i386|i686)
            asset="sing-box-${VERSION}-linux-386.tar.gz"
            digest="efb46daaa48ee55e090bd1dfcaee34bfbe9bdbd9b89fa9f328d25f81a619e865"
            arch="386" ;;
        armv5*)
            asset="sing-box-${VERSION}-linux-armv5.tar.gz"
            digest="683e377e519816fe192b2e9878f259f8d3e1176624ac11e7c6733d997f41454c"
            arch="armv5" ;;
        armv6*)
            asset="sing-box-${VERSION}-linux-armv6.tar.gz"
            digest="5aa85015945c29261bb640dedcc27bc7e71e103ce1daf0b9d798f1de5af971ee"
            arch="armv6" ;;
        armv7*|armhf)
            asset="sing-box-${VERSION}-linux-armv7.tar.gz"
            digest="fd164ed45ebc784f657a3e1d0e0ad0f68afdcff864ee57122c2cfed3ddb80a00"
            arch="armv7" ;;
        loongarch64|loong64)
            asset="sing-box-${VERSION}-linux-loong64.tar.gz"
            digest="83569a90fe785fa6c74f0db51af62853b549963deab51500e035af8d0d781ca6"
            arch="loong64" ;;
        ppc64le)
            asset="sing-box-${VERSION}-linux-ppc64le.tar.gz"
            digest="d5fdb4cf17c1952391308574e236dc47029020ce7fdee5528cab4e17c780b13c"
            arch="ppc64le" ;;
        riscv64)
            asset="sing-box-${VERSION}-linux-riscv64.tar.gz"
            digest="6499842fe63be341eb9517bfecc70d676ca38d26d9f82730b6dd52491f0970db"
            arch="riscv64" ;;
        s390x)
            asset="sing-box-${VERSION}-linux-s390x.tar.gz"
            digest="316b2fe8217d34aeab89782712246473d114850b065ad58ecf73daabcb58bbe9"
            arch="s390x" ;;
        *)
            die "unsupported Linux architecture: $machine; pass --sing-box PATH"
            ;;
    esac
}

sha256_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d ' ' -f 1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | cut -d ' ' -f 1
    else
        die "sha256sum or shasum is required to verify the sing-box archive"
    fi
}

download_tmp=""

download_binary() {
    asset_for_arch
    command -v curl >/dev/null 2>&1 || die "curl is required to download sing-box"
    command -v tar >/dev/null 2>&1 || die "tar is required to unpack sing-box"

    local cache_dir="/usr/local/libexec/wg-hy2"
    local target="${cache_dir}/sing-box-${VERSION}-${arch}"
    if [[ -e "$target" ]]; then
        compatible_binary "$target" || die "existing $target is not a compatible sing-box ${VERSION}"
        printf '%s\n' "$target"
        return
    fi

    local archive extracted actual
    download_tmp=$(mktemp -d "${TMPDIR:-/tmp}/wg-hy2-download.XXXXXX")
    trap 'if [[ -n "${download_tmp:-}" ]]; then rm -rf -- "$download_tmp"; fi' EXIT
    archive="${download_tmp}/${asset}"
    echo "Downloading verified sing-box ${VERSION} for ${arch}..." >&2
    curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
        --output "$archive" "${RELEASE_BASE}/${asset}"
    actual=$(sha256_file "$archive")
    [[ "$actual" == "$digest" ]] || die "sing-box archive SHA-256 mismatch"
    mkdir -p -m 0755 "$cache_dir"
    mkdir -p "$download_tmp/extract"
    tar -xzf "$archive" -C "$download_tmp/extract"
    extracted=$(find "$download_tmp/extract" -type f -name sing-box -print -quit)
    [[ -n "$extracted" ]] || die "sing-box archive did not contain its executable"
    install -m 0755 "$extracted" "$target"
    compatible_binary "$target" || die "downloaded sing-box failed version/tag validation"
    rm -rf -- "$download_tmp"
    download_tmp=""
    trap - EXIT
    printf '%s\n' "$target"
}

if [[ -n "$manual_binary" ]]; then
    binary=$manual_binary
elif candidate=$(command -v sing-box 2>/dev/null) && compatible_binary "$candidate"; then
    binary=$candidate
elif (( dry_run )); then
    die "--dry-run needs an existing compatible sing-box; pass --sing-box PATH"
else
    binary=$(download_binary)
fi

# ${array[@]+...} keeps an empty argument list working under `set -u` in
# bash releases before 4.4, which treat an empty "${array[@]}" as unbound.
if [[ -n "$manual_binary" ]]; then
    exec python3 "$script_dir/wg_hy2_install.py" --bundle "$bundle" \
        ${installer_args[@]+"${installer_args[@]}"}
fi
exec python3 "$script_dir/wg_hy2_install.py" --bundle "$bundle" \
    --sing-box "$binary" ${installer_args[@]+"${installer_args[@]}"}
