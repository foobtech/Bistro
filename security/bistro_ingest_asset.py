#!/usr/bin/env python3
"""
Bistro asset ingestion pipeline — the glue that chains all three
security layers together into one real pipeline:

    1. validate_resource_path()   <- is this path allowed to exist?
    2. validate_asset_bytes()     <- do the bytes look like what they claim?
    3. bistro_sandbox_process.sh  <- actually parse/re-encode inside bwrap
    4. move to ~/.cache/bistro/   <- ONLY on success, atomic rename

Nothing gets written to the real cache directory until it has survived
all four steps. Any failure anywhere aborts the whole ingest and raises
AssetSecurityError — callers must not silently swallow this.

This is deliberately synchronous and per-asset. Called once per file the
server pushes down; not meant to batch multiple assets in one call.
"""

from pathlib import Path
import subprocess
import tempfile
import shutil
import sys

sys.path.insert(0, str(Path(__file__).parent))
from bistro_asset_security import (
    validate_resource_path,
    validate_asset_bytes,
    sanitize_ascii_art,
    sha256_of,
    AssetSecurityError,
)

SANDBOX_SCRIPT = Path(__file__).parent / "bistro_sandbox_process.sh"

# Categories that need the bwrap sandbox (real parsers involved).
# theme/ascii are just text/TOML, so they skip sandboxing but still
# go through path + byte validation, and ascii gets escape-sequence
# stripped separately.
SANDBOXED_CATEGORIES = {"font", "wallpaper"}


def ingest_asset(server_id: str, category: str, relative_path: str, data: bytes) -> Path:
    """
    Run one incoming asset through the full pipeline and, if it passes
    everything, place it at its final location under ~/.cache/bistro/.

    Returns the final cached Path on success.
    Raises AssetSecurityError on any failure — asset is NEVER cached
    in that case, and any temp files are cleaned up before raising.
    """
    # --- Layer 1: path validation ---------------------------------------
    final_path = validate_resource_path(server_id, category, relative_path)
    ext = final_path.suffix.lower()

    # --- Layer 2: byte-level validation ---------------------------------
    validate_asset_bytes(category, ext, data)

    asset_hash = sha256_of(data)
    # NOTE: this is where a call out to the community hash-blocklist
    # would go once that repo exists. Left as a stub so it's obvious
    # where it plugs in later:
    #   if asset_hash in blocklist: raise AssetSecurityError(...)

    # --- ascii art: no sandbox needed, just strip escape sequences -----
    if category == "ascii":
        text = data.decode("utf-8", errors="replace")
        clean_text = sanitize_ascii_art(text)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = final_path.with_suffix(final_path.suffix + ".tmp")
        tmp.write_text(clean_text, encoding="utf-8")
        tmp.replace(final_path)  # atomic on same filesystem
        return final_path

    # --- theme: just TOML text, no sandbox, but never eval/exec it -----
    if category == "theme":
        final_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = final_path.with_suffix(final_path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(final_path)
        return final_path

    # --- font / wallpaper: Layer 3, sandboxed processing ----------------
    if category not in SANDBOXED_CATEGORIES:
        raise AssetSecurityError(f"No ingestion path defined for category: {category}")

    with tempfile.TemporaryDirectory(prefix="bistro_ingest_") as tmpdir:
        tmp_input = Path(tmpdir) / f"input{ext}"
        tmp_output = Path(tmpdir) / f"output{final_path.suffix}"
        tmp_input.write_bytes(data)

        if not SANDBOX_SCRIPT.exists():
            raise AssetSecurityError(f"Sandbox script missing: {SANDBOX_SCRIPT}")

        try:
            result = subprocess.run(
                [str(SANDBOX_SCRIPT), category, str(tmp_input), str(tmp_output)],
                capture_output=True,
                text=True,
                timeout=60,  # don't let a hostile/broken asset hang the pipeline forever
            )
        except (OSError, subprocess.SubprocessError) as e:
            # Covers: missing +x bit, bwrap not installed, timeout, etc.
            # Anything here means "could not even run the sandbox" — treat
            # it the same as a rejection, never fall through to caching.
            raise AssetSecurityError(
                f"Could not launch sandbox for {category} asset "
                f"(server={server_id}, path={relative_path}): {e}"
            )

        if result.returncode != 0:
            raise AssetSecurityError(
                f"Sandboxed processing failed for {category} asset "
                f"(server={server_id}, path={relative_path}): {result.stderr.strip()}"
            )

        if not tmp_output.exists() or tmp_output.stat().st_size == 0:
            raise AssetSecurityError(
                f"Sandbox reported success but produced no output for {relative_path}"
            )

        # --- Layer 4: move to real cache, only now that everything passed ---
        final_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp_output), str(final_path))

    return final_path


# --- Example usage / smoke test -----------------------------------------

if __name__ == "__main__":
    if len(sys.argv) != 5:
        print(
            "Usage: bistro_ingest_asset.py <server_id> <category> <relative_path> <local_file_to_ingest>",
            file=sys.stderr,
        )
        sys.exit(1)

    server_id, category, relative_path, local_file = sys.argv[1:5]
    data = Path(local_file).read_bytes()

    try:
        cached_path = ingest_asset(server_id, category, relative_path, data)
        print(f"Ingested successfully -> {cached_path}")
    except AssetSecurityError as e:
        print(f"Rejected: {e}", file=sys.stderr)
        sys.exit(1)
