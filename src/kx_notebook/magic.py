"""IPython ``%%q`` execution and ``%kx`` session management."""

from __future__ import annotations

import shlex
import time
from dataclasses import dataclass
from typing import Any, Optional

from IPython.core.error import UsageError
from IPython.core.magic import Magics, cell_magic, line_magic, magics_class

from . import display as display_module
from .config import ConfigError, Profile, load_config, resolve_password, resolve_token
from .contract import (
    DEFAULT_BYTE_LIMIT,
    DEFAULT_ROW_LIMIT,
    EvaluationResult,
    build_mime_bundle,
)
from .defaults import DEFAULT_CONNECT_TIMEOUT_SECONDS, DEFAULT_QUERY_TIMEOUT_SECONDS
from .evaluators import (
    BrokerEvaluator,
    DirectQEvaluator,
    EvaluationContext,
    Evaluator,
    EvaluatorLike,
    PyKXEvaluator,
    as_evaluator,
)


@dataclass
class _Session:
    evaluator: Optional[Evaluator] = None
    profile_name: Optional[str] = None
    label: Optional[str] = None
    row_limit: int = DEFAULT_ROW_LIMIT
    byte_limit: int = DEFAULT_BYTE_LIMIT
    include_q_source: bool = False


@dataclass(frozen=True)
class _QOptions:
    profile: Optional[str]
    row_limit: int
    byte_limit: int
    timeout: Optional[float]
    label: Optional[str]
    label_explicit: bool


_session = _Session()


def configure_evaluator(
    evaluator: EvaluatorLike,
    *,
    label: Optional[str] = None,
    row_limit: int = DEFAULT_ROW_LIMIT,
    byte_limit: int = DEFAULT_BYTE_LIMIT,
    include_q_source: bool = False,
) -> None:
    """Configure a callback or evaluator object for the current Python process."""

    build_mime_bundle(
        [],
        columns=["_validation"],
        label=label,
        row_limit=row_limit,
        byte_limit=byte_limit,
    )
    normalized = as_evaluator(evaluator)
    previous = _session.evaluator
    _session.evaluator = normalized
    _session.profile_name = None
    _session.label = label
    _session.row_limit = row_limit
    _session.byte_limit = byte_limit
    _session.include_q_source = bool(include_q_source)
    if previous is not None and previous is not normalized:
        previous.close()


def clear_evaluator() -> None:
    """Close and remove the active evaluator."""

    previous = _session.evaluator
    _session.evaluator = None
    _session.profile_name = None
    _session.label = None
    _session.row_limit = DEFAULT_ROW_LIMIT
    _session.byte_limit = DEFAULT_BYTE_LIMIT
    _session.include_q_source = False
    if previous is not None:
        previous.close()


@magics_class
class KxQMagics(Magics):
    """q execution and connection-management magics."""

    @cell_magic
    def q(self, line: str, cell: str) -> None:
        """Evaluate a complete q cell and publish a portable bounded result."""

        options = _parse_q_options(line)
        temporary = options.profile is not None
        evaluator = (
            _profile_evaluator(options.profile)
            if options.profile is not None
            else _session.evaluator
        )
        if evaluator is None:
            raise UsageError(
                "No q evaluator is active. Run `%kx connect HOST:PORT`, "
                "`%kx use PROFILE`, or call kx_notebook.configure_evaluator(...)."
            )
        context = EvaluationContext(
            row_limit=options.row_limit,
            byte_limit=options.byte_limit,
            timeout=options.timeout,
        )
        started = time.perf_counter()
        try:
            evaluated = evaluator.evaluate(cell, context)
        finally:
            if temporary:
                evaluator.close()
        elapsed_ms = (time.perf_counter() - started) * 1000
        result = (
            evaluated if isinstance(evaluated, EvaluationResult) else EvaluationResult(evaluated)
        )
        if options.label_explicit:
            label = options.label
        elif result.label is not None:
            label = result.label
        else:
            label = options.label
        redactor = getattr(evaluator, "redact_text", None)
        if not callable(redactor):
            redactor = None
        elif label is not None:
            label = redactor(label)
        q_source: Optional[str] = None
        if _session.include_q_source and not temporary:
            q_source = redactor(cell) if redactor is not None else cell
        display_module.display_result(
            result.value,
            columns=result.columns,
            row_count=result.row_count,
            label=label,
            elapsed_ms=elapsed_ms,
            q_source=q_source,
            row_limit=options.row_limit,
            byte_limit=options.byte_limit,
            chart=result.chart,
            redact_text=redactor,
        )

    @line_magic
    def kx(self, line: str) -> None:
        """Manage the evaluator used by ``%%q``."""

        try:
            tokens = shlex.split(line, posix=True)
        except ValueError as error:
            raise UsageError(f"Invalid %kx arguments: {error}") from error
        command = tokens[0].lower() if tokens else "status"
        arguments = tokens[1:]
        if command == "status":
            _no_arguments(command, arguments)
            print(_status())
            return
        if command == "profiles":
            _no_arguments(command, arguments)
            print(_profiles_text())
            return
        if command == "use":
            if len(arguments) != 1:
                raise UsageError("%kx use requires exactly one profile name")
            _activate_profile(arguments[0])
            print(f"Using KX profile {arguments[0]!r}.")
            return
        if command == "connect":
            evaluator = _connect_arguments(arguments)
            previous = _session.evaluator
            _session.evaluator = evaluator
            _session.profile_name = None
            _session.label = None
            _session.include_q_source = False
            if previous is not None and previous is not evaluator:
                previous.close()
            print(f"Connected to q at {evaluator.endpoint}.")
            return
        if command == "disconnect":
            _no_arguments(command, arguments)
            was_active = _session.evaluator is not None
            clear_evaluator()
            print("Disconnected." if was_active else "No active KX evaluator.")
            return
        if command in {"help", "-h", "--help"}:
            _no_arguments(command, arguments)
            print(_help())
            return
        raise UsageError(f"Unknown %kx command {command!r}; use `%kx help` for available commands")


def load_ipython_extension(ipython: Any) -> None:
    """Register ``%%q`` and ``%kx``."""

    ipython.register_magics(KxQMagics)


def unload_ipython_extension(ipython: Any) -> None:
    """Unregister magics and close live resources."""

    manager = getattr(ipython, "magics_manager", None)
    magics = getattr(manager, "magics", {})
    for kind, name in (("cell", "q"), ("line", "kx")):
        registered = magics.get(kind)
        if isinstance(registered, dict):
            registered.pop(name, None)
    clear_evaluator()


def _parse_q_options(line: str) -> _QOptions:
    try:
        tokens = shlex.split(line, posix=True)
    except ValueError as error:
        raise UsageError(f"Invalid %%q options: {error}") from error
    profile: Optional[str] = None
    row_limit = _session.row_limit
    byte_limit = _session.byte_limit
    timeout: Optional[float] = None
    label = _session.label
    label_explicit = False
    seen: set[str] = set()
    index = 0
    supported = {
        "--profile",
        "--max-rows",
        "--max-bytes",
        "--timeout",
        "--label",
    }
    while index < len(tokens):
        option = tokens[index]
        if option not in supported:
            raise UsageError(
                f"Unknown %%q option {option!r}; supported: "
                "--profile, --max-rows, --max-bytes, --timeout, --label"
            )
        if option in seen:
            raise UsageError(f"Duplicate %%q option {option}")
        seen.add(option)
        index += 1
        if index >= len(tokens):
            raise UsageError(f"%%q option {option} requires a value")
        value = tokens[index]
        index += 1
        if option == "--profile":
            profile = value
        elif option == "--label":
            label = value
            label_explicit = True
        elif option == "--timeout":
            timeout = _parse_positive_float(option, value)
        else:
            number = _parse_integer(option, value)
            if option == "--max-rows":
                row_limit = number
            else:
                byte_limit = number
    try:
        build_mime_bundle(
            [],
            columns=["_validation"],
            label=label,
            row_limit=row_limit,
            byte_limit=byte_limit,
        )
    except ValueError as error:
        raise UsageError(f"Invalid %%q options: {error}") from error
    return _QOptions(profile, row_limit, byte_limit, timeout, label, label_explicit)


def _activate_profile(name: str) -> None:
    evaluator = _profile_evaluator(name)
    previous = _session.evaluator
    _session.evaluator = evaluator
    _session.profile_name = name
    _session.label = None
    _session.include_q_source = False
    if previous is not None and previous is not evaluator:
        previous.close()


def _profile_evaluator(name: str) -> Evaluator:
    try:
        profile = load_config().profile(name)
        return _from_profile(profile)
    except ConfigError as error:
        raise UsageError(str(error)) from error


def _from_profile(profile: Profile) -> Evaluator:
    if profile.kind == "direct":
        return DirectQEvaluator(
            profile.host or "",
            profile.port or 0,
            username=profile.username,
            password=resolve_password(profile),
            connect_timeout=profile.connect_timeout,
            query_timeout=profile.query_timeout,
            max_receive_bytes=profile.max_receive_bytes,
        )
    if profile.kind == "broker":
        token = resolve_token(profile)
        if token is None:
            variable = profile.token_env or "(no token_env configured)"
            raise ConfigError(f"broker token is unavailable from {variable}")
        return BrokerEvaluator(profile.base_url or "", token, timeout=profile.timeout)
    return PyKXEvaluator()


def _connect_arguments(arguments: list[str]) -> DirectQEvaluator:
    if not arguments:
        raise UsageError("%kx connect requires HOST:PORT")
    endpoint = arguments[0]
    username = ""
    password_env: Optional[str] = None
    connect_timeout = DEFAULT_CONNECT_TIMEOUT_SECONDS
    query_timeout = DEFAULT_QUERY_TIMEOUT_SECONDS
    seen: set[str] = set()
    index = 1
    supported = {
        "--username",
        "--password-env",
        "--connect-timeout",
        "--query-timeout",
    }
    while index < len(arguments):
        option = arguments[index]
        if option not in supported:
            raise UsageError(f"Unknown %kx connect option {option!r}")
        if option in seen:
            raise UsageError(f"Duplicate %kx connect option {option}")
        seen.add(option)
        index += 1
        if index >= len(arguments):
            raise UsageError(f"%kx connect option {option} requires a value")
        value = arguments[index]
        index += 1
        if option == "--username":
            username = value
        elif option == "--password-env":
            password_env = value
        elif option == "--connect-timeout":
            connect_timeout = _parse_positive_float(option, value)
        else:
            query_timeout = _parse_positive_float(option, value)
    host, port = _endpoint(endpoint)
    try:
        profile = Profile(
            name="runtime",
            host=host,
            port=port,
            username=username,
            password_env=password_env,
            connect_timeout=connect_timeout,
            query_timeout=query_timeout,
        )
        evaluator = DirectQEvaluator(
            host,
            port,
            username=username,
            password=resolve_password(profile),
            connect_timeout=connect_timeout,
            query_timeout=query_timeout,
        )
        evaluator.connect()
        return evaluator
    except (ConfigError, ValueError) as error:
        raise UsageError(str(error)) from error


def _endpoint(value: str) -> tuple[str, int]:
    if value.startswith("["):
        closing = value.find("]")
        if closing < 1 or closing + 1 >= len(value) or value[closing + 1] != ":":
            raise UsageError("IPv6 endpoints must use [ADDRESS]:PORT")
        host, raw_port = value[1:closing], value[closing + 2 :]
    else:
        try:
            host, raw_port = value.rsplit(":", 1)
        except ValueError as error:
            raise UsageError("endpoint must use HOST:PORT") from error
        if ":" in host:
            raise UsageError("IPv6 endpoints must use [ADDRESS]:PORT")
    try:
        port = int(raw_port, 10)
    except ValueError as error:
        raise UsageError("endpoint port must be an integer") from error
    if not host or not 1 <= port <= 65_535:
        raise UsageError("endpoint must contain a host and port from 1 to 65535")
    return host, port


def _status() -> str:
    evaluator = _session.evaluator
    if evaluator is None:
        return "KX evaluator: disconnected"
    if isinstance(evaluator, DirectQEvaluator):
        state = "connected" if evaluator.connected else "not yet connected"
        profile = f" · profile {_session.profile_name}" if _session.profile_name else ""
        return f"KX evaluator: direct IPC · {evaluator.endpoint} · {state}{profile}"
    profile = f" · profile {_session.profile_name}" if _session.profile_name else ""
    return f"KX evaluator: {type(evaluator).__name__}{profile}"


def _profiles_text() -> str:
    try:
        config = load_config()
    except ConfigError as error:
        raise UsageError(str(error)) from error
    if not config.profiles:
        return "No KX profiles configured."
    lines: list[str] = []
    for name, profile in sorted(config.profiles.items()):
        flags: list[str] = []
        if name == config.default_profile:
            flags.append("default")
        if name == _session.profile_name:
            flags.append("active")
        suffix = f" ({', '.join(flags)})" if flags else ""
        target = (
            f"{profile.host}:{profile.port}"
            if profile.kind == "direct"
            else profile.base_url or "in-process"
        )
        lines.append(f"{name}: {profile.kind} · {target}{suffix}")
    return "\n".join(lines)


def _help() -> str:
    return "\n".join(
        (
            "%kx status",
            "%kx profiles",
            "%kx use PROFILE",
            "%kx connect HOST:PORT [--username USER] [--password-env VARIABLE]",
            "  [--connect-timeout SECONDS] [--query-timeout SECONDS]",
            "%kx disconnect",
            "%kx help",
            "",
            "%%q options: --profile NAME --max-rows N --max-bytes N",
            "             --timeout SECONDS --label TEXT",
        )
    )


def _parse_integer(option: str, value: str) -> int:
    try:
        number = int(value, 10)
    except ValueError as error:
        raise UsageError(f"{option} requires a base-10 integer") from error
    if str(number) != value and not (value.startswith("+") and str(number) == value[1:]):
        raise UsageError(f"{option} requires a base-10 integer")
    return number


def _parse_positive_float(option: str, value: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise UsageError(f"{option} requires a number") from error
    if not 0 < number <= 86_400:
        raise UsageError(f"{option} must be greater than 0 and at most 86400")
    return number


def _no_arguments(command: str, arguments: list[str]) -> None:
    if arguments:
        raise UsageError(f"%kx {command} takes no arguments")
