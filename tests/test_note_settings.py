from types import SimpleNamespace

from core.ui import UIManager
from plugins.notes_pipeline.plugin import NotesPipelinePlugin
from plugins.settings.store import SettingsService, SettingsStore


def test_pipeline_registers_generation_settings(tmp_path):
    settings = SettingsService(SettingsStore(tmp_path / "settings.json"))
    services = {"settings": settings, "agent": SimpleNamespace()}
    ctx = SimpleNamespace(
        data_dir=tmp_path,
        require=services.__getitem__,
        register_service=services.__setitem__,
        events=SimpleNamespace(on=lambda *args, **kwargs: None),
        ui=UIManager(),
    )
    pipeline = NotesPipelinePlugin()
    pipeline.setup(ctx)
    try:
        settings.set("instant-summarize.enabled", False)
        settings.set("summarize.review_max_rounds", 3)
        assert pipeline.setting("instant-summarize.enabled", True) is False
        assert pipeline.setting("summarize.review_max_rounds", 0) == 3
    finally:
        pipeline.teardown()
