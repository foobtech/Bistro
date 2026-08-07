```
████  █████  ████ █████ ████   ███ 
█   █   █   █       █   █   █ █   █
█   █   █   █       █   █   █ █   █
████    █    ███    █   ████  █   █
█   █   █       █   █   █ █   █   █
█   █   █       █   █   █  █  █   █
████  █████ ████    █   █   █  ███ 
```

# Bistro

An Arch-based Linux distro built around community servers.

Bistro connects to community-run "servers" that push down themes, fonts,
wallpapers, and ASCII art, hot-reloading your terminal environment (via
Hyprland) to match that server's atmosphere — like Discord server theming,
but for your actual desktop.

## Core pieces

- **Base**: Arch Linux
- **Compositor**: Hyprland (Wayland) — smooth animations, rounded corners,
  background blur
- **Command prompt**: `bistro ~ $` — plain text, no logo glyph
- **Unified command**: `myserver` — one verb for all server management,
  profiles, roles, and badges (see [Concept](#concept) below)

## Built-in Flavor Profiles

Five low-contrast themes ship out of the box, each with a matched animated
backdrop:

| Profile | Palette | Backdrop |
|---|---|---|
| Espresso Roast | Deep brown / warm cream | Rainy coffee shop window |
| Matcha Latte | Desaturated forest green | Sunlit greenhouse |
| Chai Spice | Terracotta / burnt orange | Flickering fireplace |
| Earl Grey | Slate blue / lavender | Misty mountain train window |
| Vanilla Cream | Off-white / charcoal | Light mode, no backdrop |

## Security architecture

Bistro's asset streaming pipeline (server → client theme/font/wallpaper
delivery) runs every incoming asset through four layers before it's ever
trusted:

1. **Path validation** (`security/bistro_asset_security.py`) — every
   server-supplied path is resolved and verified to stay inside its
   sandboxed cache directory before anything is written.
2. **Byte-level checks** (`security/bistro_asset_security.py`) — size caps
   and magic-byte sniffing reject obviously spoofed or oversized files.
3. **Sandboxed processing** (`security/bistro_sandbox_process.sh`) — fonts
   and wallpapers are parsed/re-encoded inside a locked-down `bubblewrap`
   sandbox (no network, no real filesystem access) so a parser exploit
   can't reach anything real.
4. **Ingestion pipeline** (`security/bistro_ingest_asset.py`) — chains all
   of the above together; nothing reaches `~/.cache/bistro/` unless it
   survives every step.

This is layer 1 of a planned multi-layer defense — a community-maintained
hash/domain blocklist repo is planned as a second line of defense on top
of this, not a replacement for it.

### Status

- [x] Path validation + traversal protection
- [x] Byte-level size/format checks
- [x] Bubblewrap sandboxing (font + wallpaper paths, tested end-to-end)
- [x] Full ingestion pipeline (validate → sandbox → cache)
- [ ] Community hash/domain blocklist repo
- [ ] Signed server manifests
- [ ] Server directory structure (`server.toml` / `state.toml` parsing)
- [ ] `myserver` command implementation

## Repo layout

```
bistro/
└── security/
    ├── bistro_asset_security.py   # path + byte validation
    ├── bistro_sandbox_process.sh  # bubblewrap sandbox wrapper
    └── bistro_ingest_asset.py     # full ingestion pipeline
```

## Requirements

- `bubblewrap` (`pacman -S bubblewrap`)
- `python-fonttools` (`pacman -S python-fonttools`)
- `ffmpeg`

## Concept

Servers are modular directories host operators can share or version-control:

```
my-cozy-server/
├── server.toml       # static rules, roles, resource paths
├── state.toml         # dynamic user/role/stat database
└── resources/
    ├── themes/
    ├── fonts/
    ├── ascii/
    └── wallpapers/
```

When you connect to a server, it streams assets into your local cache and
Hyprland hot-reloads to match. `bistro font lock` / `bistro theme override`
always force you back to a built-in fallback if a server's design hampers
readability.

Roles and badges (Global OS milestones + local server-granted badges) are
managed through `myserver` — `myserver profile @user`, `myserver roles`,
`myserver promote @user helper`, `myserver award`.

## License

TBD
