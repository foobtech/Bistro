#!/usr/bin/env python3
"""
bistro_subscribe — manage the list of servers bistro_daemon.py watches.

    bistro_subscribe.py add <url>       subscribe to a server
    bistro_subscribe.py remove <url>    unsubscribe
    bistro_subscribe.py list            show current subscriptions

Subscriptions are stored at ~/.config/bistro/subscriptions.toml — a
flat list of server URLs, nothing else. Deliberately minimal: no
per-server settings live here (yet). This file is LOCAL config the
user themselves wrote/edited by running these commands — it's not
untrusted remote input the way server.toml/state.toml are, so it
doesn't go through the same validation pipeline. It still gets
size-capped and URL-shape-checked, just as a sanity net against a
corrupted file, not as a security boundary.
"""

from __future__ import annotations
from pathlib import Path
from urllib.parse import urlparse
import sys
import tomllib

CONFIG_DIR = Path.home() / ".config" / "bistro"
SUBSCRIPTIONS_PATH = CONFIG_DIR / "subscriptions.toml"
MAX_SUBSCRIPTIONS = 200  # sanity cap, not a security boundary — this is local config


class SubscriptionsError(Exception):
    pass


def _validate_url(url: str) -> str:
    url = url.rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise SubscriptionsError(f"URL must start with http:// or https://, got: {url!r}")
    if not parsed.netloc:
        raise SubscriptionsError(f"Not a valid URL: {url!r}")
    return url


def load_subscriptions() -> list[str]:
    if not SUBSCRIPTIONS_PATH.exists():
        return []

    try:
        raw = tomllib.loads(SUBSCRIPTIONS_PATH.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise SubscriptionsError(f"Malformed subscriptions.toml: {e}")

    servers = raw.get("servers", [])
    if not isinstance(servers, list) or not all(isinstance(s, str) for s in servers):
        raise SubscriptionsError("subscriptions.toml's 'servers' must be a list of strings")

    return servers


def save_subscriptions(urls: list[str]) -> None:
    if len(urls) > MAX_SUBSCRIPTIONS:
        raise SubscriptionsError(f"Too many subscriptions: {len(urls)} > {MAX_SUBSCRIPTIONS}")

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["servers = ["]
    for url in urls:
        escaped = url.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'    "{escaped}",')
    lines.append("]")
    text = "\n".join(lines) + "\n"

    tmp = SUBSCRIPTIONS_PATH.with_suffix(".toml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(SUBSCRIPTIONS_PATH)


def add_subscription(url: str) -> bool:
    """Returns False (no-op) if already subscribed, True if newly added."""
    url = _validate_url(url)
    urls = load_subscriptions()
    if url in urls:
        return False
    urls.append(url)
    save_subscriptions(urls)
    return True


def remove_subscription(url: str) -> bool:
    """Returns False if it wasn't in the list, True if removed."""
    url = url.rstrip("/")
    urls = load_subscriptions()
    if url not in urls:
        return False
    urls.remove(url)
    save_subscriptions(urls)
    return True


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1

    try:
        if argv[1] == "add" and len(argv) == 3:
            added = add_subscription(argv[2])
            print(f"Subscribed to {argv[2]}" if added else f"Already subscribed to {argv[2]}")
        elif argv[1] == "remove" and len(argv) == 3:
            removed = remove_subscription(argv[2])
            print(f"Unsubscribed from {argv[2]}" if removed else f"Wasn't subscribed to {argv[2]}")
        elif argv[1] == "list":
            urls = load_subscriptions()
            if not urls:
                print("No subscriptions yet. Add one with: bistro_subscribe.py add <url>")
            for url in urls:
                print(f"  {url}")
        else:
            print(__doc__)
            return 1
    except SubscriptionsError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
