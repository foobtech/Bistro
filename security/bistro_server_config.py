"""
Bistro server.toml parser + validator.

server.toml is untrusted input — it comes from a remote server operator,
not from the local user. This module ONLY reads structured data out of it
(strings, numbers, lists of hex codes, etc). It never evaluates, execs, or
treats any field as a shell command or import path. Every field is checked
against an expected shape before being handed back to the rest of Bistro.

Actual asset bytes referenced by paths in here still go through the full
security pipeline (bistro_asset_security.py + bistro_ingest_asset.py) —
this module only validates the STRUCTURE of the config, not the files it
points to.

Expected schema (see project README "Concept" section):

    [server]
    name = "my-cozy-server"
    description = "..."          # optional

    [[roles]]
    name = "Owner"
    color = "#efebe9"             # hex, validated
    can_promote = true            # optional, default false
    can_award_badges = true       # optional, default false

    [[roles]]
    name = "Helper"
    color = "#3e2723"

    [resources]
    themes = ["themes/espresso.toml"]
    fonts = ["fonts/custom.ttf"]
    ascii = ["ascii/login.txt"]
    wallpapers = ["wallpapers/rain.mp4"]
"""

from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import tomllib
import re

HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_SERVER_NAME_LEN = 64
MAX_DESCRIPTION_LEN = 350
MAX_ROLES = 32
MAX_RESOURCES_PER_CATEGORY = 100  # sane upper bound, prevents absurd manifests


class ServerConfigError(Exception):
    """Raised when server.toml is malformed, oversized, or fails any
    structural validation. Callers must reject the whole server on this —
    never partially trust a config that failed validation."""
    pass


@dataclass
class Role:
    name: str
    color: str
    can_promote: bool = False
    can_award_badges: bool = False


@dataclass
class ServerConfig:
    name: str
    description: str
    roles: list[Role] = field(default_factory=list)
    # category -> list of relative resource paths, e.g. "fonts" -> ["fonts/x.ttf"]
    # These are RAW strings from the config — still untrusted, still need to
    # go through validate_resource_path() before any file is fetched/written.
    resources: dict[str, list[str]] = field(default_factory=dict)


_ALLOWED_RESOURCE_CATEGORIES = {"themes", "fonts", "ascii", "wallpapers", "kitty"}


def _validate_str(value, field_name: str, max_len: int) -> str:
    if not isinstance(value, str):
        raise ServerConfigError(f"{field_name} must be a string, got {type(value).__name__}")
    if len(value) == 0:
        raise ServerConfigError(f"{field_name} cannot be empty")
    if len(value) > max_len:
        raise ServerConfigError(f"{field_name} exceeds max length {max_len}")
    return value


def _validate_hex_color(value, field_name: str) -> str:
    if not isinstance(value, str) or not HEX_COLOR_RE.match(value):
        raise ServerConfigError(
            f"{field_name} must be a 6-digit hex color like '#3e2723', got {value!r}"
        )
    return value


def _validate_role(raw: dict) -> Role:
    if not isinstance(raw, dict):
        raise ServerConfigError(f"Each role must be a table, got {type(raw).__name__}")

    name = _validate_str(raw.get("name"), "role.name", 32)
    color = _validate_hex_color(raw.get("color"), "role.color")

    can_promote = raw.get("can_promote", False)
    can_award_badges = raw.get("can_award_badges", False)
    if not isinstance(can_promote, bool):
        raise ServerConfigError("role.can_promote must be a boolean")
    if not isinstance(can_award_badges, bool):
        raise ServerConfigError("role.can_award_badges must be a boolean")

    return Role(name=name, color=color, can_promote=can_promote, can_award_badges=can_award_badges)


def _validate_resources(raw: dict) -> dict[str, list[str]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ServerConfigError("[resources] must be a table")

    result: dict[str, list[str]] = {}
    for category, paths in raw.items():
        if category not in _ALLOWED_RESOURCE_CATEGORIES:
            raise ServerConfigError(
                f"Unknown resource category {category!r}. "
                f"Allowed: {sorted(_ALLOWED_RESOURCE_CATEGORIES)}"
            )
        if not isinstance(paths, list):
            raise ServerConfigError(f"resources.{category} must be a list of paths")
        if len(paths) > MAX_RESOURCES_PER_CATEGORY:
            raise ServerConfigError(
                f"resources.{category} has {len(paths)} entries, "
                f"exceeds max of {MAX_RESOURCES_PER_CATEGORY}"
            )
        clean_paths = []
        for p in paths:
            if not isinstance(p, str) or len(p) == 0:
                raise ServerConfigError(f"resources.{category} entries must be non-empty strings")
            # NOTE: this is a shape check only. The actual traversal/escape
            # check happens later in validate_resource_path() when the file
            # is fetched — deliberately not duplicated here so there's a
            # single source of truth for that logic.
            clean_paths.append(p)
        result[category] = clean_paths

    return result


def parse_server_config(raw_bytes: bytes) -> ServerConfig:
    """
    Parse and fully validate a server.toml file's raw bytes.

    Raises ServerConfigError on ANY structural problem — missing fields,
    wrong types, malformed TOML, oversized values, too many roles, unknown
    resource categories, etc. There is no partial-success mode: either the
    whole config is valid or the server is rejected outright.
    """
    if len(raw_bytes) > 64 * 1024:
        raise ServerConfigError("server.toml exceeds 64 KB size cap")

    try:
        raw = tomllib.loads(raw_bytes.decode("utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ServerConfigError(f"Malformed TOML: {e}")
    except UnicodeDecodeError as e:
        raise ServerConfigError(f"server.toml is not valid UTF-8: {e}")

    server_section = raw.get("server")
    if not isinstance(server_section, dict):
        raise ServerConfigError("Missing required [server] section")

    name = _validate_str(server_section.get("name"), "server.name", MAX_SERVER_NAME_LEN)
    description = server_section.get("description", "")
    if description:
        description = _validate_str(description, "server.description", MAX_DESCRIPTION_LEN)

    raw_roles = raw.get("roles", [])
    if not isinstance(raw_roles, list):
        raise ServerConfigError("[[roles]] must be an array of tables")
    if len(raw_roles) > MAX_ROLES:
        raise ServerConfigError(f"Too many roles: {len(raw_roles)} > {MAX_ROLES}")
    roles = [_validate_role(r) for r in raw_roles]

    resources = _validate_resources(raw.get("resources"))

    return ServerConfig(
        name=name,
        description=description,
        roles=roles,
        resources=resources,
    )


def load_server_config(path: str | Path) -> ServerConfig:
    """Convenience wrapper: read a server.toml file from disk and parse it."""
    data = Path(path).read_bytes()
    return parse_server_config(data)


# --- Example usage / smoke test -----------------------------------------

if __name__ == "__main__":
    good_toml = b"""
[server]
name = "my-cozy-server"
description = "A chill place to hang out"

[[roles]]
name = "Owner"
color = "#3e2723"
can_promote = true
can_award_badges = true

[[roles]]
name = "Helper"
color = "#efebe9"

[resources]
themes = ["themes/espresso.toml"]
fonts = ["fonts/custom.ttf"]
wallpapers = ["wallpapers/rain.mp4"]
"""
    cfg = parse_server_config(good_toml)
    print(f"Parsed OK: {cfg.name!r}, {len(cfg.roles)} roles, "
          f"resource categories: {list(cfg.resources.keys())}")

    # Malicious attempt: bogus resource category
    bad_toml = b"""
[server]
name = "evil-server"

[resources]
executables = ["payload.sh"]
"""
    try:
        parse_server_config(bad_toml)
        print("FAIL: should have rejected unknown resource category")
    except ServerConfigError as e:
        print(f"Correctly rejected: {e}")

    # Malicious attempt: bad color format
    bad_color_toml = b"""
[server]
name = "sneaky-server"

[[roles]]
name = "Owner"
color = "javascript:alert(1)"
"""
    try:
        parse_server_config(bad_color_toml)
        print("FAIL: should have rejected bad color")
    except ServerConfigError as e:
        print(f"Correctly rejected: {e}")
