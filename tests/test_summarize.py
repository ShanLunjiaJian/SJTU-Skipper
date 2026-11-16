"""Consumer contracts for final note controls and stop policy."""

from types import SimpleNamespace
from plugins.summarize.plugin import SummarizePlugin


def test_stop_policy_is_session_bound():
    calls = []
    plugin = SummarizePlugin()
    plugin.pipeline = SimpleNamespace(start=calls.append)
    plugin.settings = SimpleNamespace(get=lambda key, default: "direct")
    old = SimpleNamespace(meta={})
    plugin.ctx = SimpleNamespace(streams=SimpleNamespace(get_session=lambda sid: old))
    plugin._stopped({"session_id": "old-course"})
    assert calls == ["old-course"]


def test_fill_requests_original_course():
    calls = []
    plugin = SummarizePlugin()
    plugin.pipeline = SimpleNamespace(start=calls.append)
    fill = SimpleNamespace(meta={"fill_for": "original-course"})
    plugin.ctx = SimpleNamespace(streams=SimpleNamespace(get_session=lambda sid: fill))
    plugin._stopped({"session_id": "fill-course"})
    assert calls == ["original-course"]


def test_skip_preserves_preview():
    calls = []
    plugin = SummarizePlugin()
    plugin.pipeline = SimpleNamespace(start=calls.append)
    plugin.settings = SimpleNamespace(get=lambda key, default: "skip")
    plugin.ctx = SimpleNamespace(
        streams=SimpleNamespace(get_session=lambda sid: SimpleNamespace(meta={}))
    )
    plugin._stopped({"session_id": "course"})
    assert calls == []
