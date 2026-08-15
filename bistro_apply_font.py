#!/usr/bin/env python3
"""
bistro_apply_font — takes a font already sitting in ~/.cache/bistro/
(fetched + sandboxed/re-saved by bistro_ingest_asset.py via fontTools)
and installs it where fontconfig will actually find it.

WHY THIS EXISTS: a server can push a kitty.toml with font_family =
"Fantasque Sans Mono", and bistro_apply_theme.py will happily write
that into bistro-theme.conf — but if the font isn't actually installed
on this machine, kitty just silently falls back to its default. No
error, no signal. This script is what makes font_family real instead
of a request that might just get ignored.

The font file itself was already parsed and re-saved by fontTools
inside a bwrap sandbox during ingest (see bistro_sandbox_process.sh) —
this script only ever touches that already-sandboxed local file, never
raw server bytes. It doesn't reopen any trust boundary.

Usage:
    bistro_apply_font.py <path-to-cached-font.ttf|woff2>
    bistro_apply_font.py --latest

What it does:
    1. Copies the font into ~/.local/share/fonts/bistro/
    2. Runs `fc-cache -f` on that directory so fontconfig picks it up
       immediately, without needing a logout/relogin
    3. Reads the font's own name table (via fontTools — same library
       already used to validate it during ingest) and reports the
       family name fontconfig will actually register it under, so you
       can immediately tell if it matches what a kitty.toml expects
"""

from __future__ import annotations
from pathlib import Path
import subprocess
import shutil
import sys

CACHE_ROOT = Path.home() / ".cache" / "bistro"
FONT_INSTALL_DIR = Path.home() / ".local" / "share" / "fonts" / "bistro"


class ApplyFontError(Exception):
    pass


def find_latest_font() -> Path:
    candidates = list(CACHE_ROOT.glob("*/font/*"))
    if not candidates:
        raise ApplyFontError(
            f"No cached fonts found under {CACHE_ROOT}. "
            f"Run bistro_connect.py against a server with --ingest first."
        )
    return max(candidates, key=lambda p: p.stat().st_mtime)


def read_family_name(font_path: Path) -> str | None:
    """Best-effort read of the font's actual family name from its own
    name table. Returns None rather than raising — a font that's
    already passed the sandboxed fontTools re-save during ingest is
    trusted enough to install even if this particular read fails."""
    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        return None

    try:
        font = TTFont(str(font_path), lazy=True)
        name_table = font["name"]
        # nameID 1 = Font Family name, platformID 3 (Windows) preferred,
        # fall back to whatever's present
        best = name_table.getDebugName(1)
        return best
    except Exception:
        return None


def install_font(font_path: Path) -> Path:
    if not font_path.exists():
        raise ApplyFontError(f"Font file not found: {font_path}")

    if font_path.suffix.lower() not in (".ttf", ".woff2"):
        raise ApplyFontError(
            f"Unexpected font extension {font_path.suffix!r} — "
            f"expected .ttf or .woff2 (only formats bistro_sandbox_process.sh handles)"
        )

    FONT_INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    dest = FONT_INSTALL_DIR / font_path.name
    shutil.copyfile(font_path, dest)
    return dest


def refresh_font_cache() -> None:
    if shutil.which("fc-cache") is None:
        raise ApplyFontError(
            "fc-cache not found. Install fontconfig with: pacman -S fontconfig"
        )

    result = subprocess.run(
        ["fc-cache", "-f", str(FONT_INSTALL_DIR)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise ApplyFontError(f"fc-cache failed: {result.stderr.strip()}")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1

    try:
        if argv[1] == "--latest":
            font_path = find_latest_font()
        else:
            font_path = Path(argv[1])

        print(f"Installing font: {font_path.name}")
        dest = install_font(font_path)
        print(f"Copied to {dest}")

        refresh_font_cache()
        print("Refreshed fontconfig cache.")

        family_name = read_family_name(dest)
        if family_name:
            print(f"Registered family name: {family_name!r}")
            print(
                "If a kitty.toml's font_family doesn't match this string "
                "exactly, kitty will silently fall back to its default — "
                "worth double-checking against the server's kitty.toml."
            )
        else:
            print(
                "Couldn't read the family name back out (fontTools not "
                "available, or an unusual font) — installed anyway, but "
                "you'll want to confirm the name manually with fc-list."
            )

    except ApplyFontError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
