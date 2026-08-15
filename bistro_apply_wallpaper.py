#!/usr/bin/env python3
"""
bistro_apply_wallpaper — takes a wallpaper already sitting in
~/.cache/bistro/ (fetched, sandboxed, and transcoded to mp4 by
bistro_ingest_asset.py) and sets it as the desktop background.

IMPORTANT LIMITATION, stated plainly: the current sandboxed wallpaper
pipeline (bistro_sandbox_process.sh) always transcodes to mp4 — it does
not yet support setting an animated/looping background live. This tool
extracts a single still frame from that already-sandboxed mp4 and
applies THAT as a static wallpaper. True animated wallpaper support
(via swww or mpvpaper on Hyprland) is a real future increment, not
something this script pretends to do.

The frame extraction step operates on the ALREADY-SANDBOXED local mp4,
never on raw server bytes — it doesn't reopen any trust boundary.

Usage:
    bistro_apply_wallpaper.py <path-to-cached-wallpaper.mp4>
    bistro_apply_wallpaper.py --latest

Desktop targets:
    - GNOME: applied live via gsettings (works today, testable)
    - Hyprland: swaybg command PRINTED for reference (not run here —
      there's no running Hyprland session to hand it to yet). Real
      Hyprland integration is a follow-up once Bistro actually runs
      under Hyprland day-to-day rather than being tested from GNOME.
"""

from __future__ import annotations
from pathlib import Path
import subprocess
import shutil
import sys

CACHE_ROOT = Path.home() / ".cache" / "bistro"
DERIVED_DIR = Path.home() / ".cache" / "bistro" / "_derived" / "wallpapers"


class ApplyWallpaperError(Exception):
    pass


def find_latest_wallpaper() -> Path:
    candidates = list(CACHE_ROOT.glob("*/wallpaper/*"))
    # Exclude our own derived output directory from the search
    candidates = [c for c in candidates if "_derived" not in c.parts]
    if not candidates:
        raise ApplyWallpaperError(
            f"No cached wallpapers found under {CACHE_ROOT}. "
            f"Run bistro_connect.py against a server with --ingest first."
        )
    return max(candidates, key=lambda p: p.stat().st_mtime)


def extract_still_frame(video_path: Path) -> Path:
    """
    Pull a single frame out of the (already sandboxed) wallpaper video
    to use as a static background. This is a LOCAL, post-sandbox
    operation — the video already passed through bwrap + ffmpeg
    transcoding once during ingest; this step never touches raw
    server-supplied bytes.
    """
    if shutil.which("ffmpeg") is None:
        raise ApplyWallpaperError("ffmpeg not found. Install it with: pacman -S ffmpeg")

    DERIVED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DERIVED_DIR / f"{video_path.stem}.png"

    result = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video_path),
         "-frames:v", "1", "-q:v", "2", str(out_path)],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise ApplyWallpaperError(f"Frame extraction failed: {result.stderr.strip()}")
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise ApplyWallpaperError("Frame extraction produced no output")

    return out_path


def apply_gnome(image_path: Path) -> None:
    """Set the wallpaper live via gsettings — works on any GNOME session,
    which is what's actually testable right now."""
    if shutil.which("gsettings") is None:
        raise ApplyWallpaperError("gsettings not found — not a GNOME session?")

    uri = f"file://{image_path}"
    for key in ("picture-uri", "picture-uri-dark"):
        result = subprocess.run(
            ["gsettings", "set", "org.gnome.desktop.background", key, uri],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            raise ApplyWallpaperError(f"gsettings set {key} failed: {result.stderr.strip()}")

    subprocess.run(
        ["gsettings", "set", "org.gnome.desktop.background", "picture-options", "zoom"],
        capture_output=True, text=True, timeout=10,
    )


def print_hyprland_equivalent(image_path: Path) -> None:
    """Not run — no Hyprland session exists to hand this to during
    testing. Printed so the command is ready once Bistro is actually
    running under Hyprland day-to-day."""
    print("\nHyprland equivalent (not run — no active Hyprland session):")
    print(f"  swaybg -i \"{image_path}\" -m fill &")
    print("  (add to your Hyprland config's exec-once for it to persist across restarts)")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1

    try:
        if argv[1] == "--latest":
            video_path = find_latest_wallpaper()
        else:
            video_path = Path(argv[1])

        if not video_path.exists():
            raise ApplyWallpaperError(f"Wallpaper file not found: {video_path}")

        print(f"Applying wallpaper: {video_path.name}")
        still = extract_still_frame(video_path)
        print(f"Extracted still frame: {still}")

        try:
            apply_gnome(still)
            print("Applied live via gsettings (GNOME).")
        except ApplyWallpaperError as e:
            print(f"Could not apply via GNOME: {e}")

        print_hyprland_equivalent(still)

    except ApplyWallpaperError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
