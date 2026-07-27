"""Self-contained HTML and text fallbacks for portable KX notebook results."""

from __future__ import annotations

import html
import math
import unicodedata
from collections.abc import Mapping
from typing import Any


def static_html(payload: Mapping[str, Any]) -> str:
    """Render escaped HTML with no scripts, network requests, or active content."""

    if payload.get("kind") == "qText":
        return _qtext_html(payload)
    columns = payload["schema"]["columns"]
    rows = payload["data"]["rows"]
    result = payload["result"]
    provenance = payload["provenance"]
    parts = [_open(), _heading(provenance)]
    schema = ", ".join(f"{column['name']} ({column['type']})" for column in columns)
    parts.append(
        '<div class="kx-schema"><strong>Schema:</strong> '
        + html.escape(schema or "(no columns)")
        + "</div>"
    )
    parts.append(
        '<div class="kx-meta">Rows: '
        f"{int(result['rowCount'])}; preview: {int(result['previewRowCount'])}</div>"
    )
    parts.extend(_source_and_notices(payload))
    if payload.get("chart"):
        parts.append(_chart(payload))
    parts.append('<div class="kx-table-wrap"><table><thead><tr>')
    parts.extend(f"<th>{html.escape(str(column['name']))}</th>" for column in columns)
    parts.append("</tr></thead><tbody>")
    for row in rows:
        parts.append("<tr>")
        parts.extend(f"<td>{html.escape(_cell_text(cell))}</td>" for cell in row)
        parts.append("</tr>")
    if not rows:
        parts.append(f'<tr><td colspan="{max(1, len(columns))}">(no preview rows)</td></tr>')
    parts.append("</tbody></table></div></div>")
    return "".join(parts)


def static_text(payload: Mapping[str, Any]) -> str:
    """Render a durable terminal-friendly fallback."""

    provenance = payload["provenance"]
    lines = [_heading_text(provenance)]
    if provenance.get("qSource"):
        lines.extend(
            (
                "q source:",
                _terminal_text(str(provenance["qSource"]), preserve_layout=True),
            )
        )
    if payload.get("kind") == "qText":
        lines.extend(_notices(payload["result"]))
        lines.append(_terminal_text(str(payload["data"]["text"]), preserve_layout=True))
        return "\n".join(lines)
    columns = payload["schema"]["columns"]
    result = payload["result"]
    lines.append(
        "Schema: "
        + (
            ", ".join(
                f"{_terminal_text(str(column['name']))} ({_terminal_text(str(column['type']))})"
                for column in columns
            )
            or "(no columns)"
        )
    )
    lines.append(f"Rows: {result['rowCount']}; preview: {result['previewRowCount']}")
    lines.extend(f"Notice: {notice}" for notice in _notices(result))
    chart = payload.get("chart")
    if chart:
        lines.append(
            f"Chart: {_terminal_text(str(chart['type']))}; "
            f"x={_terminal_text(str(chart['xColumn']))}; "
            f"y={_terminal_text(','.join(chart['yColumns']))}"
        )
    lines.append("\t".join(_terminal_text(str(column["name"])) for column in columns))
    for row in payload["data"]["rows"]:
        lines.append("\t".join(_plain_cell(cell) for cell in row))
    if not payload["data"]["rows"]:
        lines.append("(no preview rows)")
    return "\n".join(lines)


def _open() -> str:
    return (
        '<div class="kx-result" data-kx-result-version="1"><style>'
        ".kx-result{font:13px system-ui,sans-serif;color:#202124;max-width:100%}"
        ".kx-result .kx-meta,.kx-result .kx-schema,.kx-result .kx-notice{margin:.3rem 0}"
        ".kx-result pre{overflow:auto;padding:.45rem .6rem;background:#f6f8fa}"
        ".kx-result .kx-notice{padding:.4rem .55rem;border-left:3px solid #b7791f;background:#fff8e1}"
        ".kx-result .kx-table-wrap{overflow:auto;max-height:28rem;border:1px solid #d0d7de}"
        ".kx-result table{border-collapse:collapse;width:max-content;min-width:100%}"
        ".kx-result th,.kx-result td{padding:.25rem .5rem;border-bottom:1px solid #d8dee4;"
        "text-align:left;white-space:pre-wrap;vertical-align:top}"
        ".kx-result th{position:sticky;top:0;background:#f6f8fa;font-weight:600}"
        ".kx-result svg{display:block;max-width:100%;height:auto;margin:.65rem 0;"
        "border:1px solid #d0d7de;background:#fff}</style>"
    )


def _heading(provenance: Mapping[str, Any]) -> str:
    text = "<strong>KX q result</strong>"
    if provenance.get("label"):
        text += " — " + html.escape(str(provenance["label"]))
    if provenance.get("elapsedMs") is not None:
        text += " · " + html.escape(_number(provenance["elapsedMs"])) + " ms"
    return f'<div class="kx-meta">{text}</div>'


def _heading_text(provenance: Mapping[str, Any]) -> str:
    text = "KX q result"
    if provenance.get("label"):
        text += f" — {_terminal_text(str(provenance['label']))}"
    if provenance.get("elapsedMs") is not None:
        text += f" · {_number(provenance['elapsedMs'])} ms"
    return text


def _source_and_notices(payload: Mapping[str, Any]) -> list[str]:
    provenance = payload["provenance"]
    parts: list[str] = []
    if provenance.get("qSource"):
        parts.append(
            "<details><summary>q source</summary><pre>"
            + html.escape(str(provenance["qSource"]))
            + "</pre></details>"
        )
    parts.extend(
        f'<div class="kx-notice">{html.escape(notice)}</div>'
        for notice in _notices(payload["result"])
    )
    return parts


def _qtext_html(payload: Mapping[str, Any]) -> str:
    return "".join(
        [
            _open(),
            _heading(payload["provenance"]),
            *_source_and_notices(payload),
            "<pre>",
            html.escape(str(payload["data"]["text"])),
            "</pre></div>",
        ]
    )


def _notices(result: Mapping[str, Any]) -> list[str]:
    reasons = set(result.get("truncationReasons", ()))
    notices: list[str] = []
    if "rowLimit" in reasons:
        notices.append(
            f"Preview limited to at most {result.get('rowLimit', '?')} rows; "
            "the full result is not embedded in this notebook."
        )
    if "sourcePreview" in reasons:
        notices.append("The source supplied a bounded preview; omitted rows are not embedded.")
    if "byteLimit" in reasons:
        notices.append(
            f"Preview reduced to stay within the {result['byteLimit']}-byte output limit."
        )
    if "cellValueLimit" in reasons:
        notices.append("One or more text values were shortened for output safety.")
    if "columnLimit" in reasons:
        notices.append("Columns were omitted from this bounded preview.")
    if result.get("truncated") and not notices:
        notices.append("This output is a bounded preview, not the full live result.")
    return notices


def _cell_text(cell: Mapping[str, Any]) -> str:
    if cell.get("kind") == "null":
        return "null"
    if cell.get("kind") == "boolean":
        return "true" if cell.get("value") else "false"
    return str(cell.get("value", ""))


def _plain_cell(cell: Mapping[str, Any]) -> str:
    return _terminal_text(_cell_text(cell))


def _number(value: Any) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:.3f}".rstrip("0").rstrip(".")


def _chart(payload: Mapping[str, Any]) -> str:
    chart = payload["chart"]
    if (
        chart["type"] in {"box", "candlestick"}
        or chart.get("groupByColumn")
        or not chart.get("yColumns")
    ):
        return (
            '<div class="kx-notice">This chart selection is persisted for a '
            "compatible interactive KX renderer.</div>"
        )
    columns = [column["name"] for column in payload["schema"]["columns"]]
    try:
        x_index = columns.index(chart["xColumn"])
        y_index = columns.index(chart["yColumns"][0])
    except ValueError:
        return ""
    points: list[tuple[float, float]] = []
    for row in payload["data"]["rows"][:240]:
        x = _numeric_cell(row[x_index])
        y = _numeric_cell(row[y_index])
        if x is not None and y is not None:
            points.append((x, y))
    if not points:
        return '<div class="kx-notice">No numeric preview points for the chart.</div>'
    xs, ys = zip(*points)
    xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)

    def coordinate(value: float, minimum: float, maximum: float, size: float, pad: float) -> float:
        return pad + (0.5 if maximum == minimum else (value - minimum) / (maximum - minimum)) * size

    plotted = [
        (
            coordinate(x, xmin, xmax, 500, 35),
            245 - coordinate(y, ymin, ymax, 200, 10),
        )
        for x, y in points
    ]
    title = html.escape(str(chart.get("title", "")))
    label = f'<text x="35" y="18">{title}</text>' if title else ""
    if chart["type"] == "scatter":
        marks = "".join(
            f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3" fill="#2563eb"/>' for x, y in plotted
        )
    elif chart["type"] == "bar":
        width = max(2.0, min(30.0, 450.0 / len(plotted)))
        marks = "".join(
            f'<rect x="{x - width / 2:.2f}" y="{y:.2f}" width="{width:.2f}" '
            f'height="{max(0.0, 245 - y):.2f}" fill="#2563eb"/>'
            for x, y in plotted
        )
    else:
        coordinates = " ".join(f"{x:.2f},{y:.2f}" for x, y in plotted)
        marks = f'<polyline points="{coordinates}" fill="none" stroke="#2563eb" stroke-width="2"/>'
    return (
        '<svg viewBox="0 0 570 260" role="img" aria-label="KX result chart" '
        'xmlns="http://www.w3.org/2000/svg">' + label + marks + "</svg>"
    )


def _numeric_cell(cell: Mapping[str, Any]) -> float | None:
    if cell.get("kind") != "number":
        return None
    value = float(cell["value"])
    return value if math.isfinite(value) else None


def _terminal_text(value: str, *, preserve_layout: bool = False) -> str:
    parts: list[str] = []
    for character in value:
        if preserve_layout and character in {"\n", "\t"}:
            parts.append(character)
        elif character == "\r":
            parts.append("\\r")
        elif character == "\n":
            parts.append("\\n")
        elif character == "\t":
            parts.append(" ")
        elif unicodedata.category(character).startswith("C"):
            codepoint = ord(character)
            parts.append(f"\\x{codepoint:02x}" if codepoint <= 0xFF else f"\\u{codepoint:04x}")
        else:
            parts.append(character)
    return "".join(parts)
