"""
Bistro state.toml parser + validator.

state.toml holds the DYNAMIC side of a server: which users exist, what
role/badges they currently have, and stat counters (like connection hours)
used to auto-unlock local badges. Like server.toml, this is untrusted
remote input, so every field gets bounds-checked the same way.

Expected schema:

    [users."@alice"]
    role = "Owner"
    connection_hours = 142.5
    badges = ["founder", "regular"]
    joined = "2026-01-01"          # optional, ISO date string

    [users."@bob"]
    role = "Helper"
    connection_hours = 8.0
    badges = []

Usernames are TOML table keys and can be arbitrary strings, so this module
validates the username SHAPE (safe charset, length) since usernames often
get displayed directly in a terminal (myserver profile @user) and may be
used to namespace cached profile data on disk.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import tomllib
import re

USERNAME_RE = re.compile(r"^@[a-zA-Z0-9_-]{1,32}$")
BADGE_NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")

MAX_USERS = 10_000
MAX_BADGES_PER_USER = 100
MAX_CONNECTION_HOURS = 10 * 365 * 24
MAX_STATE_TOML_SIZE = 5 * 1024 * 1024


class StateConfigError(Exception):
    """Raised on any structural or bounds problem in state.toml. No
    partial-trust mode — reject the whole file on any single issue."""
    pass


@dataclass
class UserState:
    username: str
    role: str
    connection_hours: float
    badges: list[str] = field(default_factory=list)
    joined: str | None = None


@dataclass
class StateConfig:
    users: dict[str, UserState] = field(default_factory=dict)


def _validate_username(raw: str) -> str:
    if not isinstance(raw, str) or not USERNAME_RE.match(raw):
        raise StateConfigError(
            f"Invalid username {raw!r}: must match @[a-zA-Z0-9_-]{{1,32}}"
        )
    return raw


def _validate_badge_name(raw, username: str) -> str:
    if not isinstance(raw, str) or not BADGE_NAME_RE.match(raw):
        raise StateConfigError(
            f"Invalid badge name {raw!r} for user {username!r}: "
            f"must be lowercase alphanumeric/underscore, 1-32 chars"
        )
    return raw


def _validate_user(username: str, raw: dict) -> UserState:
    if not isinstance(raw, dict):
        raise StateConfigError(f"users.{username!r} must be a table")

    role = raw.get("role")
    if not isinstance(role, str) or len(role) == 0 or len(role) > 32:
        raise StateConfigError(f"users.{username!r}.role must be a non-empty string <= 32 chars")

    connection_hours = raw.get("connection_hours", 0)
    if not isinstance(connection_hours, (int, float)) or isinstance(connection_hours, bool):
        raise StateConfigError(f"users.{username!r}.connection_hours must be numeric")
    if connection_hours < 0 or connection_hours > MAX_CONNECTION_HOURS:
        raise StateConfigError(
            f"users.{username!r}.connection_hours out of sane range: {connection_hours}"
        )

    raw_badges = raw.get("badges", [])
    if not isinstance(raw_badges, list):
        raise StateConfigError(f"users.{username!r}.badges must be a list")
    if len(raw_badges) > MAX_BADGES_PER_USER:
        raise StateConfigError(
            f"users.{username!r} has {len(raw_badges)} badges, exceeds max {MAX_BADGES_PER_USER}"
        )
    badges = [_validate_badge_name(b, username) for b in raw_badges]

    joined = raw.get("joined")
    if joined is not None:
        if not isinstance(joined, str) or len(joined) > 32:
            raise StateConfigError(f"users.{username!r}.joined must be a short string if present")

    return UserState(
        username=username,
        role=role,
        connection_hours=float(connection_hours),
        badges=badges,
        joined=joined,
    )


def parse_state_config(raw_bytes: bytes, known_roles: set[str] | None = None) -> StateConfig:
    """
    Parse and validate state.toml's raw bytes.

    known_roles: if provided (e.g. from an already-parsed ServerConfig),
    every user's role is cross-checked against this set. A server claiming
    a user has a role that doesn't exist in server.toml is a sign of a
    buggy or malicious config pair -- reject it rather than silently
    accepting an undefined role.
    """
    if len(raw_bytes) > MAX_STATE_TOML_SIZE:
        raise StateConfigError(f"state.toml exceeds {MAX_STATE_TOML_SIZE} byte cap")

    try:
        raw = tomllib.loads(raw_bytes.decode("utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise StateConfigError(f"Malformed TOML: {e}")
    except UnicodeDecodeError as e:
        raise StateConfigError(f"state.toml is not valid UTF-8: {e}")

    raw_users = raw.get("users", {})
    if not isinstance(raw_users, dict):
        raise StateConfigError("[users] must be a table")
    if len(raw_users) > MAX_USERS:
        raise StateConfigError(f"Too many users: {len(raw_users)} > {MAX_USERS}")

    users: dict[str, UserState] = {}
    for username, raw_user in raw_users.items():
        username = _validate_username(username)
        user_state = _validate_user(username, raw_user)
        if known_roles is not None and user_state.role not in known_roles:
            raise StateConfigError(
                f"users.{username!r} has role {user_state.role!r} which is not "
                f"defined in this server's server.toml"
            )
        users[username] = user_state

    return StateConfig(users=users)


def load_state_config(path, known_roles=None) -> StateConfig:
    """Convenience wrapper: read a state.toml file from disk and parse it."""
    data = Path(path).read_bytes()
    return parse_state_config(data, known_roles=known_roles)


# --- Example usage / smoke test -----------------------------------------

if __name__ == "__main__":
    good_toml = b"""
[users."@alice"]
role = "Owner"
connection_hours = 142.5
badges = ["founder", "regular"]
joined = "2026-01-01"

[users."@bob"]
role = "Helper"
connection_hours = 8.0
badges = []
"""
    cfg = parse_state_config(good_toml, known_roles={"Owner", "Helper"})
    print(f"Parsed OK: {len(cfg.users)} users")
    for u in cfg.users.values():
        print(f"  {u.username}: role={u.role}, hours={u.connection_hours}, badges={u.badges}")

    undefined_role_toml = b"""
[users."@mallory"]
role = "SuperAdmin"
connection_hours = 0
"""
    try:
        parse_state_config(undefined_role_toml, known_roles={"Owner", "Helper"})
        print("FAIL: should have rejected undefined role")
    except StateConfigError as e:
        print(f"Correctly rejected: {e}")

    absurd_hours_toml = b"""
[users."@abuser"]
role = "Helper"
connection_hours = 999999999999
"""
    try:
        parse_state_config(absurd_hours_toml, known_roles={"Owner", "Helper"})
        print("FAIL: should have rejected absurd connection_hours")
    except StateConfigError as e:
        print(f"Correctly rejected: {e}")

    bad_username_toml = b"""
[users."../../etc/passwd"]
role = "Helper"
connection_hours = 0
"""
    try:
        parse_state_config(bad_username_toml, known_roles={"Owner", "Helper"})
        print("FAIL: should have rejected malformed username")
    except StateConfigError as e:
        print(f"Correctly rejected: {e}")
