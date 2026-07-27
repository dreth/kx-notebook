from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kx_notebook.config import (
    MAX_CONFIG_BYTES,
    Config,
    Profile,
    config_path,
    load_config,
    resolve_password,
    save_config,
)


def test_config_round_trip_contains_only_non_secret_profile_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "kx-notebook" / "config.toml"
    fake_password = "fixture-config-password"
    monkeypatch.setenv("KX_NOTEBOOK_TEST_PASSWORD", fake_password)
    config = Config(
        profiles={
            "local": Profile(
                name="local",
                host="127.0.0.1",
                port=5000,
                username="alice",
                password_env="KX_NOTEBOOK_TEST_PASSWORD",
                kind="direct",
            )
        },
        default_profile="local",
    )

    save_config(config, path)
    loaded = load_config(path)
    text = path.read_text(encoding="utf-8")

    assert loaded == config
    assert fake_password not in text
    assert "password =" not in text
    assert "KX_NOTEBOOK_TEST_PASSWORD" in text
    assert resolve_password(loaded.profiles["local"]) == fake_password


def test_config_path_uses_xdg_config_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert config_path() == tmp_path / "kx-notebook" / "config.toml"


def test_missing_config_loads_an_empty_config(tmp_path: Path) -> None:
    loaded = load_config(tmp_path / "does-not-exist.toml")

    assert loaded.profiles == {}
    assert loaded.default_profile is None


def test_config_reader_rejects_oversized_special_and_symlink_paths(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized.toml"
    with oversized.open("wb") as handle:
        handle.truncate(MAX_CONFIG_BYTES + 1)
    with pytest.raises(ValueError, match="limit|exceeds"):
        load_config(oversized)

    if hasattr(os, "mkfifo"):
        fifo = tmp_path / "config.fifo"
        os.mkfifo(fifo)
        with pytest.raises(ValueError, match="regular"):
            load_config(fifo)

    target = tmp_path / "target.toml"
    target.write_text("", encoding="utf-8")
    link = tmp_path / "linked.toml"
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError):
        return
    with pytest.raises(ValueError, match="safely|regular"):
        load_config(link)


def test_profile_receive_limit_cannot_request_multi_gigabyte_allocations() -> None:
    with pytest.raises(ValueError, match="max_receive_bytes"):
        Profile(
            name="local",
            host="localhost",
            port=5000,
            max_receive_bytes=64 * 1024 * 1024 + 1,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "", "host": "localhost", "port": 5000},
        {"name": "bad name", "host": "localhost", "port": 5000},
        {"name": "local", "host": "", "port": 5000},
        {"name": "local", "host": "localhost", "port": 0},
        {"name": "local", "host": "localhost", "port": 65_536},
        {"name": "local", "host": "localhost", "port": True},
        {"name": "local", "host": "localhost", "port": 5000, "username": "a\nb"},
        {"name": "local", "host": "localhost", "port": 5000, "username": "a:b"},
        {
            "name": "local",
            "host": "localhost",
            "port": 5000,
            "password_env": "not-an-env-name",
        },
        {
            "name": "local",
            "host": "localhost",
            "port": 5000,
            "kind": "unknown",
        },
    ],
)
def test_profile_validation_is_strict(kwargs: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        profile = Profile(**kwargs)  # type: ignore[arg-type]
        Config(profiles={profile.name or "profile": profile})


def test_mapping_key_must_match_profile_name() -> None:
    with pytest.raises(ValueError, match="name|key"):
        Config(
            profiles={
                "other": Profile(name="local", host="localhost", port=5000),
            }
        )


def test_default_profile_must_exist() -> None:
    with pytest.raises(ValueError, match="default|missing"):
        Config(
            profiles={
                "local": Profile(name="local", host="localhost", port=5000),
            },
            default_profile="missing",
        )


@pytest.mark.parametrize(
    "text",
    [
        """
[profiles.local]
host = "localhost"
port = 5000
password = "plaintext-is-forbidden"
""",
        """
[profiles.local]
host = "localhost"
port = 5000
tls = true
""",
        """
[profiles.local]
host = "localhost"
port = 5000
url = "q://localhost:5000"
""",
        """
[profiles.local]
host = "localhost"
port = 5000
surprise = "unknown"
""",
        """
default_profile = "missing"
[profiles.local]
host = "localhost"
port = 5000
""",
        "not = [valid toml",
    ],
)
def test_config_rejects_secrets_unknown_keys_and_malformed_toml(
    tmp_path: Path,
    text: str,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises((TypeError, ValueError)):
        load_config(path)


def test_config_rejects_pathologically_nested_toml_as_a_config_error(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nested.toml"
    path.write_text("x = " + "[" * 2_000 + "0" + "]" * 2_000, encoding="utf-8")

    with pytest.raises(ValueError, match="invalid TOML"):
        load_config(path)


@pytest.mark.parametrize(
    "text",
    [
        """
[profiles.local]
kind = "broker"
base_url = "http://127.0.0.1:8765"
connect_timeout = 5.0
""",
        """
[profiles.local]
kind = "pykx"
timeout = 30.0
""",
    ],
)
def test_config_rejects_fields_from_another_profile_kind(
    tmp_path: Path,
    text: str,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match="cannot contain"):
        load_config(path)


def test_explicit_password_wins_over_environment_and_keyring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = Profile(
        name="local",
        host="localhost",
        port=5000,
        username="alice",
        password_env="KX_NOTEBOOK_TEST_PASSWORD",
    )
    monkeypatch.setenv("KX_NOTEBOOK_TEST_PASSWORD", "environment-password")
    fake_keyring = SimpleNamespace(get_password=lambda _service, _username: "keyring-password")
    monkeypatch.setitem(sys.modules, "keyring", fake_keyring)

    assert (
        resolve_password(profile, explicit="explicit-password", use_keyring=True)
        == "explicit-password"
    )
    assert resolve_password(profile, use_keyring=True) == "environment-password"


def test_keyring_is_optional_lazy_and_used_only_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = Profile(
        name="local",
        host="localhost",
        port=5000,
        username="alice",
    )
    calls: list[tuple[str, str]] = []
    fake_keyring = SimpleNamespace(
        get_password=lambda service, username: (
            calls.append((service, username)) or "keyring-password"
        )
    )
    monkeypatch.setitem(sys.modules, "keyring", fake_keyring)

    assert resolve_password(profile, use_keyring=False) is None
    assert calls == []
    assert resolve_password(profile, use_keyring=True) == "keyring-password"
    assert len(calls) == 1
    assert calls[0][1].endswith(":alice")


def test_missing_optional_keyring_has_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = Profile(
        name="local",
        host="localhost",
        port=5000,
        username="alice",
    )
    monkeypatch.setitem(sys.modules, "keyring", None)

    with pytest.raises((RuntimeError, ValueError), match="keyring|install"):
        resolve_password(profile, use_keyring=True)


def test_environment_password_is_never_captured_in_repr_or_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_password = "fixture-env-password"
    monkeypatch.setenv("KX_NOTEBOOK_TEST_PASSWORD", fake_password)
    profile = Profile(
        name="local",
        host="localhost",
        port=5000,
        username="alice",
        password_env="KX_NOTEBOOK_TEST_PASSWORD",
    )
    config = Config(profiles={"local": profile}, default_profile="local")
    path = tmp_path / "config.toml"
    save_config(config, path)

    assert fake_password not in repr(profile)
    assert fake_password not in repr(config)
    assert fake_password not in path.read_text(encoding="utf-8")
    assert os.environ["KX_NOTEBOOK_TEST_PASSWORD"] == fake_password
