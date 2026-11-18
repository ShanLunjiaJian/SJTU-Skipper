from __future__ import annotations

import io
import json
import time
import wave
from pathlib import Path
from typing import Any

import httpx2
import pytest

from config import load_config
from core.media import MediaKind
from core.runtime import build_runtime
from core.ui import BlockType
from plugins.settings import SettingsPlugin
from plugins.stt import google_cloud
from plugins.stt.credentials import GoogleTokenProvider
from plugins.stt.errors import SttError
from plugins.stt.google_cloud import GoogleProvider, wav_duration
from plugins.stt.plugin import STTPlugin
from tests.test_stt import make_ctx, wait_until


def getter(values: dict[str, Any]):
    def get(key: str, default: Any = None) -> Any:
        return values.get(key, default)

    return get


def make_wav(seconds: float, *, rate: int = 16000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))
    return buffer.getvalue()


class FakeAuth:
    def __init__(self, **_kwargs: Any) -> None:
        self.closed = False

    @property
    def project_id(self) -> str:
        return "proj"

    def token(self) -> str:
        return "token"

    def close(self) -> None:
        self.closed = True


class BatchClient:
    """Minimal batch-only provider used to exercise the core window worker."""

    supports_stream = False
    max_window = 1.0

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def metadata(self, context: str = "") -> dict[str, str]:
        return {"language": "Chinese", "context": context}

    def transcribe(self, wav: bytes, *, language: str, context: str) -> dict[str, Any]:
        duration = wav_duration(wav)
        self.calls.append(
            {"duration": duration, "language": language, "context": context}
        )
        return {
            "duration_ms": round(duration * 1000),
            "model": "batch-model",
            "language": "Chinese",
            "segments": [
                {
                    "start_ms": 0,
                    "end_ms": round(duration * 1000),
                    "text": f"段{len(self.calls)}",
                }
            ],
        }

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def app(tmp_path: Path):
    ctx = make_ctx(tmp_path)
    plugin = STTPlugin()
    plugin.setup(ctx)
    try:
        yield plugin, ctx
    finally:
        plugin.teardown()
        ctx.events.stop()


# ---------------------------------------------------------------------------
# Google adapter
# ---------------------------------------------------------------------------
def test_google_provider_returns_canonical_batch_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            200,
            json={
                "results": [
                    {
                        "resultEndOffset": "0.800s",
                        "languageCode": "cmn-Hans-CN",
                        "alternatives": [{"transcript": "第一段", "confidence": 0.8}],
                    },
                    {
                        "resultEndOffset": "1.500s",
                        "languageCode": "cmn-Hans-CN",
                        "alternatives": [{"transcript": "第二段"}],
                    },
                ]
            },
        )

    values = {
        "stt.google.credentials_json": "{}",
        "stt.google.project_id": "proj",
        "stt.google.location": "us",
        "stt.google.model": "chirp_3",
        "stt.google.language_code": "cmn-Hans-CN",
        "stt.google.enable_punctuation": True,
    }
    monkeypatch.setattr(google_cloud, "GoogleTokenProvider", FakeAuth)
    provider = GoogleProvider(getter(values), transport=httpx2.MockTransport(handler))
    try:
        result = provider.transcribe(make_wav(1.5), language="cmn-Hans-CN", context="")
    finally:
        provider.close()

    assert result == {
        "duration_ms": 1500,
        "model": "chirp_3",
        "language": "cmn-Hans-CN",
        "segments": [
            {"start_ms": 0, "end_ms": 800, "text": "第一段"},
            {"start_ms": 800, "end_ms": 1500, "text": "第二段"},
        ],
    }
    request = seen[0]
    assert str(request.url) == (
        "https://us-speech.googleapis.com/v2/projects/proj"
        "/locations/us/recognizers/_:recognize"
    )
    assert request.headers["authorization"] == "Bearer token"
    payload = json.loads(request.content)
    assert payload["config"]["autoDecodingConfig"] == {}
    assert payload["config"]["languageCodes"] == ["cmn-Hans-CN"]
    assert payload["config"]["model"] == "chirp_3"
    assert payload["config"]["features"]["enableAutomaticPunctuation"] is True
    assert payload["content"]


def test_google_provider_falls_back_to_regional_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    urls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        urls.append(str(request.url))
        if len(urls) == 1:
            return httpx2.Response(
                400,
                json={
                    "error": {
                        "message": (
                            "Model chirp_3 does not exist in the location global."
                        )
                    }
                },
            )
        return httpx2.Response(
            200,
            json={
                "results": [
                    {
                        "resultEndOffset": "1s",
                        "alternatives": [{"transcript": "降级成功"}],
                    }
                ]
            },
        )

    values = {
        "stt.google.credentials_json": "{}",
        "stt.google.project_id": "proj",
        "stt.google.location": "global",
        "stt.google.model": "chirp_3",
    }
    monkeypatch.setattr(google_cloud, "GoogleTokenProvider", FakeAuth)
    provider = GoogleProvider(getter(values), transport=httpx2.MockTransport(handler))
    try:
        result = provider.transcribe(make_wav(1.0), language="cmn-Hans-CN", context="")
    finally:
        provider.close()

    assert len(urls) == 2
    assert "speech.googleapis.com/v2/projects/proj/locations/global" in urls[0]
    assert "us-speech.googleapis.com/v2/projects/proj/locations/us" in urls[1]
    assert result["model"] == "chirp_3"
    assert result["segments"][0]["text"] == "降级成功"


def test_google_provider_rejects_non_wav_payload() -> None:
    provider = GoogleProvider(getter({"stt.google.project_id": "proj"}))
    try:
        with pytest.raises(SttError):
            provider.transcribe(b"not a wav", language="cmn-Hans-CN", context="")
    finally:
        provider.close()


def test_google_provider_configuration_is_local(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        google_cloud, "_default_adc_path", lambda: tmp_path / "missing.json"
    )
    assert GoogleProvider.configured(getter({})) is False
    assert (
        GoogleProvider.configured(getter({"stt.google.credentials_json": "{}"})) is True
    )
    assert (
        GoogleProvider.configured(
            getter({"stt.google.credentials_file": str(tmp_path / "sa.json")})
        )
        is True
    )

    adc = tmp_path / "adc.json"
    adc.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(google_cloud, "_default_adc_path", lambda: adc)
    assert GoogleProvider.configured(getter({})) is True


def test_google_token_provider_refreshes_authorized_user() -> None:
    urls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        urls.append(str(request.url))
        return httpx2.Response(
            200,
            json={
                "access_token": "tok",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )

    info = {
        "type": "authorized_user",
        "client_id": "cid",
        "client_secret": "secret",
        "refresh_token": "refresh",
    }
    provider = GoogleTokenProvider(
        credentials_json=json.dumps(info),
        transport=httpx2.MockTransport(handler),
    )
    try:
        assert provider.token() == "tok"
    finally:
        provider.close()
    assert urls == ["https://oauth2.googleapis.com/token"]


# ---------------------------------------------------------------------------
# Core batch worker
# ---------------------------------------------------------------------------
def test_batch_provider_transcribes_windows_and_tail(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, ctx = app
    client = BatchClient()
    monkeypatch.setattr(plugin, "_new_client", lambda: client)
    session = ctx.streams.current
    session.meta["course_name"] = "微积分"
    session.audio.append(b"\x00\x00" * int(16000 * 2.5))
    session.input_done = True
    ctx.service("settings").set_many({"stt.enabled": True, "stt.window_seconds": 1.0})
    wait_until(lambda: len(session.transcript) == 3 and not plugin.busy())

    assert [round(call["duration"], 3) for call in client.calls] == [1.0, 1.0, 0.5]
    assert all(call["language"] == "Chinese" for call in client.calls)
    assert all(call["context"] == "这是一堂微积分课程。" for call in client.calls)
    assert [item.text for item in session.transcript] == ["段1", "段2", "段3"]
    status = plugin.status()
    assert status["provider"] == "compatible"
    assert status["supports_stream"] is True
    assert status["scheduled_cursor"] == pytest.approx(2.5)
    assert status["covered_seconds"] == pytest.approx(2.5)
    assert status["cursor"] == pytest.approx(2.5)
    assert client.closed is True

    log = ctx.data_dir / "sessions" / session.session_id / "transcript.jsonl"
    assert [json.loads(line)["text"] for line in log.read_text().splitlines()] == [
        "段1",
        "段2",
        "段3",
    ]


def test_batch_worker_waits_for_a_full_window(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, ctx = app
    client = BatchClient()
    monkeypatch.setattr(plugin, "_new_client", lambda: client)
    session = ctx.streams.current
    ctx.service("settings").set_many({"stt.enabled": True, "stt.window_seconds": 1.0})
    session.audio.append(b"\x00\x00" * int(16000 * 0.9))
    time.sleep(0.2)
    assert client.calls == []

    session.audio.append(b"\x00\x00" * int(16000 * 0.1))
    wait_until(lambda: len(client.calls) == 1)
    assert client.calls[0]["duration"] == pytest.approx(1.0)

    session.audio.append(b"\x00\x00" * int(16000 * 0.4))
    session.input_done = True
    wait_until(lambda: len(session.transcript) == 2 and not plugin.busy())
    assert client.calls[1]["duration"] == pytest.approx(0.4)
    assert plugin.status()["scheduled_cursor"] == pytest.approx(1.4)


def test_google_provider_without_credentials_keeps_audio(
    app: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plugin, ctx = app
    monkeypatch.setattr(
        google_cloud, "_default_adc_path", lambda: tmp_path / "missing.json"
    )
    session = ctx.streams.current
    session.audio.append(b"\x00\x00" * 16000)
    session.input_done = True
    settings = ctx.service("settings")
    settings.set("stt.provider", "google")
    settings.set("stt.enabled", True)
    deadline = time.monotonic() + 3
    while plugin.status()["configured"]:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    time.sleep(0.2)
    status = plugin.status()
    assert status["provider"] == "google"
    assert status["provider_label"] == "Google Cloud Speech-to-Text v2"
    assert status["supports_stream"] is False
    assert status["state"] == "error"
    assert "Google 凭据未配置" in status["error"]
    assert status["scheduled_cursor"] == 0
    assert session.transcript.items_since(0) == []
    assert plugin._coverage(session).covered_seconds(MediaKind.AUDIO) == 0


def test_provider_settings_are_visible_per_provider(tmp_path: Path) -> None:
    settings_plugin = SettingsPlugin()
    runtime = build_runtime(
        load_config(data_dir=tmp_path),
        plugins=[settings_plugin, STTPlugin()],
    )
    runtime.start()

    def stt_fields() -> dict[str, dict[str, Any]]:
        tabs = settings_plugin._render_page()[0]["tabs"]
        tab = next(tab for tab in tabs if tab["title"] == "STT")
        return {
            field["name"]: field
            for block in tab["blocks"]
            if block["type"] == BlockType.FORM
            for field in block["fields"]
        }

    try:
        fields = stt_fields()
        names = set(fields)
        assert "stt.provider" in names
        assert "stt.server_url" in names
        assert "stt.google.model" not in names
        assert fields["stt.server_url"]["visible_when"] == {
            "key": "stt.provider",
            "values": ["compatible"],
        }
        assert "visible_when" not in fields["stt.window_seconds"]

        runtime.services.require("settings").set("stt.provider", "google")
        fields = stt_fields()
        names = set(fields)
        assert "stt.google.model" in names
        assert "stt.google.credentials_json" in names
        assert "stt.google.language_code" in names
        assert "stt.server_url" not in names
        assert "stt.context" not in names
        assert fields["stt.google.model"]["visible_when"] == {
            "key": "stt.provider",
            "values": ["google"],
        }
    finally:
        runtime.stop()
