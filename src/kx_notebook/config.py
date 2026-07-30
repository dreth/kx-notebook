"""Strict non-secret profile configuration."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, Optional

from .defaults import DEFAULT_CONNECT_TIMEOUT_SECONDS, DEFAULT_QUERY_TIMEOUT_SECONDS
from .ipc import DEFAULT_MAX_RECEIVE_BYTES, MAX_RECEIVE_BYTES

if TYPE_CHECKING:
    import tomli as tomllib
elif sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on Python 3.9/3.10
    import tomli as tomllib

PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TOP_LEVEL_KEYS = {"default_profile", "profiles"}
PROFILE_KEYS = {
    "kind",
    "host",
    "port",
    "username",
    "password_env",
    "connect_timeout",
    "query_timeout",
    "max_receive_bytes",
    "base_url",
    "token_env",
    "timeout",
}
PROFILE_KEYS_BY_KIND = {
    "direct": {
        "kind",
        "host",
        "port",
        "username",
        "password_env",
        "connect_timeout",
        "query_timeout",
        "max_receive_bytes",
    },
    "broker": {"kind", "base_url", "token_env", "timeout"},
    "pykx": {"kind"},
}
MAX_CONFIG_BYTES = 1_048_576


class ConfigError(ValueError):
    """Invalid or unsafe configuration."""


@dataclass(frozen=True)
class Profile:
    """One evaluator profile containing metadata but never a secret."""

    name: str
    kind: str = "direct"
    host: Optional[str] = None
    port: Optional[int] = None
    username: str = ""
    password_env: Optional[str] = None
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    query_timeout: float = DEFAULT_QUERY_TIMEOUT_SECONDS
    max_receive_bytes: int = DEFAULT_MAX_RECEIVE_BYTES
    base_url: Optional[str] = None
    token_env: Optional[str] = None
    timeout: float = DEFAULT_QUERY_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        _validate_profile(self)


@dataclass(frozen=True)
class Config:
    """Validated profile collection."""

    profiles: Mapping[str, Profile] = field(default_factory=dict)
    default_profile: Optional[str] = None

    def __post_init__(self) -> None:
        copied = dict(self.profiles)
        for name, profile in copied.items():
            if name != profile.name:
                raise ConfigError(f"profile key {name!r} does not match its name")
        if self.default_profile is not None and self.default_profile not in copied:
            raise ConfigError(f"default_profile {self.default_profile!r} does not exist")
        object.__setattr__(self, "profiles", copied)

    def profile(self, name: Optional[str] = None) -> Profile:
        selected = name or self.default_profile
        if selected is None:
            raise ConfigError("no profile selected and no default_profile is configured")
        try:
            return self.profiles[selected]
        except KeyError as error:
            raise ConfigError(f"unknown profile {selected!r}") from error


def config_path() -> Path:
    """Return an XDG/platform-appropriate TOML path."""

    override = os.environ.get("KX_NOTEBOOK_CONFIG")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return root / "kx-notebook" / "config.toml"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "kx-notebook" / "config.toml"
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "kx-notebook" / "config.toml"


def load_config(path: Optional[Path] = None, *, missing_ok: bool = True) -> Config:
    """Read and strictly validate TOML."""

    selected = Path(path) if path is not None else config_path()
    raw = _read_config(selected, missing_ok=missing_ok)
    if raw is None:
        return Config()
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, RecursionError):
        raise ConfigError(f"invalid TOML in {selected}") from None
    if not isinstance(document, dict):
        raise ConfigError("configuration root must be a TOML table")
    unknown = set(document) - TOP_LEVEL_KEYS
    if unknown:
        raise ConfigError(f"unknown top-level key {_first(unknown)!r}")
    default = document.get("default_profile")
    if default is not None and not isinstance(default, str):
        raise ConfigError("default_profile must be a string")
    raw_profiles = document.get("profiles", {})
    if not isinstance(raw_profiles, dict):
        raise ConfigError("profiles must be a TOML table")
    profiles: dict[str, Profile] = {}
    for name, raw_profile in raw_profiles.items():
        if not isinstance(name, str) or not isinstance(raw_profile, dict):
            raise ConfigError("each profile must be a named TOML table")
        unknown_profile = set(raw_profile) - PROFILE_KEYS
        if unknown_profile:
            raise ConfigError(f"profile {name!r} has unknown key {_first(unknown_profile)!r}")
        kind = raw_profile.get("kind", "direct")
        allowed = PROFILE_KEYS_BY_KIND.get(kind) if isinstance(kind, str) else None
        if allowed is not None:
            wrong_kind = set(raw_profile) - allowed
            if wrong_kind:
                raise ConfigError(
                    f"profile {name!r} of kind {kind!r} cannot contain {_first(wrong_kind)!r}"
                )
        try:
            profiles[name] = Profile(name=name, **raw_profile)
        except TypeError as error:
            raise ConfigError(f"profile {name!r} has invalid fields: {error}") from error
    return Config(profiles, default)


def _read_config(path: Path, *, missing_ok: bool) -> Optional[bytes]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise ConfigError(f"configuration file does not exist: {path}") from None
    except OSError:
        raise ConfigError(f"configuration file cannot be opened safely: {path}") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ConfigError(f"configuration path is not a regular file: {path}")
        if metadata.st_size > MAX_CONFIG_BYTES:
            raise ConfigError(
                f"configuration file exceeds the {MAX_CONFIG_BYTES}-byte limit: {path}"
            )
        chunks: list[bytes] = []
        remaining = MAX_CONFIG_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_CONFIG_BYTES:
            raise ConfigError(
                f"configuration file exceeds the {MAX_CONFIG_BYTES}-byte limit: {path}"
            )
        return raw
    except OSError:
        raise ConfigError(f"configuration file cannot be read safely: {path}") from None
    finally:
        os.close(descriptor)


def save_config(config: Config, path: Optional[Path] = None) -> Path:
    """Atomically persist profile metadata with owner-only permissions."""

    if not isinstance(config, Config):
        raise TypeError("config must be a Config")
    selected = Path(path) if path is not None else config_path()
    selected.parent.mkdir(parents=True, exist_ok=True)
    text = _toml(config)
    descriptor, temporary = tempfile.mkstemp(prefix=".config.", suffix=".toml", dir=selected.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temporary, selected)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return selected


def resolve_password(
    profile: Profile,
    explicit: Optional[str] = None,
    *,
    use_keyring: bool = False,
) -> Optional[str]:
    """Resolve a password from runtime input, the named environment variable,
    then (only when requested) the optional system keyring.
    """

    if explicit is not None:
        return _secret("password", explicit)
    if profile.password_env is not None:
        value = os.environ.get(profile.password_env)
        if value is not None:
            return _secret("password environment value", value)
    if use_keyring:
        try:
            import keyring
        except ImportError as error:
            raise ConfigError(
                "keyring support is not installed; install kx-notebook[keyring]"
            ) from error
        value = keyring.get_password("kx-notebook", f"{profile.name}:{profile.username}")
        if value is not None:
            return _secret("keyring password", value)
    return None


def resolve_token(profile: Profile, explicit: Optional[str] = None) -> Optional[str]:
    """Resolve a broker token from runtime input or its named environment variable."""

    if explicit is not None:
        return _token_secret("token", explicit)
    if profile.token_env is None:
        return None
    value = os.environ.get(profile.token_env)
    return None if value is None else _token_secret("token environment value", value)


def _validate_profile(profile: Profile) -> None:
    if not PROFILE_NAME.fullmatch(profile.name):
        raise ConfigError(
            "profile name must use 1-64 letters, digits, dots, dashes, or underscores"
        )
    if profile.kind not in {"direct", "broker", "pykx"}:
        raise ConfigError("profile kind must be 'direct', 'broker', or 'pykx'")
    for field_name in ("password_env", "token_env"):
        value = getattr(profile, field_name)
        if value is not None and (len(value) > 128 or not ENV_NAME.fullmatch(value)):
            raise ConfigError(f"{field_name} must be a valid environment variable name")
    for field_name in ("connect_timeout", "query_timeout", "timeout"):
        value = getattr(profile, field_name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{field_name} must be numeric")
        if not 0 < float(value) <= 86_400:
            raise ConfigError(f"{field_name} must be greater than 0 and at most 86400")
    if (
        isinstance(profile.max_receive_bytes, bool)
        or not isinstance(profile.max_receive_bytes, int)
        or not 1_024 <= profile.max_receive_bytes <= MAX_RECEIVE_BYTES
    ):
        raise ConfigError(f"max_receive_bytes must be between 1024 and {MAX_RECEIVE_BYTES}")
    if profile.kind == "direct":
        if (
            not isinstance(profile.host, str)
            or not profile.host
            or len(profile.host) > 1_024
            or any(character.isspace() for character in profile.host)
            or not profile.host.isprintable()
            or "\x00" in profile.host
            or "://" in profile.host
        ):
            raise ConfigError("direct profile host must be a plain hostname or address")
        if (
            isinstance(profile.port, bool)
            or not isinstance(profile.port, int)
            or not 1 <= profile.port <= 65_535
        ):
            raise ConfigError("direct profile port must be between 1 and 65535")
        if profile.base_url is not None or profile.token_env is not None:
            raise ConfigError("direct profiles cannot contain broker fields")
        if profile.timeout != DEFAULT_QUERY_TIMEOUT_SECONDS:
            raise ConfigError("direct profiles cannot set broker timeout")
    elif profile.kind == "broker":
        if not isinstance(profile.base_url, str) or not profile.base_url:
            raise ConfigError("broker profile base_url is required")
        # Import lazily to keep config validation aligned with the adapter.
        from .evaluators import _broker_url

        try:
            _broker_url(profile.base_url)
        except (TypeError, ValueError) as error:
            raise ConfigError(f"invalid broker base_url: {error}") from None
        if (
            profile.host is not None
            or profile.port is not None
            or profile.password_env is not None
            or profile.username
            or profile.connect_timeout != DEFAULT_CONNECT_TIMEOUT_SECONDS
            or profile.query_timeout != DEFAULT_QUERY_TIMEOUT_SECONDS
            or profile.max_receive_bytes != DEFAULT_MAX_RECEIVE_BYTES
        ):
            raise ConfigError("broker profiles cannot contain direct IPC fields")
    else:
        forbidden = (
            profile.host,
            profile.port,
            profile.password_env,
            profile.base_url,
            profile.token_env,
        )
        if any(value is not None for value in forbidden):
            raise ConfigError("pykx profiles cannot contain connection fields")
        if (
            profile.username
            or profile.connect_timeout != DEFAULT_CONNECT_TIMEOUT_SECONDS
            or profile.query_timeout != DEFAULT_QUERY_TIMEOUT_SECONDS
            or profile.max_receive_bytes != DEFAULT_MAX_RECEIVE_BYTES
            or profile.timeout != DEFAULT_QUERY_TIMEOUT_SECONDS
        ):
            raise ConfigError("pykx profiles cannot contain connection timeouts")
    if (
        not isinstance(profile.username, str)
        or len(profile.username) > 1_024
        or any(character in profile.username for character in ("\x00", "\r", "\n", ":"))
    ):
        raise ConfigError("username must be a single-line string without colons or NUL bytes")
    try:
        profile.username.encode("utf-8")
    except UnicodeEncodeError:
        raise ConfigError("username must contain valid UTF-8 text") from None


def _toml(config: Config) -> str:
    lines: list[str] = []
    if config.default_profile is not None:
        lines.append(f"default_profile = {_toml_string(config.default_profile)}")
    for name in sorted(config.profiles):
        profile = config.profiles[name]
        if lines:
            lines.append("")
        lines.append(f"[profiles.{_toml_key(name)}]")
        lines.append(f"kind = {_toml_string(profile.kind)}")
        if profile.kind == "direct":
            lines.extend(
                (
                    f"host = {_toml_string(profile.host or '')}",
                    f"port = {profile.port}",
                )
            )
            if profile.username:
                lines.append(f"username = {_toml_string(profile.username)}")
            if profile.password_env:
                lines.append(f"password_env = {_toml_string(profile.password_env)}")
            if profile.connect_timeout != DEFAULT_CONNECT_TIMEOUT_SECONDS:
                lines.append(f"connect_timeout = {profile.connect_timeout:g}")
            if profile.query_timeout != DEFAULT_QUERY_TIMEOUT_SECONDS:
                lines.append(f"query_timeout = {profile.query_timeout:g}")
            if profile.max_receive_bytes != DEFAULT_MAX_RECEIVE_BYTES:
                lines.append(f"max_receive_bytes = {profile.max_receive_bytes}")
        elif profile.kind == "broker":
            lines.append(f"base_url = {_toml_string(profile.base_url or '')}")
            if profile.token_env:
                lines.append(f"token_env = {_toml_string(profile.token_env)}")
            if profile.timeout != DEFAULT_QUERY_TIMEOUT_SECONDS:
                lines.append(f"timeout = {profile.timeout:g}")
    return "\n".join(lines) + "\n"


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_key(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else _toml_string(value)


def _secret(name: str, value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4_096 or "\x00" in value:
        raise ConfigError(f"{name} must be a non-empty bounded string without NUL bytes")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ConfigError(f"{name} must contain valid UTF-8 text") from None
    return value


def _token_secret(name: str, value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 8_192
        or not value.isascii()
        or not value.isprintable()
        or any(character.isspace() for character in value)
    ):
        raise ConfigError(f"{name} must be a bounded printable ASCII token")
    return value


def _first(values: set[str]) -> str:
    return sorted(values)[0]
