from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from kx_notebook.config import Config, Profile, save_config

REPOSITORY = Path(__file__).resolve().parents[1]


def run_cli(
    *arguments: str,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "kx_notebook.cli", *arguments],
        cwd=REPOSITORY,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def cli_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["IPYTHONDIR"] = str(tmp_path / "ipython")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    return env


def hook_files(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "ipython").glob("profile_default/startup/*.py"))


def test_package_import_is_lightweight() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, kx_notebook; "
                "assert 'IPython' not in sys.modules; "
                "assert 'kx_notebook.magic' not in sys.modules; "
                "from kx_notebook import DirectQEvaluator; "
                "assert DirectQEvaluator.__name__ == 'DirectQEvaluator'"
            ),
        ],
        cwd=REPOSITORY,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr


def test_startup_hook_install_status_uninstall_is_scoped_and_idempotent(
    tmp_path: Path,
) -> None:
    env = cli_env(tmp_path)
    unrelated = tmp_path / "ipython" / "profile_default" / "startup" / "10-user.py"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("user_setting = True\n", encoding="utf-8")

    dry_install = run_cli("install", "--profile", "default", "--dry-run", env=env)
    assert dry_install.returncode == 0, dry_install.stderr
    assert "would" in dry_install.stdout.lower()
    assert hook_files(tmp_path) == [unrelated]

    first = run_cli("install", "--profile", "default", env=env)
    assert first.returncode == 0, first.stderr
    installed = [path for path in hook_files(tmp_path) if path != unrelated]
    assert len(installed) == 1
    hook = installed[0]
    content = hook.read_text(encoding="utf-8")
    assert "load_ext" in content
    assert "kx_notebook" in content

    second = run_cli("install", "--profile", "default", env=env)
    assert second.returncode == 0, second.stderr
    assert [path for path in hook_files(tmp_path) if path != unrelated] == [hook]
    assert hook.read_text(encoding="utf-8") == content

    status = run_cli("status", "--profile", "default", env=env)
    assert status.returncode == 0, status.stderr
    assert "installed" in status.stdout.lower()

    dry_uninstall = run_cli(
        "uninstall",
        "--profile",
        "default",
        "--dry-run",
        env=env,
    )
    assert dry_uninstall.returncode == 0, dry_uninstall.stderr
    assert hook.is_file()
    assert unrelated.is_file()

    first_uninstall = run_cli("uninstall", "--profile", "default", env=env)
    assert first_uninstall.returncode == 0, first_uninstall.stderr
    assert not hook.exists()
    assert unrelated.read_text(encoding="utf-8") == "user_setting = True\n"

    second_uninstall = run_cli("uninstall", "--profile", "default", env=env)
    assert second_uninstall.returncode == 0, second_uninstall.stderr
    assert unrelated.is_file()


@pytest.mark.parametrize("profile", ["../escape", "/absolute", "bad/name", ""])
def test_startup_hook_rejects_unsafe_profile_names(
    tmp_path: Path,
    profile: str,
) -> None:
    env = cli_env(tmp_path)

    completed = run_cli("install", "--profile", profile, env=env)

    assert completed.returncode != 0
    assert not list(tmp_path.rglob("*kx*notebook*.py"))


def test_config_cli_path_validate_and_profiles(tmp_path: Path) -> None:
    env = cli_env(tmp_path)
    path = tmp_path / "config" / "kx-notebook" / "config.toml"
    save_config(
        Config(
            profiles={
                "local": Profile(
                    name="local",
                    host="127.0.0.1",
                    port=5000,
                )
            },
            default_profile="local",
        ),
        path,
    )

    path_result = run_cli("config", "path", env=env)
    validate_result = run_cli("config", "validate", env=env)
    profiles_result = run_cli("config", "profiles", env=env)

    assert path_result.returncode == 0, path_result.stderr
    assert Path(path_result.stdout.strip()) == path
    assert validate_result.returncode == 0, validate_result.stderr
    assert "valid" in validate_result.stdout.lower()
    assert profiles_result.returncode == 0, profiles_result.stderr
    assert "local" in profiles_result.stdout
    assert "127.0.0.1:5000" in profiles_result.stdout


def test_config_cli_error_is_concise_and_does_not_echo_plaintext_secret(
    tmp_path: Path,
) -> None:
    env = cli_env(tmp_path)
    path = tmp_path / "config" / "kx-notebook" / "config.toml"
    path.parent.mkdir(parents=True)
    fake_password = "fixture-cli-password"
    path.write_text(
        "\n".join(
            [
                "[profiles.local]",
                'host = "localhost"',
                "port = 5000",
                f'password = "{fake_password}"',
            ]
        ),
        encoding="utf-8",
    )

    completed = run_cli("config", "validate", env=env)
    combined = completed.stdout + completed.stderr

    assert completed.returncode != 0
    assert fake_password not in combined
    assert "traceback" not in combined.lower()


def test_cli_neutralizes_terminal_controls_in_environment_paths(tmp_path: Path) -> None:
    env = cli_env(tmp_path)
    env["IPYTHONDIR"] = str(tmp_path / "\x1b]8;;https://evil.invalid\x07ipython")

    completed = run_cli("status", "--profile", "default", env=env)

    assert completed.returncode == 0
    assert "\x1b" not in completed.stdout
    assert "\x07" not in completed.stdout
    assert "\\x1b" in completed.stdout
