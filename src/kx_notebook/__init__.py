"""Standalone q execution and portable KX results for IPython/Jupyter.

Public objects are loaded lazily so config and hook CLI commands do not import
IPython or optional evaluator dependencies during package initialization.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__version__ = "0.1.1"

_EXPORTS: dict[str, tuple[str, str]] = {
    "Config": (".config", "Config"),
    "ConfigError": (".config", "ConfigError"),
    "Profile": (".config", "Profile"),
    "config_path": (".config", "config_path"),
    "load_config": (".config", "load_config"),
    "resolve_password": (".config", "resolve_password"),
    "save_config": (".config", "save_config"),
    "CONTRACT_VERSION": (".contract", "CONTRACT_VERSION"),
    "DEFAULT_BYTE_LIMIT": (".contract", "DEFAULT_BYTE_LIMIT"),
    "DEFAULT_ROW_LIMIT": (".contract", "DEFAULT_ROW_LIMIT"),
    "MIME_TYPE": (".contract", "MIME_TYPE"),
    "Chart": (".contract", "Chart"),
    "EvaluationResult": (".contract", "EvaluationResult"),
    "KxNotebookError": (".contract", "KxNotebookError"),
    "OutputLimitError": (".contract", "OutputLimitError"),
    "PortableOutput": (".contract", "PortableOutput"),
    "QText": (".contract", "QText"),
    "TableShapeError": (".contract", "TableShapeError"),
    "build_mime_bundle": (".contract", "build_mime_bundle"),
    "display_result": (".display", "display_result"),
    "BrokerEvaluator": (".evaluators", "BrokerEvaluator"),
    "CallbackEvaluator": (".evaluators", "CallbackEvaluator"),
    "DirectQEvaluator": (".evaluators", "DirectQEvaluator"),
    "EvaluationContext": (".evaluators", "EvaluationContext"),
    "Evaluator": (".evaluators", "Evaluator"),
    "EvaluatorError": (".evaluators", "EvaluatorError"),
    "PyKXEvaluator": (".evaluators", "PyKXEvaluator"),
    "QCancelledError": (".ipc", "QCancelledError"),
    "QChar": (".ipc", "QChar"),
    "QCharVector": (".ipc", "QCharVector"),
    "QConnection": (".ipc", "QConnection"),
    "QDictionary": (".ipc", "QDictionary"),
    "QError": (".ipc", "QError"),
    "QIpcError": (".ipc", "QIpcError"),
    "QKeyedTable": (".ipc", "QKeyedTable"),
    "QSymbol": (".ipc", "QSymbol"),
    "QTable": (".ipc", "QTable"),
    "QTemporal": (".ipc", "QTemporal"),
    "QTimeoutError": (".ipc", "QTimeoutError"),
    "clear_evaluator": (".magic", "clear_evaluator"),
    "configure_evaluator": (".magic", "configure_evaluator"),
    "load_ipython_extension": (".magic", "load_ipython_extension"),
    "unload_ipython_extension": (".magic", "unload_ipython_extension"),
    "FixtureEvaluator": (".testing", "FixtureEvaluator"),
}

__all__ = [*_EXPORTS, "__version__"]


def __getattr__(name: str) -> Any:
    """Resolve and cache one public export on first use."""

    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
