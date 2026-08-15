#!/usr/bin/env bash
# bistro-vm-create.sh — creates a libvirt/QEMU VM for testing Bistro
# under a real Hyprland session, isolated from your main machine.
#
# RUN THIS ON YOUR HOST (not inside any VM), with an Arch ISO already
# downloaded. It boots you into the Arch installer; from there, install
# Arch normally (or via Amphi), then run bistro-vm-provision.sh INSIDE
# the VM afterward.
#
# CRITICAL: Hyprland needs real GPU acceleration to render anything.
# A default virt-install VM gives you a plain framebuffer with no GL,
# and Hyprland will either crash on launch or just show a black screen
# — this is the single most common way people give up on "Hyprland in
# a VM" thinking it's broken when it's actually just a graphics config
# problem. This script requests virtio-gpu with 3D acceleration
# (accel3d) and SPICE with gl.enable=on specifically to avoid that.
#
# This does mean: you need to run this from an actual local desktop
# session on your host (SPICE GL doesn't work over a remote/SSH
# connection with no local display) — which matches your setup, since
# you're testing this locally anyway.
#
# Usage:
#   ./bistro-vm-create.sh /path/to/archlinux.iso
#
# Adjust MEMORY/VCPUS/DISK_SIZE below if the defaults don't fit your
# machine.

set -euo pipefail

ISO_PATH="${1:?Usage: $0 /path/to/archlinux.iso}"
VM_NAME="bistro-test"
DISK_PATH="$HOME/.local/share/libvirt/images/${VM_NAME}.qcow2"
DISK_SIZE="20G"
MEMORY="4096"    # MB — Hyprland + kitty + a browser for testing is fine at 4G, bump if it feels tight
VCPUS="2"

if [ ! -f "$ISO_PATH" ]; then
    echo "Error: ISO not found at $ISO_PATH" >&2
    exit 1
fi

if ! command -v virt-install &>/dev/null; then
    echo "virt-install not found. On Arch: pacman -S virt-install libvirt qemu-desktop" >&2
    echo "Then: systemctl enable --now libvirtd, and add yourself to the libvirt group." >&2
    exit 1
fi

if [ ! -e /dev/kvm ]; then
    echo "Warning: /dev/kvm not found — VM will run unaccelerated (very slow) or fail to start." >&2
    echo "Check that virtualization (VT-x/AMD-V) is enabled in your BIOS and the kvm module is loaded." >&2
fi

mkdir -p "$(dirname "$DISK_PATH")"

if [ -f "$DISK_PATH" ]; then
    echo "A VM disk already exists at $DISK_PATH."
    read -p "Overwrite it and start fresh? [y/N] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Aborted — remove the disk yourself first if you want to recreate it."
        exit 1
    fi
    rm -f "$DISK_PATH"
fi

# A previous attempt that got as far as disk allocation but then failed
# (e.g. a missing network, like the 'default' network not existing
# under qemu:///session) still leaves the domain DEFINED — a plain
# rerun would then hit "domain already exists" even though nothing
# actually booted. Clean that up so reruns are always safe to just
# try again.
if virsh --connect qemu:///session dominfo "$VM_NAME" &>/dev/null; then
    echo "Found a leftover domain definition from a previous attempt — removing it."
    virsh --connect qemu:///session undefine "$VM_NAME" --nvram 2>/dev/null || \
        virsh --connect qemu:///session undefine "$VM_NAME"
fi

echo "Creating VM '$VM_NAME': ${MEMORY}MB RAM, ${VCPUS} vCPUs, ${DISK_SIZE} disk"

virt-install \
    --name "$VM_NAME" \
    --memory "$MEMORY" \
    --vcpus "$VCPUS" \
    --disk path="$DISK_PATH",size=${DISK_SIZE%G},format=qcow2,bus=virtio \
    --cdrom "$ISO_PATH" \
    --os-variant archlinux \
    --network user,model=virtio \
    --graphics spice,gl.enable=on,listen=none \
    --video virtio,accel3d=on \
    --channel spicevmc \
    --sound ich9 \
    --boot uefi \
    --noautoconsole

echo
echo "VM created and starting. Connect to its display with:"
echo "  virt-viewer --connect qemu:///session $VM_NAME"
echo "  (or: virt-manager, and open '$VM_NAME' from the list)"
echo
echo "From here: install Arch as normal (archinstall, or your Amphi"
echo "installer if you want to test that path too), then reboot into"
echo "the installed system and run bistro-vm-provision.sh inside it."
