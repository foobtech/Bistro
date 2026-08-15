#!/usr/bin/env bash
# bistro-vm-provision.sh — run this INSIDE the VM, after Arch is
# installed and you've booted into it (NOT the live ISO — a real
# installed system, logged in as a normal user with sudo).
#
# Installs Hyprland + a minimal but complete session (terminal, bar,
# wallpaper daemon), pulls down Bistro, wires up kitty's remote-control
# socket (the same fixed-socket fix from bistro_apply_theme.py — a VM
# is yet another context with no ambient kitty auto-detection, same as
# the daemon), and sets up the systemd service.
#
# Usage:
#   chmod +x bistro-vm-provision.sh
#   ./bistro-vm-provision.sh

set -euo pipefail

if [ "$EUID" -eq 0 ]; then
    echo "Don't run this as root — run it as your normal user. It'll" >&2
    echo "prompt for sudo for the parts that actually need it." >&2
    exit 1
fi

echo "=== Installing Hyprland + minimal session ==="
sudo pacman -Sy --needed --noconfirm \
    hyprland kitty waybar hyprpaper \
    python python-pip python-fonttools \
    ffmpeg fontconfig bubblewrap \
    git base-devel \
    xdg-desktop-portal-hyprland \
    qt5-wayland qt6-wayland

echo
echo "=== Cloning Bistro ==="
if [ -d "$HOME/Bistro" ]; then
    echo "$HOME/Bistro already exists, skipping clone — pull manually if you want updates."
else
    git clone https://github.com/foobtech/Bistro.git "$HOME/Bistro"
fi

echo
echo "=== Configuring kitty remote control (fixed socket, same fix as the daemon) ==="
mkdir -p "$HOME/.config/kitty"
KITTY_CONF="$HOME/.config/kitty/kitty.conf"
if ! grep -q "allow_remote_control" "$KITTY_CONF" 2>/dev/null; then
    echo "allow_remote_control yes" >> "$KITTY_CONF"
fi
if ! grep -q "listen_on" "$KITTY_CONF" 2>/dev/null; then
    echo "listen_on unix:/tmp/bistro-kitty.sock" >> "$KITTY_CONF"
fi

echo
echo "=== Minimal Hyprland config ==="
mkdir -p "$HOME/.config/hypr"
HYPR_CONF="$HOME/.config/hypr/hyprland.conf"
if [ ! -f "$HYPR_CONF" ]; then
    cat > "$HYPR_CONF" <<'EOF'
# Minimal test config — just enough to get a usable session for
# testing Bistro. Expand as needed once the basics are confirmed working.

monitor=,preferred,auto,1

exec-once = waybar
exec-once = kitty

input {
    kb_layout = us
}

general {
    gaps_in = 4
    gaps_out = 8
    border_size = 2
}

# Bistro's own theming will start overriding colors live once you
# connect to a server — these are just sane starting defaults.
decoration {
    rounding = 6
}

bind = SUPER, RETURN, exec, kitty
bind = SUPER, Q, killactive
bind = SUPER, M, exit
EOF
fi

echo
echo "=== Setting up bistro-daemon systemd service ==="
mkdir -p "$HOME/.config/systemd/user"
cp "$HOME/Bistro/bistro-daemon.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable bistro-daemon.service
# Deliberately NOT starting it yet — first login to Hyprland, confirm
# kitty itself launches and looks right, THEN start the daemon so any
# problem you hit is isolated to one layer at a time instead of
# debugging Hyprland and Bistro simultaneously.
echo "(Service enabled but not started — start it manually once you've confirmed Hyprland + kitty work on their own: systemctl --user start bistro-daemon.service)"

echo
echo "=== Done ==="
echo "Log out and start a Hyprland session (from a TTY: Hyprland)."
echo "Once kitty opens and looks normal, subscribe to a server and start the daemon:"
echo "  cd ~/Bistro"
echo "  python3 bistro_subscribe.py add https://foobtech.github.io/bistro-test-server"
echo "  systemctl --user start bistro-daemon.service"
echo "  journalctl --user -u bistro-daemon -f"
