"""Portable-result contract tests.

The core cases are adapted from the historical vscode-kdb Python package tests.
They intentionally assert the already-consumed v1 MIME shape.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import math
from collections.abc import Iterator

import pytest

from kx_notebook import (
    MIME_TYPE,
    Chart,
    KxNotebookError,
    TableShapeError,
    build_mime_bundle,
)
from kx_notebook.contract import MAX_STRING_CHARS, QText, canonical_payload_bytes


def test_v1_bundle_has_typed_cells_and_portable_fallbacks() -> None:
    output = build_mime_bundle(
        [
            {
                "null": None,
                "flag": True,
                "number": 2.5,
                "big": 2**63 - 1,
                "text": "AAPL",
                "when": dt.datetime(2026, 7, 22, 9, 0, tzinfo=dt.timezone.utc),
                "nested": {"items": [1, "x"]},
            }
        ]
    )

    assert set(output.bundle) == {MIME_TYPE, "text/html", "text/plain"}
    assert MIME_TYPE == "application/vnd.kx.result+json"
    payload = output.bundle[MIME_TYPE]
    assert payload["version"] == 1
    assert payload["kind"] == "table"
    assert payload["data"]["encoding"] == "rows"
    assert [cell["kind"] for cell in payload["data"]["rows"][0]] == [
        "null",
        "boolean",
        "number",
        "bigint",
        "string",
        "temporal",
        "json",
    ]
    assert payload["data"]["rows"][0][3]["value"] == str(2**63 - 1)
    assert payload["provenance"] == {"marker": "%%q"}
    assert payload["result"] == {
        "rowCount": 1,
        "previewRowCount": 1,
        "truncated": False,
        "truncationReasons": [],
        "rowLimit": 20,
        "byteLimit": 1_000_000,
    }
    assert json.loads(canonical_payload_bytes(payload)) == payload


@pytest.mark.parametrize("row_count", [0, 1, 20])
def test_default_preview_keeps_small_tables_complete(row_count: int) -> None:
    output = build_mime_bundle(
        [{"id": index} for index in range(row_count)],
        columns=["id"] if row_count == 0 else None,
    )
    payload = output.bundle[MIME_TYPE]

    assert payload["result"]["rowCount"] == row_count
    assert payload["result"]["previewRowCount"] == row_count
    assert len(payload["data"]["rows"]) == row_count
    assert payload["result"]["truncated"] is False
    assert payload["result"]["truncationReasons"] == []


def test_row_limit_never_consumes_the_unpublished_tail() -> None:
    consumed: list[int] = []

    def rows():
        for index in range(50_000):
            consumed.append(index)
            yield [index, f"row-{index}"]

    output = build_mime_bundle(
        rows(),
        columns=["id", "label"],
        row_count=50_000,
        row_limit=3,
    )
    payload = output.bundle[MIME_TYPE]

    assert consumed == [0, 1, 2]
    assert payload["result"]["rowCount"] == 50_000
    assert payload["result"]["previewRowCount"] == 3
    assert payload["result"]["truncated"] is True
    assert "rowLimit" in payload["result"]["truncationReasons"]
    assert "full result is not embedded" in output.bundle["text/html"]


def test_source_preview_does_not_fabricate_a_complete_result() -> None:
    output = build_mime_bundle([{"id": 1}, {"id": 2}], row_count=20, row_limit=10)
    payload = output.bundle[MIME_TYPE]

    assert payload["result"]["rowCount"] == 20
    assert payload["result"]["previewRowCount"] == 2
    assert payload["result"]["truncated"] is True
    assert "sourcePreview" in payload["result"]["truncationReasons"]
    assert "bounded preview" in output.bundle["text/html"]


@pytest.mark.parametrize("elapsed", [True, "1"])
def test_elapsed_metadata_requires_a_numeric_api_value(elapsed: object) -> None:
    with pytest.raises(KxNotebookError, match="numeric"):
        build_mime_bundle([{"id": 1}], elapsed_ms=elapsed)  # type: ignore[arg-type]


def test_total_mime_byte_limit_reduces_rows_but_retains_schema() -> None:
    rows = [{"id": index, "note": "x" * 500} for index in range(100)]
    output = build_mime_bundle(rows, row_limit=100, byte_limit=16_384)
    payload = output.bundle[MIME_TYPE]

    assert output.body_bytes <= 16_384
    assert payload["result"]["previewRowCount"] < 100
    assert payload["schema"]["columns"] == [
        {"name": "id", "type": "number"},
        {"name": "note", "type": "string"},
    ]
    assert "byteLimit" in payload["result"]["truncationReasons"]
    assert "output limit" in output.bundle["text/plain"]


def test_strings_are_bounded_and_disclosed() -> None:
    output = build_mime_bundle(
        [{"text": "x" * (MAX_STRING_CHARS + 10)}],
        byte_limit=200_000,
    )
    payload = output.bundle[MIME_TYPE]
    value = payload["data"]["rows"][0][0]["value"]

    assert len(value) == MAX_STRING_CHARS
    assert value.endswith("…")
    assert "cellValueLimit" in payload["result"]["truncationReasons"]


def test_qtext_contract_is_versioned_bounded_and_has_safe_fallbacks() -> None:
    hostile = '<script src="https://evil.test/x.js">bad()</script>'
    output = build_mime_bundle(
        QText(hostile + ("x" * 40_000)),
        byte_limit=16_384,
    )
    payload = output.bundle[MIME_TYPE]

    assert output.body_bytes <= 16_384
    assert payload["version"] == 1
    assert payload["kind"] == "qText"
    assert payload["result"]["truncated"] is True
    assert "byteLimit" in payload["result"]["truncationReasons"]
    assert len(payload["data"]["text"]) < len(hostile) + 40_000
    assert hostile not in output.bundle["text/html"]
    assert "<script" not in output.bundle["text/html"].lower()
    assert 'src="https://' not in output.bundle["text/html"].lower()
    assert "preview reduced" in output.bundle["text/plain"].lower()


def test_pretruncated_qtext_never_claims_to_be_complete() -> None:
    output = build_mime_bundle(
        QText(
            "unsupported q function preview",
            truncated=True,
            truncation_reasons=("sourcePreview",),
        )
    )
    payload = output.bundle[MIME_TYPE]

    assert payload["result"]["truncated"] is True
    assert payload["result"]["truncationReasons"] == ["sourcePreview"]
    assert "full" not in output.bundle["text/plain"].lower() or (
        "not the full" in output.bundle["text/plain"].lower()
    )


def test_inferred_row_count_must_fit_the_portable_contract() -> None:
    class OversizedRows:
        def __len__(self) -> int:
            return 1 << 53

        def __iter__(self) -> Iterator[list[int]]:
            while True:
                yield [1]

    with pytest.raises(KxNotebookError, match="row_count"):
        build_mime_bundle(OversizedRows(), columns=["n"])


def test_mapping_rows_cannot_silently_drop_extra_columns() -> None:
    with pytest.raises(TableShapeError, match="keys|columns"):
        build_mime_bundle([{"a": 1}, {"a": 2, "secret": "lost"}])
    with pytest.raises(TableShapeError, match="keys|columns"):
        build_mime_bundle([{}, {"secret": "lost"}])
    with pytest.raises(TableShapeError, match="exactly"):
        build_mime_bundle({"a": [1], "secret": [2]}, columns=["a"])


def test_overlong_columns_are_rejected_instead_of_silently_clipped() -> None:
    with pytest.raises(TableShapeError, match="256"):
        build_mime_bundle([[1]], columns=["x" * 257])


def test_decimal_and_large_binary_cells_are_preserved_or_disclosed() -> None:
    exact = decimal.Decimal("1234567890.12345678901234567890")
    output = build_mime_bundle(
        [{"decimal": exact, "binary": b"x" * 1_000_000}],
        byte_limit=200_000,
    )
    payload = output.bundle[MIME_TYPE]

    assert payload["data"]["rows"][0][0] == {
        "kind": "string",
        "value": str(exact),
    }
    binary = payload["data"]["rows"][0][1]["value"]
    assert len(binary) == MAX_STRING_CHARS
    assert binary.endswith("…")
    assert "cellValueLimit" in payload["result"]["truncationReasons"]


def test_non_sequence_row_is_rejected_without_materializing_its_iterator() -> None:
    class InfiniteRow:
        def __iter__(self) -> Iterator[int]:
            while True:
                yield 1

    with pytest.raises(TableShapeError, match="sequence"):
        build_mime_bundle([InfiniteRow()], columns=["x"])


def test_html_escapes_untrusted_fields_and_loads_no_network_assets() -> None:
    hostile = '<script src="https://evil.test/x.js">alert(1)</script><img onerror="x">'
    output = build_mime_bundle(
        [{hostile: hostile}],
        label=hostile,
        q_source=hostile,
    )
    fallback = output.bundle["text/html"]

    assert hostile not in fallback
    assert "<script" not in fallback.lower()
    assert 'src="https://' not in fallback.lower()
    assert 'onerror="' not in fallback.lower()
    assert "&lt;script" in fallback
    assert output.bundle[MIME_TYPE]["provenance"]["qSource"] == hostile


def test_static_chart_round_trips_and_uses_inline_svg_only() -> None:
    output = build_mime_bundle(
        [
            {"time": 1, "price": 10.0},
            {"time": 2, "price": 11.5},
        ],
        chart=Chart("line", "time", ("price",), title="Price <safe>"),
    )
    payload = output.bundle[MIME_TYPE]

    assert payload["chart"] == {
        "version": 1,
        "visible": True,
        "type": "line",
        "xColumn": "time",
        "yColumns": ["price"],
        "title": "Price <safe>",
    }
    fallback = output.bundle["text/html"]
    assert "<svg" in fallback
    assert "<polyline" in fallback
    assert "Price &lt;safe&gt;" in fallback
    assert "https://" not in fallback
    assert "Chart: line; x=time; y=price" in output.bundle["text/plain"]


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_nonfinite_numbers_are_encoded_as_safe_text(value: float) -> None:
    output = build_mime_bundle([{"value": value}])
    cell = output.bundle[MIME_TYPE]["data"]["rows"][0][0]

    assert cell == {"kind": "string", "value": str(value)}
    canonical_payload_bytes(output.bundle[MIME_TYPE])


def test_invalid_table_and_chart_shapes_are_rejected() -> None:
    with pytest.raises(TableShapeError, match="row_count is required"):
        build_mime_bundle(iter([[1]]), columns=["x"])
    with pytest.raises(TableShapeError, match="duplicate|unique"):
        build_mime_bundle([[1, 2]], columns=["x", "x"])
    with pytest.raises(KxNotebookError, match="unavailable"):
        build_mime_bundle(
            [{"x": 1, "y": 2}],
            chart=Chart("line", "x", ("missing",)),
        )
