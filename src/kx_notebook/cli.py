"""Command-line interface for explicit IPython hook and config management."""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
import unicodedata
from pathlib import Path
from typing import Optional, Sequence

from . import __version__
from .config import ConfigError, config_path, load_config

HOOK_NAME = "00-kx-notebook.py"
HOOK_CONTENT = (
    "# Installed by kx-notebook; remove with: kx-notebook uninstall\n"
    "# Equivalent interactive command: %load_ext kx_notebook\n"
    'get_ipython().run_line_magic("load_ext", "kx_notebook")  # noqa: F821\n'
)
PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def hook_path(profile: str = "default", ipython_dir: Optional[Path] = None) -> Path:
    """Return the one profile-specific startup hook path."""

    if not PROFILE_PATTERN.fullmatch(profile):
        raise ValueError("profile must use 1-64 letters, digits, dashes, or underscores")
    if ipython_dir is None:
        configured = os.environ.get("IPYTHONDIR")
        if configured:
            ipython_dir = Path(configured).expanduser()
        else:
            from IPython.paths import get_ipython_dir

            ipython_dir = Path(get_ipython_dir())
    return Path(ipython_dir) / f"profile_{profile}" / "startup" / HOOK_NAME


def install_hook(
    profile: str = "default",
    *,
    dry_run: bool = False,
    ipython_dir: Optional[Path] = None,
) -> str:
    """Install the exact hook idempotently, refusing to overwrite other content."""

    path = hook_path(profile, ipython_dir)
    _validate_hook_parents(path)
    if path.is_symlink():
        raise RuntimeError(f"refusing to replace symlink: {path}")
    if path.exists():
        if _read_hook(path) == HOOK_CONTENT:
            return f"already installed: {path}"
        raise RuntimeError(f"refusing to overwrite an existing non-kx-notebook hook: {path}")
    if dry_run:
        return f"would install: {path}"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            try:
                os.fchmod(handle.fileno(), 0o600)
            except OSError:
                pass
            handle.write(HOOK_CONTENT)
    except FileExistsError:
        if _read_hook(path) == HOOK_CONTENT:
            return f"already installed: {path}"
        raise RuntimeError(f"hook appeared concurrently and was not overwritten: {path}") from None
    return f"installed: {path}"


def uninstall_hook(
    profile: str = "default",
    *,
    dry_run: bool = False,
    ipython_dir: Optional[Path] = None,
) -> str:
    """Remove only the exact package-owned hook."""

    path = hook_path(profile, ipython_dir)
    _validate_hook_parents(path)
    if path.is_symlink():
        raise RuntimeError(f"refusing to remove symlink: {path}")
    if not path.exists():
        return f"not installed: {path}"
    if _read_hook(path) != HOOK_CONTENT:
        raise RuntimeError(f"refusing to remove a modified startup hook: {path}")
    if dry_run:
        return f"would uninstall: {path}"
    path.unlink()
    return f"uninstalled: {path}"


def hook_status(profile: str = "default", ipython_dir: Optional[Path] = None) -> str:
    """Describe the profile hook without changing it."""

    path = hook_path(profile, ipython_dir)
    _validate_hook_parents(path)
    if path.is_symlink():
        return f"unsafe symlink: {path}"
    if not path.exists():
        return f"not installed: {path}"
    if _read_hook(path) == HOOK_CONTENT:
        return f"installed: {path}"
    return f"occupied by different content: {path}"


def _validate_hook_parents(path: Path) -> None:
    for parent in (path.parent.parent, path.parent):
        if parent.is_symlink():
            raise RuntimeError(f"refusing to traverse symlinked IPython profile path: {parent}")


def _read_hook(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError(f"startup hook is not a regular file: {path}")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            return handle.read(len(HOOK_CONTENT) + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kx-notebook",
        description="Standalone q execution for IPython and Jupyter.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "uninstall", "status"):
        command = commands.add_parser(name)
        command.add_argument("--profile", default="default")
        if name != "status":
            command.add_argument("--dry-run", action="store_true")
    config = commands.add_parser("config", help="inspect or validate non-secret profiles")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser("path")
    validate = config_commands.add_parser("validate")
    validate.add_argument("--allow-missing", action="store_true")
    config_commands.add_parser("profiles")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point."""

    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "install":
            print(_terminal_output(install_hook(arguments.profile, dry_run=arguments.dry_run)))
        elif arguments.command == "uninstall":
            print(_terminal_output(uninstall_hook(arguments.profile, dry_run=arguments.dry_run)))
        elif arguments.command == "status":
            print(_terminal_output(hook_status(arguments.profile)))
        elif arguments.config_command == "path":
            print(_terminal_output(str(config_path())))
        elif arguments.config_command == "validate":
            selected = config_path()
            config = load_config(selected, missing_ok=arguments.allow_missing)
            print(
                _terminal_output(
                    f"valid: {selected} ({len(config.profiles)} profile"
                    f"{'' if len(config.profiles) == 1 else 's'})"
                )
            )
        elif arguments.config_command == "profiles":
            config = load_config()
            if not config.profiles:
                print("No profiles configured.")
            for name, profile in sorted(config.profiles.items()):
                default = " (default)" if name == config.default_profile else ""
                if profile.kind == "direct":
                    target = f" · {profile.host}:{profile.port}"
                elif profile.kind == "broker":
                    target = f" · {profile.base_url}"
                else:
                    target = ""
                print(f"{name}: {profile.kind}{target}{default}")
        return 0
    except (ConfigError, OSError, RuntimeError, ValueError) as error:
        print(_terminal_output(f"kx-notebook: error: {error}"), file=sys.stderr)
        return 2


def _terminal_output(value: str) -> str:
    parts: list[str] = []
    for character in value:
        if unicodedata.category(character).startswith("C"):
            codepoint = ord(character)
            if codepoint <= 0xFF:
                parts.append(f"\\x{codepoint:02x}")
            elif codepoint <= 0xFFFF:
                parts.append(f"\\u{codepoint:04x}")
            else:
                parts.append(f"\\U{codepoint:08x}")
        else:
            parts.append(character)
    return "".join(parts)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
