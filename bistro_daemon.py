#!/usr/bin/env python3
"""
bistro_daemon — runs bistro_connect.py's connect+ingest+apply loop
against every subscribed server on a timer, so themes/wallpapers/fonts/
ascii actually update in the background instead of needing a manual
`bistro_connect.py <url> --ingest` every time a server pushes something
new.

Usage:
    bistro_daemon.py                    run with default 15 min interval
    bistro_daemon.py --interval 300     run with a custom interval (seconds)
    bistro_daemon.py --once             do one pass over all subscriptions
                                         and exit (useful for testing, or
                                         for driving this from cron/a
                                         systemd timer instead of the
                                         built-in sleep loop)

Meant to run as a systemd --user service (see bistro-daemon.service in
this repo) so it starts on login and restarts if it crashes — but it's
plain Python with no systemd-specific code, so it also just runs fine
directly in a terminal for testing.

One subscribed server misbehaving (unreachable, bad TLS, malformed
config) never stops the daemon from checking the others — errors are
caught and logged per-server, same "don't let one bad thing take down
everything else" principle as ingest_all_resources().
"""

from __future__ import annotations
from pathlib import Path
import argparse
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).parent))
from bistro_connect import connect_and_apply  # noqa: E402
from bistro_subscribe import load_subscriptions, SubscriptionsError  # noqa: E402

DEFAULT_INTERVAL_SECONDS = 15 * 60


def _timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def run_once() -> None:
    """One full pass over every subscribed server. Never raises —
    a single server's failure is logged and skipped, not fatal to the
    daemon process itself."""
    try:
        urls = load_subscriptions()
    except SubscriptionsError as e:
        print(f"[{_timestamp()}] Could not load subscriptions: {e}", file=sys.stderr)
        return

    if not urls:
        print(f"[{_timestamp()}] No subscriptions yet — nothing to check. "
              f"Add one with: bistro_subscribe.py add <url>")
        return

    print(f"[{_timestamp()}] Checking {len(urls)} subscribed server(s)...")
    for url in urls:
        print(f"\n[{_timestamp()}] --- {url} ---")
        try:
            connect_and_apply(url, do_ingest=True)
        except Exception as e:
            # Deliberately broad: this loop must survive ANY single
            # server's failure, including bugs we didn't anticipate,
            # not just the ConnectError/ServerConfigError cases
            # connect_and_apply already handles internally.
            print(f"[{_timestamp()}] Unexpected error checking {url}: {e}", file=sys.stderr)
            traceback.print_exc()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Periodically check subscribed Bistro servers and apply updates."
    )
    parser.add_argument(
        "--interval", type=int, default=DEFAULT_INTERVAL_SECONDS,
        help=f"Seconds between checks (default: {DEFAULT_INTERVAL_SECONDS})",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Do one pass over all subscriptions and exit, instead of looping forever.",
    )
    args = parser.parse_args(argv[1:])

    if args.once:
        run_once()
        return 0

    print(f"[{_timestamp()}] bistro_daemon starting, checking every {args.interval}s")
    while True:
        run_once()
        print(f"\n[{_timestamp()}] Sleeping {args.interval}s until next check...")
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
