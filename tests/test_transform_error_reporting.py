from __future__ import annotations

from pathlib import Path

import pytest

from omoika import Registry
from omoika.errors import PluginError
from omoika.ipc_worker import ObWorker


def _reset_registry() -> None:
    Registry.labels.clear()
    Registry.plugins.clear()
    Registry.ui_labels.clear()
    Registry.transforms_map.clear()


@pytest.mark.asyncio
async def test_worker_reports_stream_transform_source_details(tmp_path: Path):
    plugin_root = tmp_path / "plugins"
    entities_dir = plugin_root / "entities"
    transforms_dir = plugin_root / "transforms"
    entities_dir.mkdir(parents=True)
    transforms_dir.mkdir(parents=True)

    (entities_dir / "failing_entity.py").write_text(
        (
            'from omoika import Plugin\n\n'
            "class FailingEntity(Plugin):\n"
            '    version = "1.0.0"\n'
            '    label = "Failing Entity"\n'
        ),
        encoding="utf-8",
    )
    transform_file = transforms_dir / "broken_stream.py"
    transform_file.write_text(
        (
            "from omoika import transform\n\n"
            '@transform(target="failing_entity@1.0.0", label="Broken Stream")\n'
            "async def broken_stream(entity):\n"
            '    yield {"label": "Transient"}\n'
            '    raise ValueError("boom")\n'
        ),
        encoding="utf-8",
    )

    _reset_registry()
    worker = ObWorker()
    worker.ensure_plugins(str(plugin_root))

    edge_label, result = await worker.run_transform(
        source={
            "entity": {
                "id": "seed-error-1",
                "label": "Failing Entity",
                "transform": "Broken Stream",
                "data": {"label": "Failing Entity"},
            }
        },
        plugins_path=str(plugin_root),
        cfg={},
    )

    assert edge_label == ""

    with pytest.raises(PluginError) as exc_info:
        [item async for item in result]

    error = exc_info.value
    assert error.details["transform_path"] == "plugins.transforms.broken_stream.broken_stream"
    assert error.details["source_path"] == str(transform_file)
    assert isinstance(error.details["line_number"], int)
    assert "boom" in str(error)
    assert "plugins.transforms.broken_stream.broken_stream" in str(error)
    assert str(transform_file) in str(error)
