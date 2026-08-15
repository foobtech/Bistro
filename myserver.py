#!/usr/bin/env python3
"""
myserver — Bistro's unified server management command.

    myserver                        show the dashboard
    myserver profile @user          show a user's identity card
    myserver roles                  list roles and permissions
    myserver promote @actor @target <role>
                                     promote target to a role, if actor
                                     is permitted to do so
    myserver award @actor @target <badge>
                                     award target a badge, if actor
                                     is permitted to do so

This operates on a LOCAL pair of server.toml/state.toml files (the same
files a real Bistro server would host) — it's the same code a real
`myserver` daemon would use, just invoked directly for now rather than
over a network connection. All config is parsed through the validated
loaders in bistro_server_config.py / bistro_state_config.py; nothing here
trusts raw TOML directly.

Usage:
    myserver.py <server_dir> [command...]

Where <server_dir> contains server.toml and state.toml (see README
"Concept" section for the expected directory layout).
"""

from __future__ import annotations
from pathlib import Path
import sys
import tomllib

sys.path.insert(0, str(Path(__file__).parent / "security"))
from bistro_server_config import (  # noqa: E402
    load_server_config, ServerConfig, ServerConfigError, Role,
)
from bistro_state_config import (  # noqa: E402
    load_state_config, parse_state_config, StateConfig, UserState, StateConfigError,
    BADGE_NAME_RE, MAX_BADGES_PER_USER,
)


class MyServerError(Exception):
    """User-facing error for CLI misuse or permission problems — distinct
    from ServerConfigError/StateConfigError, which mean the TOML itself
    is malformed or untrusted."""
    pass


# --- Rendering helpers ----------------------------------------------------

def _hex_to_ansi(hex_color: str) -> str:
    """Convert a validated '#rrggbb' string into a truecolor ANSI prefix."""
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    return f"\x1b[38;2;{r};{g};{b}m"


RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"


def _role_lookup(server: ServerConfig) -> dict[str, Role]:
    return {r.name: r for r in server.roles}


# --- Commands ---------------------------------------------------------------

def cmd_dashboard(server: ServerConfig, state: StateConfig) -> None:
    roles = _role_lookup(server)
    staff = [u for u in state.users.values()
             if u.role in roles and (roles[u.role].can_promote or roles[u.role].can_award_badges)]

    print(f"{BOLD}{server.name}{RESET}")
    if server.description:
        print(f"{DIM}{server.description}{RESET}")
    print()
    print(f"  {len(state.users)} member(s)  ·  {len(server.roles)} role(s)  ·  {len(staff)} staff online-eligible")
    print()
    print(f"{BOLD}Staff{RESET}")
    if not staff:
        print(f"  {DIM}(none){RESET}")
    for u in staff:
        role = roles[u.role]
        color = _hex_to_ansi(role.color)
        print(f"  {color}{role.name:<10}{RESET} {u.username}")


def cmd_profile(server: ServerConfig, state: StateConfig, username: str) -> None:
    user = state.users.get(username)
    if user is None:
        raise MyServerError(f"No such user: {username}")

    roles = _role_lookup(server)
    role = roles.get(user.role)
    color = _hex_to_ansi(role.color) if role else ""

    print(f"{BOLD}{user.username}{RESET}")
    print(f"  Role:    {color}{user.role}{RESET}")
    print(f"  Hours:   {user.connection_hours:.1f}")
    if user.joined:
        print(f"  Joined:  {user.joined}")
    print(f"  Badges:  {', '.join(user.badges) if user.badges else DIM + '(none)' + RESET}")


def cmd_roles(server: ServerConfig) -> None:
    print(f"{BOLD}Roles on {server.name}{RESET}")
    for role in server.roles:
        color = _hex_to_ansi(role.color)
        perms = []
        if role.can_promote:
            perms.append("promote")
        if role.can_award_badges:
            perms.append("award badges")
        perm_str = f"  ({', '.join(perms)})" if perms else ""
        print(f"  {color}{role.name}{RESET}{perm_str}")


def cmd_promote(
    server: ServerConfig, state: StateConfig, actor: str, target: str, new_role: str
) -> StateConfig:
    """
    Returns the UPDATED StateConfig — caller is responsible for writing it
    back to disk. This function only decides whether the promotion is
    allowed and produces the new in-memory state; it never touches disk
    itself, so callers can dry-run this if they want.
    """
    roles = _role_lookup(server)

    if new_role not in roles:
        raise MyServerError(f"Role {new_role!r} does not exist on this server")

    actor_state = state.users.get(actor)
    if actor_state is None:
        raise MyServerError(f"No such user: {actor}")
    actor_role = roles.get(actor_state.role)
    if actor_role is None or not actor_role.can_promote:
        raise MyServerError(f"{actor} does not have permission to promote users")

    target_state = state.users.get(target)
    if target_state is None:
        raise MyServerError(f"No such user: {target}")

    updated_target = UserState(
        username=target_state.username,
        role=new_role,
        connection_hours=target_state.connection_hours,
        badges=target_state.badges,
        joined=target_state.joined,
    )
    new_users = dict(state.users)
    new_users[target] = updated_target
    return StateConfig(users=new_users)


def cmd_award(
    server: ServerConfig, state: StateConfig, actor: str, target: str, badge: str
) -> StateConfig:
    """
    Award a badge to target, if actor has can_award_badges permission.
    Same shape as cmd_promote: returns the UPDATED StateConfig, never
    touches disk itself. Awarding a badge the target already has is a
    no-op (returns state unchanged) rather than an error — badges have
    no concept of "duplicate," so there's nothing meaningfully wrong
    with re-awarding one.
    """
    roles = _role_lookup(server)

    if not BADGE_NAME_RE.match(badge):
        raise MyServerError(
            f"Invalid badge name {badge!r}: must be lowercase alphanumeric/"
            f"underscore, 1-32 chars"
        )

    actor_state = state.users.get(actor)
    if actor_state is None:
        raise MyServerError(f"No such user: {actor}")
    actor_role = roles.get(actor_state.role)
    if actor_role is None or not actor_role.can_award_badges:
        raise MyServerError(f"{actor} does not have permission to award badges")

    target_state = state.users.get(target)
    if target_state is None:
        raise MyServerError(f"No such user: {target}")

    if badge in target_state.badges:
        return state

    if len(target_state.badges) >= MAX_BADGES_PER_USER:
        raise MyServerError(
            f"{target} already has the maximum of {MAX_BADGES_PER_USER} badges"
        )

    updated_target = UserState(
        username=target_state.username,
        role=target_state.role,
        connection_hours=target_state.connection_hours,
        badges=target_state.badges + [badge],
        joined=target_state.joined,
    )
    new_users = dict(state.users)
    new_users[target] = updated_target
    return StateConfig(users=new_users)


# --- state.toml serialization (write-back for promote) ---------------------

def _toml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def serialize_state_config(state: StateConfig) -> str:
    """
    Regenerate state.toml text from a StateConfig. Deliberately a narrow,
    schema-specific serializer (not a general TOML writer) since the
    schema is small and fixed — avoids pulling in a write-capable TOML
    dependency for one use case.
    """
    lines = []
    for user in state.users.values():
        lines.append(f'[users."{_toml_escape(user.username)}"]')
        lines.append(f'role = "{_toml_escape(user.role)}"')
        lines.append(f"connection_hours = {user.connection_hours}")
        badges_str = ", ".join(f'"{_toml_escape(b)}"' for b in user.badges)
        lines.append(f"badges = [{badges_str}]")
        if user.joined:
            lines.append(f'joined = "{_toml_escape(user.joined)}"')
        lines.append("")
    return "\n".join(lines)


def write_state_config(state: StateConfig, path: Path) -> None:
    """Atomic write: build the new file fully in a temp file, then rename
    over the original. Never leaves state.toml half-written."""
    text = serialize_state_config(state)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# --- CLI entrypoint ----------------------------------------------------------

def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1

    server_dir = Path(argv[1])
    server_toml = server_dir / "server.toml"
    state_toml = server_dir / "state.toml"

    try:
        server = load_server_config(server_toml)
        known_roles = {r.name for r in server.roles}
        state = load_state_config(state_toml, known_roles=known_roles)
    except (ServerConfigError, StateConfigError) as e:
        print(f"Server config rejected: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"Missing config file: {e}", file=sys.stderr)
        return 1

    rest = argv[2:]

    try:
        if not rest:
            cmd_dashboard(server, state)
        elif rest[0] == "profile" and len(rest) == 2:
            cmd_profile(server, state, rest[1])
        elif rest[0] == "roles":
            cmd_roles(server)
        elif rest[0] == "promote" and len(rest) == 4:
            new_state = cmd_promote(server, state, rest[1], rest[2], rest[3])
            write_state_config(new_state, state_toml)
            print(f"{rest[2]} promoted to {rest[3]}")
        elif rest[0] == "award" and len(rest) == 4:
            new_state = cmd_award(server, state, rest[1], rest[2], rest[3])
            if new_state is state:
                print(f"{rest[2]} already has badge '{rest[3]}' — no change")
            else:
                write_state_config(new_state, state_toml)
                print(f"{rest[2]} awarded badge '{rest[3]}'")
        else:
            print(__doc__)
            return 1
    except MyServerError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
