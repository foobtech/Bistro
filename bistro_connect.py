#!/usr/bin/env python3
"""
bistro_connect — the transport layer. Fetches a real server's config and
resources over HTTP(S) and runs everything through the SAME validation
pipeline already used for local files. Nothing about trust changes just
because the bytes came over a network instead of off disk.

Expected server layout (matches the local test_server/ layout, just
served over HTTP instead of read from disk):

    https://example.com/server.toml
    https://example.com/state.toml
    https://example.com/resources/<category>/<path>   (as listed in
                                                         server.toml)

Usage:
    bistro_connect.py <base_url>                 fetch + show dashboard
    bistro_connect.py <base_url> --ingest         also fetch + ingest
                                                   every resource listed
                                                   in server.toml through
                                                   the sandboxed pipeline

Security notes:
    - HTTPS certificate verification is NEVER disabled here, even for
      convenience. A server that can't present a valid cert is not
      trusted, full stop.
    - Every fetch has a hard byte-size cap BEFORE the body is fully
      read, so a malicious server can't OOM the client with an
      infinite/huge response.
    - Every fetch has a timeout, so a hanging connection can't stall
      the whole ingest process indefinitely.
    - server_id (used to namespace the local cache) is derived from the
      URL itself via a hash, never taken from anything the server sends.
"""

from __future__ import annotations
from pathlib import Path
from urllib.parse import urljoin, urlparse
import urllib.request
import urllib.error
import hashlib
import ssl
import sys
import time

sys.path.insert(0, str(Path(__file__).parent / "security"))
from bistro_server_config import parse_server_config, ServerConfigError  # noqa: E402
from bistro_state_config import parse_state_config, StateConfigError  # noqa: E402
from bistro_ingest_asset import ingest_asset  # noqa: E402
from bistro_asset_security import AssetSecurityError  # noqa: E402

# bistro_apply_theme.py lives at repo root alongside this file, not in
# security/, so it's imported without the path insert above. Import is
# optional: connecting to a server and caching assets should still work
# even on a machine with no kitty installed, so a missing/broken
# apply-theme module or kitty binary must never break --ingest itself.
try:
    from bistro_apply_theme import load_theme, load_kitty_config, apply_live_kitty, write_persistent_config, ApplyThemeError
    _THEME_APPLY_AVAILABLE = True
except ImportError:
    _THEME_APPLY_AVAILABLE = False

# Same optional-import reasoning as bistro_apply_theme above: a missing
# fontTools/fc-cache on this machine must never make --ingest itself
# look like it failed. Fonts are already safely cached at that point
# regardless of whether this apply step can run.
try:
    from bistro_apply_font import install_font, refresh_font_cache, read_family_name, ApplyFontError
    _FONT_APPLY_AVAILABLE = True
except ImportError:
    _FONT_APPLY_AVAILABLE = False

# Same optional-import reasoning again: ascii art is purely cosmetic
# display, never load-bearing for --ingest succeeding.
try:
    from bistro_apply_ascii import find_ascii_for_server, load_ascii, ApplyAsciiError
    _ASCII_APPLY_AVAILABLE = True
except ImportError:
    _ASCII_APPLY_AVAILABLE = False

MAX_CONFIG_SIZE = 5 * 1024 * 1024       # matches state.toml's own cap
MAX_RESOURCE_SIZE = 100 * 1024 * 1024   # matches wallpaper's own cap, the largest category
FETCH_TIMEOUT_SECONDS = 15

# server.toml (bistro_server_config.py) uses plural resource category names
# ("themes", "fonts", "ascii", "wallpapers") since they hold LISTS of paths.
# The security/ingest layer (bistro_asset_security.py, bistro_ingest_asset.py)
# uses singular category names ("theme", "font", "ascii", "wallpaper") since
# each call there processes ONE asset at a time. This maps between the two
# conventions rather than forcing either module to change its own naming.
_CATEGORY_PLURAL_TO_SINGULAR = {
    "themes": "theme",
    "fonts": "font",
    "ascii": "ascii",
    "wallpapers": "wallpaper",
}


class ConnectError(Exception):
    """Network/protocol-level failure — distinct from ServerConfigError/
    AssetSecurityError, which mean the fetched bytes themselves are bad."""
    pass


def _server_id_for(base_url: str) -> str:
    """Derive a filesystem-safe, stable ID from the server's own URL.
    Deliberately never derived from anything the server sends back."""
    return hashlib.sha256(base_url.encode()).hexdigest()[:16]


def _fetch(url: str, max_size: int, retries: int = 3) -> bytes:
    """
    Fetch a URL with: enforced HTTPS cert verification, a timeout, a
    hard size cap enforced DURING the read, and automatic retry with
    backoff on 404s specifically. The retry exists because static-host
    CDNs (GitHub Pages' Fastly edge, in particular) can briefly disagree
    between edge nodes on whether a just-deployed file exists yet — a
    fetch moments after a successful curl from the same machine can
    still 404. This is a transient-availability problem, not a trust
    problem, so retrying the same already-validated URL is safe here.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise ConnectError(f"Unsupported URL scheme: {parsed.scheme!r}")

    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "Bistro/0.1"})

    last_error = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS, context=ctx) as resp:
                data = resp.read(max_size + 1)
            if len(data) > max_size:
                raise ConnectError(f"Response from {url} exceeds {max_size} byte cap, aborting")
            return data
        except urllib.error.HTTPError as e:
            last_error = ConnectError(f"HTTP {e.code} fetching {url}")
            if e.code != 404 or attempt == retries - 1:
                raise last_error
            time.sleep(0.5 * (attempt + 1))
        except urllib.error.URLError as e:
            raise ConnectError(f"Could not reach {url}: {e.reason}")
        except (TimeoutError, ConnectionError) as e:
            raise ConnectError(f"Connection problem fetching {url}: {e}")

    raise last_error


def fetch_configs(base_url: str):
    """Fetch and fully validate server.toml + state.toml from a server.
    Returns (ServerConfig, StateConfig). Raises ConnectError/
    ServerConfigError/StateConfigError — never returns a partially
    trusted result."""
    server_bytes = _fetch(urljoin(base_url + "/", "server.toml"), MAX_CONFIG_SIZE)
    server = parse_server_config(server_bytes)

    state_bytes = _fetch(urljoin(base_url + "/", "state.toml"), MAX_CONFIG_SIZE)
    known_roles = {r.name for r in server.roles}
    state = parse_state_config(state_bytes, known_roles=known_roles)

    return server, state


def ingest_all_resources(base_url: str, server) -> list[tuple[str, str, bool, str]]:
    """
    Fetch every resource listed in server.resources and run each one
    through the full ingest_asset() pipeline (validate -> sandbox ->
    cache). Never aborts on a single bad asset — one malicious/broken
    file shouldn't block every other legitimate asset on the server.

    Returns a list of (category, path, success, message) so the caller
    can report exactly what happened per-asset.
    """
    server_id = _server_id_for(base_url)
    results = []

    for category, paths in server.resources.items():
        singular_category = _CATEGORY_PLURAL_TO_SINGULAR.get(category, category)
        for rel_path in paths:
            url = urljoin(base_url + "/", f"resources/{category}/{rel_path}")
            try:
                data = _fetch(url, MAX_RESOURCE_SIZE)
                cached_path = ingest_asset(server_id, singular_category, rel_path, data)
                results.append((category, rel_path, True, str(cached_path)))
            except (ConnectError, AssetSecurityError) as e:
                results.append((category, rel_path, False, str(e)))

    return results


def connect_and_apply(base_url: str, do_ingest: bool = True) -> bool:
    """
    The full connect -> (optionally ingest) -> auto-apply flow, as one
    reusable function instead of being locked inside main(). Extracted
    specifically so bistro_daemon.py can call this per-subscribed-server
    on a timer, without duplicating any of this logic.

    Returns True if the connect itself succeeded (regardless of whether
    individual assets applied cleanly — those failures are reported but
    non-fatal, same as they always were). Returns False only on a
    connect-level failure (unreachable server, malformed configs).
    """
    try:
        server, state = fetch_configs(base_url)
    except (ConnectError, ServerConfigError, StateConfigError) as e:
        print(f"Could not connect: {e}", file=sys.stderr)
        return False

    print(f"Connected to {server.name} ({base_url})")
    if server.description:
        print(f"  {server.description}")
    print(f"  {len(state.users)} member(s), {len(server.roles)} role(s)")

    if do_ingest:
        print(f"\nFetching + ingesting resources...")
        results = ingest_all_resources(base_url, server)
        ok_count = sum(1 for r in results if r[2])
        print(f"\n{ok_count}/{len(results)} resources ingested successfully")
        for category, rel_path, success, message in results:
            status = "OK" if success else "REJECTED"
            print(f"  [{status}] {category}/{rel_path}: {message}")

        # Auto-apply the last successfully ingested theme (+ kitty.toml if
        # the server pushed one too), if any. This is what makes the
        # concept doc's "server pushes a theme, terminal hot-reloads" idea
        # actually true end-to-end, instead of needing a second manual
        # command. Never lets a theme-apply failure make the overall
        # connect+ingest look like it failed — the assets are already
        # safely cached at this point regardless.
        theme_results = [r for r in results if r[0] == "themes" and r[2]]
        kitty_results = [r for r in results if r[0] == "kitty" and r[2]]
        if theme_results and _THEME_APPLY_AVAILABLE:
            category, rel_path, success, cached_path = theme_results[-1]
            try:
                theme = load_theme(Path(cached_path))
                kitty_config = None
                if kitty_results:
                    _, _, _, kitty_cached_path = kitty_results[-1]
                    kitty_config = load_kitty_config(Path(kitty_cached_path))
                applied_count = apply_live_kitty(theme)
                write_persistent_config(theme, kitty_config)
                window_word = "window" if applied_count == 1 else "windows"
                print(f"\nApplied theme '{Path(cached_path).stem}' to {applied_count} kitty {window_word} (live + persisted).")
            except ApplyThemeError as e:
                print(f"\nAssets cached, but couldn't auto-apply theme: {e}")
        elif theme_results and not _THEME_APPLY_AVAILABLE:
            print(f"\nTheme cached, but bistro_apply_theme.py wasn't found to auto-apply it.")

        # Auto-install any pushed fonts. Without this, a kitty.toml's
        # font_family is just a string that gets written into
        # bistro-theme.conf with no guarantee kitty can actually find
        # that font — this is what makes it real.
        font_results = [r for r in results if r[0] == "fonts" and r[2]]
        if font_results and _FONT_APPLY_AVAILABLE:
            for category, rel_path, success, cached_path in font_results:
                try:
                    dest = install_font(Path(cached_path))
                    refresh_font_cache()
                    family_name = read_family_name(dest)
                    label = f" ({family_name!r})" if family_name else ""
                    print(f"\nInstalled font '{rel_path}'{label} to {dest.parent}/")
                except ApplyFontError as e:
                    print(f"\nFont cached, but couldn't auto-install it: {e}")
        elif font_results and not _FONT_APPLY_AVAILABLE:
            print(f"\nFont(s) cached, but bistro_apply_font.py wasn't found to auto-install them.")

    # Ascii banner: checked on EVERY connect, not just --ingest runs, so
    # a server's banner shows up on subsequent plain connects too, using
    # whatever's already cached from a previous --ingest. Deliberately
    # scoped to THIS server_id, not a global "latest ingested anywhere"
    # lookup — connecting to server A shouldn't show server B's banner.
    if _ASCII_APPLY_AVAILABLE:
        server_id = _server_id_for(base_url)
        ascii_path = find_ascii_for_server(server_id)
        if ascii_path:
            try:
                print(f"\n{load_ascii(ascii_path)}\n")
            except ApplyAsciiError:
                pass  # a bad cached banner should never block a connect

    return True


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1

    base_url = argv[1].rstrip("/")
    do_ingest = "--ingest" in argv[2:]

    ok = connect_and_apply(base_url, do_ingest=do_ingest)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
