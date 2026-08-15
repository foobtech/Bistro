#!/usr/bin/env python3
"""
bistro_apply_theme — takes a theme already sitting in ~/.cache/bistro/
(fetched + validated by bistro_connect.py) and pushes it into kitty:
colors LIVE via remote control, font/cursor-shape persisted for next
launch (kitty doesn't support live font/cursor-shape changes).

Requires kitty configured with a FIXED remote-control socket, not just
'allow_remote_control yes'. This matters because bistro_connect.py's
theme-apply can be triggered two very different ways:
    - interactively, by you running bistro_connect.py inside a kitty
      window (kitty can auto-detect its own window in this case)
    - by bistro_daemon.py running in the background/under systemd,
      which has no kitty window it's "attached to" and so can't rely
      on that auto-detection at all
A fixed socket path works identically in both cases, so that's what
this always targets — no auto-detect fallback, since a background
daemon would silently hang waiting on one that was never going to
resolve (this is exactly the bug that motivated pinning it down).

Add BOTH of these lines to ~/.config/kitty/kitty.conf, then fully quit
kitty (not just close the window — `killall kitty`) and reopen it:
    allow_remote_control yes
    listen_on unix:/tmp/bistro-kitty.sock

Note: kitty automatically appends '-<pid>' to that socket path, so the
real socket ends up being e.g. /tmp/bistro-kitty.sock-31337 — this is
expected, kitty does this so multiple open kitty windows don't collide
on one socket file. This module globs for whatever matches at run
time and applies to every live window it finds, so you don't need to
know the exact PID yourself.

Usage:
    bistro_apply_theme.py <path-to-cached-theme.toml>
    bistro_apply_theme.py --latest        find the most recently
                                           cached theme automatically

Theme schema — theme.toml (colors only, portable across terminals):
all fields except primary/accent are optional, so old minimal themes
with just primary/accent still work unchanged.

    primary = "#3e2723"       # background
    accent = "#efebe9"        # foreground
    cursor = "#efebe9"
    selection_background = "#5d4037"
    selection_foreground = "#efebe9"

    [colors]                  # ANSI 16-color palette, all optional
    color0 = "#000000"
    ...
    color15 = "#ffffff"

kitty.toml schema — separate resource, kitty-specific settings only
(font/cursor shape don't make sense as "theme" data, and kitty has no
live way to apply them, unlike colors):

    font_family = "Fantasque Sans Mono"
    cursor_shape = "block"    # block | beam | underline
"""

from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import tomllib
import subprocess
import shutil
import sys
import re

BISTRO_KITTY_CONF = Path.home() / ".config" / "kitty" / "bistro-theme.conf"
KITTY_CONF = Path.home() / ".config" / "kitty" / "kitty.conf"
CACHE_ROOT = Path.home() / ".cache" / "bistro"

# kitty automatically appends '-<pid>' to unix socket paths given to
# 'listen_on' (so multiple kitty windows don't collide on one socket
# file) — a config line of 'listen_on unix:/tmp/bistro-kitty.sock'
# actually creates /tmp/bistro-kitty.sock-31337, /tmp/bistro-kitty.sock-31402,
# etc, one per running kitty process, NOT a single fixed path. So this
# has to glob for whatever sockets currently exist rather than assume
# one literal path — there may be zero (kitty not running), one, or
# several (multiple kitty windows open).
KITTY_SOCKET_GLOB = "/tmp/bistro-kitty.sock-*"

HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
FONT_NAME_RE = re.compile(r"^[A-Za-z0-9 \-_.]{1,64}$")
VALID_CURSOR_SHAPES = {"block", "beam", "underline"}
ANSI_COLOR_KEYS = [f"color{i}" for i in range(16)]


def find_kitty_sockets() -> list[str]:
    """Every currently-live bistro-managed kitty socket, as 'unix:<path>'
    strings ready to pass to `kitty @ --to`. Empty list means no kitty
    window has this config active right now (not installed, not
    running, or running with stale config from before listen_on was
    added) — apply_live_kitty() turns that into one clear error rather
    than a per-socket one."""
    import glob
    return [f"unix:{path}" for path in glob.glob(KITTY_SOCKET_GLOB)]




class ApplyThemeError(Exception):
    pass


@dataclass
class BistroTheme:
    primary: str
    accent: str
    cursor: str | None = None
    selection_background: str | None = None
    selection_foreground: str | None = None
    colors: dict[str, str] = field(default_factory=dict)


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    if not HEX_RE.match(hex_color):
        raise ApplyThemeError(f"Invalid hex color: {hex_color!r}")
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    return r, g, b


def _validate_hex_field(data: dict, key: str, required: bool = False) -> str | None:
    value = data.get(key)
    if value is None:
        if required:
            raise ApplyThemeError(f"Theme must define '{key}'")
        return None
    if not HEX_RE.match(value):
        raise ApplyThemeError(f"'{key}' must be a 6-digit hex color, got {value!r}")
    return value


def load_theme(path: Path) -> BistroTheme:
    if not path.exists():
        raise ApplyThemeError(f"Theme file not found: {path}")

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ApplyThemeError(f"Malformed theme TOML: {e}")

    primary = _validate_hex_field(data, "primary", required=True)
    accent = _validate_hex_field(data, "accent", required=True)
    cursor = _validate_hex_field(data, "cursor")
    selection_background = _validate_hex_field(data, "selection_background")
    selection_foreground = _validate_hex_field(data, "selection_foreground")

    raw_colors = data.get("colors", {})
    if not isinstance(raw_colors, dict):
        raise ApplyThemeError("'colors' must be a table")
    colors: dict[str, str] = {}
    for key, value in raw_colors.items():
        if key not in ANSI_COLOR_KEYS:
            raise ApplyThemeError(f"Unknown color key {key!r}, expected one of color0..color15")
        if not HEX_RE.match(value):
            raise ApplyThemeError(f"'{key}' must be a 6-digit hex color, got {value!r}")
        colors[key] = value

    return BistroTheme(
        primary=primary, accent=accent, cursor=cursor,
        selection_background=selection_background, selection_foreground=selection_foreground,
        colors=colors,
    )


@dataclass
class KittyConfig:
    """
    Terminal-specific settings that only make sense for kitty (or any
    single terminal) — deliberately kept OUT of BistroTheme, which stays
    portable in case Bistro supports other terminals later. Comes from
    a separate kitty.toml resource, not theme.toml.
    """
    font_family: str | None = None
    cursor_shape: str | None = None


def load_kitty_config(path: Path) -> KittyConfig:
    """Parse and validate a kitty.toml. Every field optional — an empty
    file is valid, it just means 'no kitty-specific overrides'."""
    if not path.exists():
        raise ApplyThemeError(f"kitty.toml not found: {path}")

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ApplyThemeError(f"Malformed kitty.toml: {e}")

    font_family = data.get("font_family")
    if font_family is not None:
        if not isinstance(font_family, str) or not FONT_NAME_RE.match(font_family):
            raise ApplyThemeError(
                f"'font_family' must be letters/digits/spaces/-_. only, "
                f"max 64 chars, got {font_family!r}"
            )

    cursor_shape = data.get("cursor_shape")
    if cursor_shape is not None and cursor_shape not in VALID_CURSOR_SHAPES:
        raise ApplyThemeError(
            f"'cursor_shape' must be one of {sorted(VALID_CURSOR_SHAPES)}, got {cursor_shape!r}"
        )

    return KittyConfig(font_family=font_family, cursor_shape=cursor_shape)


def find_latest_kitty_config() -> Path | None:
    """Find the most recently modified kitty.toml, if any server has
    pushed one. Returns None rather than raising — a kitty.toml is
    optional, unlike a theme."""
    candidates = list(CACHE_ROOT.glob("*/kitty/*.toml"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def find_latest_theme() -> Path:
    candidates = list(CACHE_ROOT.glob("*/theme/*.toml"))
    if not candidates:
        raise ApplyThemeError(
            f"No cached themes found under {CACHE_ROOT}. "
            f"Run bistro_connect.py against a server with --ingest first."
        )
    return max(candidates, key=lambda p: p.stat().st_mtime)


def print_preview(theme: BistroTheme) -> None:
    pr, pg, pb = _hex_to_rgb(theme.primary)
    ar, ag, ab = _hex_to_rgb(theme.accent)

    bg = f"\x1b[48;2;{pr};{pg};{pb}m"
    fg = f"\x1b[38;2;{ar};{ag};{ab}m"
    reset = "\x1b[0m"

    print()
    print(f"{bg}{fg}{'':^40}{reset}")
    print(f"{bg}{fg}{'bistro ~ $':^40}{reset}")
    print(f"{bg}{fg}{'':^40}{reset}")
    print()
    print(f"  primary {theme.primary}   accent {theme.accent}")
    if theme.colors:
        print(f"  {len(theme.colors)} ANSI palette color(s) defined")
    print()


def apply_live_kitty(theme: BistroTheme) -> int:
    """Applies the theme to EVERY currently open kitty window (each has
    its own socket, see find_kitty_sockets()). Returns the count of
    windows successfully themed. Raises only if there's nothing to
    even attempt (kitty not installed, or zero live sockets found) —
    once there's at least one socket, a failure on that specific
    window is collected and reported but doesn't stop the others."""
    if shutil.which("kitty") is None:
        raise ApplyThemeError(
            "kitty is not installed or not on PATH. Install it with: "
            "pacman -S kitty"
        )

    sockets = find_kitty_sockets()
    if not sockets:
        raise ApplyThemeError(
            f"No live kitty sockets found matching {KITTY_SOCKET_GLOB} — "
            f"either kitty isn't running, or it's running with a config "
            f"from before you added 'listen_on unix:/tmp/bistro-kitty.sock' "
            f"to kitty.conf. Fully quit kitty (not just close the window — "
            f"`killall kitty` if needed) and reopen it."
        )

    color_args = [f"background={theme.primary}", f"foreground={theme.accent}"]
    if theme.cursor:
        color_args.append(f"cursor={theme.cursor}")
    if theme.selection_background:
        color_args.append(f"selection_background={theme.selection_background}")
    if theme.selection_foreground:
        color_args.append(f"selection_foreground={theme.selection_foreground}")
    for key, value in theme.colors.items():
        color_args.append(f"{key}={value}")

    succeeded = 0
    errors = []
    for socket in sockets:
        args = ["kitty", "@", "--to", socket, "set-colors", "--all", *color_args]
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            errors.append(f"{socket}: timed out after 10s (kitty may be unresponsive)")
            continue

        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "remote control" in stderr.lower() or "disabled" in stderr.lower():
                errors.append(f"{socket}: remote control disabled for this window")
            else:
                errors.append(f"{socket}: {stderr}")
            continue

        succeeded += 1

    if succeeded == 0:
        raise ApplyThemeError(
            f"Found {len(sockets)} kitty socket(s) but couldn't apply to any: "
            f"{'; '.join(errors)}"
        )

    return succeeded


def write_persistent_config(theme: BistroTheme, kitty_config: KittyConfig | None = None) -> bool:
    lines = [
        "# Generated by bistro_apply_theme.py — regenerate, don't hand-edit.",
        f"background {theme.primary}",
        f"foreground {theme.accent}",
    ]
    if theme.cursor:
        lines.append(f"cursor {theme.cursor}")
    if theme.selection_background:
        lines.append(f"selection_background {theme.selection_background}")
    if theme.selection_foreground:
        lines.append(f"selection_foreground {theme.selection_foreground}")
    if kitty_config and kitty_config.font_family:
        lines.append(f"font_family {kitty_config.font_family}")
    if kitty_config and kitty_config.cursor_shape:
        lines.append(f"cursor_shape {kitty_config.cursor_shape}")
    for key, value in sorted(theme.colors.items()):
        lines.append(f"{key} {value}")

    BISTRO_KITTY_CONF.parent.mkdir(parents=True, exist_ok=True)
    BISTRO_KITTY_CONF.write_text("\n".join(lines) + "\n", encoding="utf-8")

    include_line = "include bistro-theme.conf"
    if KITTY_CONF.exists():
        existing = KITTY_CONF.read_text(encoding="utf-8")
        if include_line in existing:
            return False
    else:
        existing = ""

    with open(KITTY_CONF, "a", encoding="utf-8") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(f"{include_line}\n")
    return True


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1

    try:
        if argv[1] == "--latest":
            theme_path = find_latest_theme()
        else:
            theme_path = Path(argv[1])

        theme = load_theme(theme_path)
        name = theme_path.stem

        # kitty.toml is optional — a server might push only a theme, no
        # font/cursor overrides. Missing/absent is not an error here.
        kitty_config = None
        kitty_path = find_latest_kitty_config()
        if kitty_path:
            kitty_config = load_kitty_config(kitty_path)

        print(f"Applying theme: {name} ({theme_path})")
        print_preview(theme)
        if kitty_config:
            if kitty_config.font_family:
                print(f"  font: {kitty_config.font_family} (applies on next kitty restart)")
            if kitty_config.cursor_shape:
                print(f"  cursor shape: {kitty_config.cursor_shape} (applies on next kitty restart)")
            print()

        applied_count = apply_live_kitty(theme)
        window_word = "window" if applied_count == 1 else "windows"
        print(f"Applied live to {applied_count} kitty {window_word} — should have just repainted.")

        needs_restart = write_persistent_config(theme, kitty_config)
        print(f"Saved to {BISTRO_KITTY_CONF} so it persists across restarts.")
        if (kitty_config and (kitty_config.font_family or kitty_config.cursor_shape)) or needs_restart:
            print("Restart kitty to pick up font/cursor-shape changes "
                  "(colors are already live).")

    except ApplyThemeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
