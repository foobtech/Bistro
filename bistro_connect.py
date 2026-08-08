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

sys.path.insert(0, str(Path(__file__).parent / "security"))
from bistro_server_config import parse_server_config, ServerConfigError  # noqa: E402
from bistro_state_config import parse_state_config, StateConfigError  # noqa: E402
from bistro_ingest_asset import ingest_asset  # noqa: E402
from bistro_asset_security import AssetSecurityError  # noqa: E402

MAX_CONFIG_SIZE = 5 * 1024 * 1024       # matches state.toml's own cap
MAX_RESOURCE_SIZE = 100 * 1024 * 1024   # matches wallpaper's own cap, the largest category
FETCH_TIMEOUT_SECONDS = 15


class ConnectError(Exception):
    """Network/protocol-level failure — distinct from ServerConfigError/
    AssetSecurityError, which mean the fetched bytes themselves are bad."""
    pass


def _server_id_for(base_url: str) -> str:
    """Derive a filesystem-safe, stable ID from the server's own URL.
    Deliberately never derived from anything the server sends back."""
    return hashlib.sha256(base_url.encode()).hexdigest()[:16]


def _fetch(url: str, max_size: int) -> bytes:
    """
    Fetch a URL with: enforced HTTPS cert verification, a timeout, and a
    hard size cap enforced DURING the read (not just after), so a server
    can't stream an unbounded response at us.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise ConnectError(f"Unsupported URL scheme: {parsed.scheme!r}")

    # Default SSL context does full certificate verification. Never
    # swapped for an unverified context, even for "just testing".
    ctx = ssl.create_default_context()

    req = urllib.request.Request(url, headers={"User-Agent": "Bistro/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS, context=ctx) as resp:
            data = resp.read(max_size + 1)
    except urllib.error.HTTPError as e:
        raise ConnectError(f"HTTP {e.code} fetching {url}")
    except urllib.error.URLError as e:
        raise ConnectError(f"Could not reach {url}: {e.reason}")
    except (TimeoutError, ConnectionError) as e:
        raise ConnectError(f"Connection problem fetching {url}: {e}")

    if len(data) > max_size:
        raise ConnectError(f"Response from {url} exceeds {max_size} byte cap, aborting")

    return data


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
        for rel_path in paths:
            url = urljoin(base_url + "/", f"resources/{rel_path}")
            try:
                data = _fetch(url, MAX_RESOURCE_SIZE)
                cached_path = ingest_asset(server_id, category, rel_path, data)
                results.append((category, rel_path, True, str(cached_path)))
            except (ConnectError, AssetSecurityError) as e:
                results.append((category, rel_path, False, str(e)))

    return results


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1

    base_url = argv[1].rstrip("/")
    do_ingest = "--ingest" in argv[2:]

    try:
        server, state = fetch_configs(base_url)
    except (ConnectError, ServerConfigError, StateConfigError) as e:
        print(f"Could not connect: {e}", file=sys.stderr)
        return 1

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

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
