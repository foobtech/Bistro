#!/usr/bin/env bash
#
# Bistro asset sandboxing — layer 3 of the security pipeline.
#
# Takes an asset that already PASSED validate_resource_path() and
# validate_asset_bytes() (see bistro_asset_security.py) and runs the
# actual parsing/re-encoding step inside a locked-down bubblewrap
# sandbox. The sandbox never sees the network, never sees the real
# home directory, and can only write to one fixed output path.
#
# Even if fontconfig/freetype/ffmpeg has an exploitable bug, the worst
# case is a compromised throwaway mount namespace with nothing in it.
#
# Usage:
#   bistro_sandbox_process.sh font <input.ttf|woff2> <output.ttf>
#   bistro_sandbox_process.sh wallpaper <input.mp4|webm|gif> <output.mp4>
#
# Requires: bwrap, python3 + fontTools (for font path), ffmpeg (for wallpaper path)

set -euo pipefail

MODE="${1:-}"
INPUT="${2:-}"
OUTPUT="${3:-}"

if [[ -z "$MODE" || -z "$INPUT" || -z "$OUTPUT" ]]; then
    echo "Usage: $0 <font|wallpaper> <input> <output>" >&2
    exit 1
fi

if [[ ! -f "$INPUT" ]]; then
    echo "Input file does not exist: $INPUT" >&2
    exit 1
fi

if ! command -v bwrap >/dev/null 2>&1; then
    echo "bwrap not found. Install bubblewrap first (pacman -S bubblewrap)." >&2
    exit 1
fi

INPUT_ABS="$(realpath "$INPUT")"
OUTPUT_ABS_DIR="$(realpath "$(dirname "$OUTPUT")")"
OUTPUT_BASENAME="$(basename "$OUTPUT")"

# Scratch dir inside the sandbox's own tmpfs — never touches the real
# filesystem outside the two explicit binds below.
SANDBOX_IN="/sandbox_in"
SANDBOX_OUT="/sandbox_out"

# --- Common sandbox lockdown flags --------------------------------------
# --unshare-all       : no network, no shared IPC/UTS/pid/etc namespaces
# --die-with-parent    : sandbox dies if this script dies, no orphans
# --new-session        : can't inject input into the parent's terminal
# --ro-bind input       : input file is READ-ONLY inside the sandbox
# --bind output-dir      : only the output dir is writable, nothing else
# --tmpfs /               : root is a fresh empty tmpfs, no host filesystem
# --proc /proc / --dev /dev : minimal, needed for most tools to run at all
BWRAP_COMMON=(
    --unshare-all
    --die-with-parent
    --new-session
    --tmpfs /
    --proc /proc
    --dev /dev
    --tmpfs /tmp
    --ro-bind /usr /usr
    --ro-bind /lib /lib
    --ro-bind /lib64 /lib64
    --symlink /usr/bin /bin
    --ro-bind-try /etc/fonts /etc/fonts
    --ro-bind "$INPUT_ABS" "$SANDBOX_IN/$(basename "$INPUT_ABS")"
    --bind "$OUTPUT_ABS_DIR" "$SANDBOX_OUT"
)

case "$MODE" in
    font)
        # Parse-and-rebuild: forces fontTools to walk every table in the
        # font and re-emit a clean file. Malformed/malicious structure
        # either fails outright (caught, asset rejected) or gets
        # normalized away in the rebuild.
        bwrap "${BWRAP_COMMON[@]}" \
            python3 -c "
from fontTools.ttLib import TTFont
import sys
try:
    font = TTFont('$SANDBOX_IN/$(basename "$INPUT_ABS")', recalcBBoxes=True, recalcTimestamp=False)
    font.save('$SANDBOX_OUT/$OUTPUT_BASENAME')
except Exception as e:
    print(f'REJECTED: font failed to parse cleanly: {e}', file=sys.stderr)
    sys.exit(1)
"
        ;;

    wallpaper)
        # Transcode to a fixed, known-safe format/codec. Raw bytes never
        # reach anything downstream — only ffmpeg's own re-encoded output
        # does. Strips metadata, caps resolution, forces a sane codec.
        bwrap "${BWRAP_COMMON[@]}" \
            --ro-bind-try /usr/lib/ffmpeg /usr/lib/ffmpeg \
            ffmpeg -y -loglevel error \
                -i "$SANDBOX_IN/$(basename "$INPUT_ABS")" \
                -map_metadata -1 \
                -vf "scale='min(1920,iw)':-2" \
                -c:v libx264 -preset fast -crf 23 \
                -an \
                "$SANDBOX_OUT/$OUTPUT_BASENAME"
        ;;

    *)
        echo "Unknown mode: $MODE (expected 'font' or 'wallpaper')" >&2
        exit 1
        ;;
esac

echo "Sandboxed processing complete: $OUTPUT"
