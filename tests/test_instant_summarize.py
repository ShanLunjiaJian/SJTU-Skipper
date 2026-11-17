from types import SimpleNamespace
from plugins.instant_summarize.plugin import (
    InstantSummarizeService,
    InstantSummarizePlugin,
)


def test_service_uses_pipeline_files(tmp_path):
    expected = {"knowledge": tmp_path / "instant_note_knowledge.md"}
    pipeline = SimpleNamespace(
        draft_paths=lambda sid: expected,
        note_files=lambda kind: {"knowledge": (f"{kind}.md", "知识")},
        running_tasks=lambda: [],
    )
    service = InstantSummarizeService(pipeline)
    assert service.draft_paths("course") == expected
    assert service.note_files()["knowledge"][0] == "instant.md"


def test_toggle_retains_source_records():
    values = {}
    plugin = InstantSummarizePlugin()
    plugin.settings = SimpleNamespace(
        set=lambda key, value: values.update({key: value})
    )
    plugin.ctx = SimpleNamespace(ui=SimpleNamespace(refresh=lambda event: None))
    assert plugin._toggle({"enabled": False})["ok"]
    assert values["instant-summarize.enabled"] is False
