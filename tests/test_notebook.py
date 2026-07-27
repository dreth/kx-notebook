from __future__ import annotations

from pathlib import Path

import nbformat
import pytest
from nbclient import NotebookClient
from nbformat import NotebookNode

from kx_notebook import MIME_TYPE


@pytest.mark.integration
def test_nbclient_executes_python_and_q_cells_with_portable_mime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("IPYTHONDIR", str(tmp_path / "ipython"))
    notebook = nbformat.v4.new_notebook(
        metadata={
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            }
        }
    )
    notebook.cells = [
        nbformat.v4.new_code_cell("%load_ext kx_notebook"),
        nbformat.v4.new_code_cell(
            "\n".join(
                [
                    "from kx_notebook import configure_evaluator",
                    "calls = []",
                    "def evaluate(source):",
                    "    calls.append(source)",
                    "    return [",
                    "        {'sym': 'AAPL', 'price': 224.1},",
                    "        {'sym': 'MSFT', 'price': 518.0},",
                    "    ]",
                    "configure_evaluator(evaluate)",
                ]
            )
        ),
        nbformat.v4.new_code_cell(
            "%%q --max-rows 1 --max-bytes 20000 --label notebook\nselect from trade"
        ),
        nbformat.v4.new_code_cell(
            "assert [source.rstrip('\\n') for source in calls] == ['select from trade']"
        ),
    ]
    source_path = tmp_path / "portable-q.ipynb"
    executed_path = tmp_path / "portable-q.executed.ipynb"
    nbformat.write(notebook, source_path)

    loaded = nbformat.read(source_path, as_version=4)
    executed = NotebookClient(
        loaded,
        timeout=60,
        kernel_name="python3",
        resources={"metadata": {"path": str(tmp_path)}},
    ).execute()
    nbformat.write(executed, executed_path)

    q_outputs: list[NotebookNode] = executed.cells[2].outputs
    rich = [output for output in q_outputs if output.output_type == "display_data"]
    assert len(rich) == 1
    assert set(rich[0].data) == {MIME_TYPE, "text/html", "text/plain"}
    payload = rich[0].data[MIME_TYPE]
    assert payload["version"] == 1
    assert payload["kind"] == "table"
    assert payload["result"]["rowCount"] == 2
    assert payload["result"]["previewRowCount"] == 1
    assert payload["result"]["truncated"] is True
    assert payload["provenance"]["label"] == "notebook"
    assert "qSource" not in payload["provenance"]
    assert executed_path.is_file()
