#!/usr/bin/env python3
"""
bistro_apply_ascii — takes ascii art already sitting in ~/.cache/bistro/
(fetched + escape-sequence-stripped by bistro_ingest_asset.py) and
prepares it for display.

Unlike theme/kitty/wallpaper/font, there's no system state to change
here — "applying" ascii art just means showing it. It's wired into
bistro_connect.py to print as a banner on connect, once a server's
ascii art has been ingested at least once.

Defense in depth: the text was already run through sanitize_ascii_art()
during ingest (strips ANSI escape sequences so a malicious server can't
smuggle terminal-control sequences into what's supposed to be plain
art). This module re-sanitizes at display time too, rather than
trusting that the cached file wasn't hand-edited or tampered with
since ingest.

Usage:
    bistro_apply_ascii.py <path-to-cached-ascii.txt>
    bistro_apply_ascii.py --latest
"""

from __future__ import annotations
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent / "security"))
from bistro_asset_security import sanitize_ascii_art  # noqa: E402

CACHE_ROOT = Path.home() / ".cache" / "bistro"

MAX_DISPLAY_LINES = 40  # a server pushing a 10,000-line "banner" shouldn't
                         # be able to flood a connect command's output


class ApplyAsciiError(Exception):
    pass


def find_ascii_for_server(server_id: str) -> Path | None:
    """Look up cached ascii art for ONE specific server (by its
    server_id, same hash bistro_connect.py derives from the URL) —
    deliberately not a global 'latest across all servers' lookup like
    the other apply_* scripts use, since a banner should reflect the
    server you just connected to, not whichever server you last
    ingested from."""
    candidates = list(CACHE_ROOT.glob(f"{server_id}/ascii/*"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def find_latest_ascii() -> Path:
    """Global latest, for standalone CLI use (bistro_apply_ascii.py
    --latest) where there's no server_id in scope."""
    candidates = list(CACHE_ROOT.glob("*/ascii/*"))
    if not candidates:
        raise ApplyAsciiError(
            f"No cached ascii art found under {CACHE_ROOT}. "
            f"Run bistro_connect.py against a server with --ingest first."
        )
    return max(candidates, key=lambda p: p.stat().st_mtime)


def load_ascii(path: Path) -> str:
    if not path.exists():
        raise ApplyAsciiError(f"Ascii file not found: {path}")

    text = path.read_text(encoding="utf-8", errors="replace")
    clean = sanitize_ascii_art(text)

    lines = clean.splitlines()
    if len(lines) > MAX_DISPLAY_LINES:
        lines = lines[:MAX_DISPLAY_LINES] + [
            f"... ({len(clean.splitlines()) - MAX_DISPLAY_LINES} more line(s) truncated)"
        ]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1

    try:
        if argv[1] == "--latest":
            ascii_path = find_latest_ascii()
        else:
            ascii_path = Path(argv[1])

        print(load_ascii(ascii_path))

    except ApplyAsciiError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
