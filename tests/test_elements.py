from __future__ import annotations

from osintbuddy.compiler import compile_entity
from osintbuddy.elements import Markdown


def test_markdown_element_serializes_for_graph_renderer() -> None:
    element = Markdown(label="Notes", value="# Findings", width=12)

    assert element.to_dict() == {
        "label": "Notes",
        "type": "md",
        "width": 12,
        "value": "# Findings",
    }


def test_compiler_accepts_markdown_json_type() -> None:
    code = compile_entity(
        {
            "label": "Markdown Note",
            "elements": [
                {"type": "markdown", "label": "Notes", "value": "# Findings"}
            ],
        }
    )

    assert "from osintbuddy.elements import Markdown" in code
    assert 'Markdown(label="Notes", value="# Findings")' in code
