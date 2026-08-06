"""
Bistro asset security layer — path validation + basic file checks
for the server -> client asset streaming pipeline.

This is the FIRST line of defense: run every incoming resource path
and file through this before it ever touches ~/.cache/bistro/.

Priority order (per design discussion):
  1. Path validation (this file)              <- prevents traversal writes
  2. Format/size allowlist (this file)         <- rejects obviously wrong files
  3. Sandboxed parsing (bubblewrap, separate)  <- isolates the parser itself
  4. Hash/pattern blocklist (separate, community-updated repo)
"""

from pathlib import Path
import hashlib
import mimetypes

# --- Config -----------------------------------------------------------

CACHE_ROOT = Path.home() / ".cache" / "bistro"

# Max sizes per asset type, in bytes. Adjust as needed.
MAX_SIZES = {
    "font": 5 * 1024 * 1024,        # 5 MB — generous for a .ttf/.woff2
    "wallpaper": 100 * 1024 * 1024, # 100 MB — animated loops can be chunky
    "ascii": 256 * 1024,            # 256 KB — plenty for text art
    "theme": 64 * 1024,             # 64 KB — it's just hex codes
}

ALLOWED_EXTENSIONS = {
    "font": {".ttf", ".woff2"},
    "wallpaper": {".mp4", ".webm", ".png", ".gif"},
    "ascii": {".txt", ".ans"},
    "theme": {".toml"},
}

# Magic bytes for cheap format sniffing (extension can lie, bytes mostly don't)
MAGIC_BYTES = {
    ".ttf": [b"\x00\x01\x00\x00", b"true", b"OTTO"],
    ".woff2": [b"wOF2"],
    ".mp4": [b"ftyp"],  # appears a few bytes in, checked specially below
    ".webm": [b"\x1a\x45\xdf\xa3"],
    ".png": [b"\x89PNG\r\n\x1a\n"],
    ".gif": [b"GIF87a", b"GIF89a"],
}


class AssetSecurityError(Exception):
    """Raised when an incoming asset fails validation. Never suppress this
    silently — the caller must abort the write and should log which server
    sent the bad asset."""
    pass


def validate_resource_path(server_id: str, category: str, relative_path: str) -> Path:
    """
    Resolve a server-supplied relative path against the cache dir and verify
    it can't escape it. This is the single most important function in this
    file — every other check is secondary to this one.

    server_id: a filesystem-safe identifier for the server (e.g. a hash of
               its connection string), used to namespace caches per-server.
    category:  one of "font", "wallpaper", "ascii", "theme"
    relative_path: the path string as supplied by server.toml / the server

    Returns the validated, absolute Path if safe. Raises AssetSecurityError
    otherwise.
    """
    if category not in ALLOWED_EXTENSIONS:
        raise AssetSecurityError(f"Unknown asset category: {category}")

    expected_dir = (CACHE_ROOT / server_id / category).resolve()
    expected_dir.mkdir(parents=True, exist_ok=True)

    # Reject absolute paths and any path containing traversal segments
    # outright, before even attempting resolution. Belt and suspenders.
    if Path(relative_path).is_absolute():
        raise AssetSecurityError(f"Absolute paths not allowed: {relative_path!r}")
    if ".." in Path(relative_path).parts:
        raise AssetSecurityError(f"Path traversal attempt: {relative_path!r}")

    candidate = (expected_dir / relative_path).resolve()

    # The real check: after resolving symlinks and '..', does the final
    # path still live inside expected_dir? This catches tricks that the
    # string-based check above might miss (symlinks, encoded separators, etc).
    try:
        candidate.relative_to(expected_dir)
    except ValueError:
        raise AssetSecurityError(
            f"Resolved path escapes cache directory: {candidate} not under {expected_dir}"
        )

    ext = candidate.suffix.lower()
    if ext not in ALLOWED_EXTENSIONS[category]:
        raise AssetSecurityError(
            f"Extension {ext!r} not allowed for category {category!r}"
        )

    return candidate


def validate_asset_bytes(category: str, ext: str, data: bytes) -> None:
    """
    Cheap sanity checks on the actual bytes before writing to disk:
    size cap + magic-byte sniff. This does NOT guarantee the file is safe
    to parse — that's what the sandboxed parsing step is for — it just
    rejects obviously wrong/oversized/spoofed files early and cheaply.
    """
    max_size = MAX_SIZES.get(category)
    if max_size and len(data) > max_size:
        raise AssetSecurityError(
            f"Asset exceeds size cap for {category}: {len(data)} > {max_size} bytes"
        )

    magic_list = MAGIC_BYTES.get(ext)
    if magic_list:
        # mp4's ftyp box isn't at offset 0, so check the first 16 bytes
        window = data[:16]
        if not any(magic in window for magic in magic_list):
            raise AssetSecurityError(
                f"File does not match expected magic bytes for {ext} "
                f"(possible spoofed/renamed file)"
            )


def sha256_of(data: bytes) -> str:
    """Used for checking against the community hash blocklist (separate repo)."""
    return hashlib.sha256(data).hexdigest()


def sanitize_ascii_art(text: str) -> str:
    """
    Strip dangerous ANSI/OSC escape sequences from server-supplied ASCII art
    before it ever reaches a terminal. Allows basic SGR color codes (safe,
    just changes text color) but strips cursor-movement, OSC, and other
    sequences that could be used for terminal injection tricks.
    """
    import re

    # Allow: ESC [ ... m  (SGR - color/style only)
    # Strip: everything else starting with ESC
    out = []
    i = 0
    while i < len(text):
        if text[i] == "\x1b":
            match = re.match(r"\x1b\[([0-9;]*)m", text[i:])
            if match:
                out.append(match.group(0))
                i += len(match.group(0))
                continue
            # Any other escape sequence: skip the ESC and let the loop
            # re-sync on the next character rather than trying to parse
            # every possible sequence type.
            i += 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


# --- Example usage ------------------------------------------------------

if __name__ == "__main__":
    # Simulated malicious server.toml trying a traversal attack
    try:
        validate_resource_path(
            server_id="a1b2c3d4",
            category="font",
            relative_path="../../../../.bashrc",
        )
    except AssetSecurityError as e:
        print(f"Blocked as expected: {e}")

    # Simulated legit request
    safe_path = validate_resource_path(
        server_id="a1b2c3d4",
        category="theme",
        relative_path="espresso_roast.toml",
    )
    print(f"Validated safe path: {safe_path}")
