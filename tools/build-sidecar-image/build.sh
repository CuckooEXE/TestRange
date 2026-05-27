#!/usr/bin/env bash
# Build the testrange sidecar VM image: Alpine + dnsmasq + nftables +
# qemu-guest-agent, plus a small OpenRC hook that picks up the per-run
# config from a TR_SIDECAR_CFG-labeled ISO at boot.
#
# Requires root (alpine-make-vm-image uses chroot internally). Output:
# ./testrange-sidecar.qcow2 alongside this script. The orchestrator
# expects the image in the cache under the pretty-name "testrange-sidecar".
#
# Usage:
#   sudo ./tools/build-sidecar-image/build.sh
#   testrange cache add tools/build-sidecar-image/testrange-sidecar.qcow2 \
#       --name testrange-sidecar

set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
out="$here/testrange-sidecar.qcow2"

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
    echo "build.sh: must run as root (chroot + losetup)" >&2
    exit 1
fi

# alpine-make-vm-image: small upstream script that builds an Alpine
# qcow2 by extracting the minirootfs into a loopback image and running
# `apk` + a customize hook inside.
mk="${ALPINE_MAKE_VM_IMAGE:-alpine-make-vm-image}"
if ! command -v "$mk" >/dev/null 2>&1; then
    echo "build.sh: $mk not on PATH; install from https://github.com/alpinelinux/alpine-make-vm-image" >&2
    exit 1
fi

customize_dir="$(mktemp -d)"
customize="$customize_dir/customize.sh"
trap 'rm -rf "$customize_dir"' EXIT

cat >"$customize" <<'CUSTOMIZE'
#!/usr/bin/env sh
# Runs inside the new image's chroot. Stage an OpenRC service that
# copies per-run config off a TR_SIDECAR_CFG-labeled ISO at boot.
set -eu

# Init service: mount the config ISO (best-effort) and stage files.
cat >/etc/init.d/testrange-sidecar <<'INIT'
#!/sbin/openrc-run
description="testrange sidecar config staging"
depend() {
    before networking dnsmasq nftables
}
start() {
    ebegin "staging testrange sidecar config from TR_SIDECAR_CFG"
    mp=$(mktemp -d)
    dev=$(blkid -L TR_SIDECAR_CFG 2>/dev/null || true)
    if [ -z "$dev" ]; then
        eend 0 "no TR_SIDECAR_CFG device; skipping"
        return 0
    fi
    if ! mount -r -t iso9660 "$dev" "$mp" 2>/dev/null; then
        eend 0 "could not mount $dev; skipping"
        return 0
    fi
    [ -f "$mp/dnsmasq.conf" ] && cp "$mp/dnsmasq.conf" /etc/dnsmasq.conf
    [ -f "$mp/interfaces" ]   && cp "$mp/interfaces"   /etc/network/interfaces
    [ -f "$mp/nftables.nft" ] && cp "$mp/nftables.nft" /etc/nftables.nft
    [ -f "$mp/sysctl.conf" ]  && cp "$mp/sysctl.conf"  /etc/sysctl.d/99-testrange.conf
    umount "$mp" || true
    rmdir "$mp" || true
    sysctl -p /etc/sysctl.d/99-testrange.conf >/dev/null 2>&1 || true
    eend 0
}
INIT
chmod +x /etc/init.d/testrange-sidecar

# Boot ordering: stage config in `boot` runlevel (before `default`'s
# networking/dnsmasq/nftables).
rc-update add testrange-sidecar boot
rc-update add networking default
rc-update add dnsmasq default
rc-update add nftables default
rc-update add qemu-guest-agent default
CUSTOMIZE
chmod +x "$customize"

echo "build.sh: building $out"
# Remove any pre-existing output so alpine-make-vm-image creates a fresh
# qcow2 (it tries to attach an existing file as-is, which fails when the
# file is a stale image or a zero-byte placeholder).
rm -f "$out"

# `--script-chroot` is a FLAG (binds the script's dir at /mnt inside the
# image and chroots into it). The script itself is a POSITIONAL argument
# after `<image>`. `--` ends option parsing.
"$mk" \
    --image-format qcow2 \
    --image-size 1G \
    --packages "dnsmasq nftables qemu-guest-agent openrc blkid" \
    --script-chroot \
    -- \
    "$out" \
    "$customize"

echo
echo "build.sh: built $out"
echo "next: testrange cache add $out --name testrange-sidecar"
