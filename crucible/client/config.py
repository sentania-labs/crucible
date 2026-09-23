"""Where the client's base URL, token, and rendering time zone come from.

One precedence for each setting: the command-line flag, then the environment, then the
client configuration file, then the default. The token has no flag, on purpose: a
flag's value is visible to every process on the host that can list arguments.

- base URL: `--api-url`, then `CRUCIBLE_URL`, then `url` in the file.
- token: `CRUCIBLE_TOKEN`, then `token_file` in the file (a path whose contents are the
  token). `crucible admin` reads `CRUCIBLE_ADMIN_TOKEN` first, the name `crucible-admin`
  used, then the same two.
- time zone for `--table`: `--timezone`, then `CRUCIBLE_TIMEZONE`, then `timezone` in the
  file, then America/Chicago.
- the file: `CRUCIBLE_CLIENT_CONFIG`, else `$XDG_CONFIG_HOME/crucible/client.toml`
  (`~/.config/crucible/client.toml`). A missing file is no error; a malformed one is.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from crucible.client.envelope import UsageError
from crucible.client.http import validate_base_url

URL_ENV = "CRUCIBLE_URL"
TOKEN_ENV = "CRUCIBLE_TOKEN"
ADMIN_TOKEN_ENV = "CRUCIBLE_ADMIN_TOKEN"
TIMEZONE_ENV = "CRUCIBLE_TIMEZONE"
CONFIG_ENV = "CRUCIBLE_CLIENT_CONFIG"
DEFAULT_TIMEZONE = "America/Chicago"
FILE_KEYS = frozenset({"url", "token_file", "timezone"})


def config_path(environ: Mapping[str, str]) -> Path:
    explicit = environ.get(CONFIG_ENV, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    base = environ.get("XDG_CONFIG_HOME", "").strip() or str(Path.home() / ".config")
    return Path(base) / "crucible" / "client.toml"


def read_file(environ: Mapping[str, str]) -> dict[str, Any]:
    path = config_path(environ)
    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise UsageError(
            f"cannot read the client configuration {path}: {exc}", code="config"
        ) from None
    unknown = sorted(set(document) - FILE_KEYS)
    if unknown:
        raise UsageError(
            f"{path} has unknown keys {unknown}; it takes {sorted(FILE_KEYS)}", code="config"
        )
    return document


@dataclass(frozen=True)
class ClientConfig:
    base_url: str | None
    token: str | None
    zone: ZoneInfo


def resolve_zone(flag: str | None, environ: Mapping[str, str], file: Mapping[str, Any]) -> ZoneInfo:
    name = flag or environ.get(TIMEZONE_ENV, "").strip() or file.get("timezone") or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError):
        raise UsageError(f"unknown time zone {name!r}", code="config") from None


def resolve(
    *,
    api_url: str | None,
    timezone: str | None,
    token_envs: tuple[str, ...] = (TOKEN_ENV,),
    environ: Mapping[str, str] | None = None,
) -> ClientConfig:
    env = os.environ if environ is None else environ
    file = read_file(env)
    zone = resolve_zone(timezone, env, file)
    url_source = "--api-url"
    url = (api_url or "").strip()
    if not url:
        url_source, url = URL_ENV, env.get(URL_ENV, "").strip()
    if not url and file.get("url"):
        url_source, url = f"url in {config_path(env)}", str(file["url"]).strip()
    base_url = validate_base_url(url, source=url_source) if url else None
    token = None
    for name in token_envs:
        value = env.get(name, "").strip()
        if value:
            token = value
            break
    if token is None and file.get("token_file"):
        path = Path(str(file["token_file"])).expanduser()
        try:
            token = path.read_text(encoding="utf-8").strip() or None
        except OSError as exc:
            raise UsageError(f"cannot read token_file {path}: {exc}", code="config") from None
    return ClientConfig(base_url=base_url, token=token, zone=zone)


def require_remote(config: ClientConfig, token_envs: tuple[str, ...]) -> tuple[str, str]:
    if not config.base_url:
        raise UsageError(
            f"no base URL: pass --api-url, set {URL_ENV}, or set `url` in the client "
            "configuration file",
            code="config",
        )
    if not config.token:
        raise UsageError(
            f"no token: set {' or '.join(token_envs)}, or `token_file` in the client "
            "configuration file",
            code="config",
        )
    return config.base_url, config.token
