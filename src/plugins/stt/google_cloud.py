"""Google Cloud Speech-to-Text **v2** REST adapter.

Only the synchronous ``recognize`` method is used; v2 accepts one audio payload
of at most about one minute, so the plugin feeds fixed-length windows.  Audio is
sent as a WAV container with ``autoDecodingConfig`` so the newer ``chirp_*``
models work as well as the classic ``long``/``short`` models.

v2 rejects API keys, so :class:`GoogleSpeechClient` takes a callable returning
an OAuth access token (see :mod:`plugins.stt.credentials`).  Google's optional
"model adaptation" (uploading phrase sets / custom classes of specialized
terms) is not used.

:class:`GoogleProvider` is the adapter bridge: it reads settings, keeps one
credential/session object per configuration, and returns the same normalized
batch result as the compatible streaming service::

    {"duration_ms": int, "model": str, "language": str,
     "segments": [{"start_ms": int, "end_ms": int, "text": str}, ...]}
"""

from __future__ import annotations

import base64
import io
import logging
import os
import threading
import wave
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx2

from .client import retryable
from .credentials import GoogleTokenProvider
from .errors import SttError

logger = logging.getLogger("lecture_agent.plugin.stt")

DEFAULT_LOCATION = "us"
DEFAULT_MODEL = "chirp_3"
# ``chirp_*`` models are not served from the ``global`` endpoint; they live in
# regional ones.  When a chirp model is paired with ``global`` we retry against
# this region before giving up.
CHIRP_FALLBACK_LOCATION = "us"
# Last-resort model: it exists in every location and supports the languages we
# care about (including cmn-Hans-CN), so transcripts keep flowing even when the
# requested model/region pair is rejected.
FALLBACK_MODEL = "long"
# v2 synchronous Recognize accepts one payload of at most ~60 seconds.
MAX_WINDOW_SECONDS = 55.0

MODEL_OPTIONS: tuple[tuple[str, str], ...] = (
    ("chirp_3", "chirp_3（最新多语言模型，推荐）"),
    ("chirp_2", "chirp_2（上一代多语言模型）"),
    ("long", "long（长音频）"),
    ("short", "short（短音频）"),
    ("telephony", "telephony（电话音频）"),
    ("telephony_short", "telephony_short（电话短音频）"),
)

Settings = Callable[[str, Any], Any]


@dataclass(frozen=True, slots=True)
class SpeechConfig:
    project_id: str
    location: str = DEFAULT_LOCATION
    model: str = DEFAULT_MODEL
    language_code: str = "cmn-Hans-CN"
    punctuation: bool = True
    timeout: float = 60.0


@dataclass(frozen=True, slots=True)
class SpeechSegment:
    """One recognized span, with offsets relative to the audio payload."""

    text: str
    start: float
    end: float
    confidence: float = 0.0
    language_code: str = ""


class GoogleSpeechClient:
    """Thin wrapper around v2 ``recognizers.recognize``."""

    def __init__(
        self,
        config: SpeechConfig,
        access_token: Callable[[], str],
        *,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self._config = config
        self._access_token = access_token
        self._used_config: SpeechConfig = config
        self._client = httpx2.Client(
            timeout=max(1.0, float(config.timeout)),
            transport=transport,
        )

    @property
    def used_config(self) -> SpeechConfig:
        """The model/location actually accepted by the last request."""

        return self._used_config

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GoogleSpeechClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # URLs
    # ------------------------------------------------------------------
    @property
    def location(self) -> str:
        return self._config.location.strip() or DEFAULT_LOCATION

    def endpoint_base(self, location: str | None = None) -> str:
        location = (location or self.location).strip().lower() or DEFAULT_LOCATION
        if location == "global":
            return "https://speech.googleapis.com"
        return f"https://{location}-speech.googleapis.com"

    def recognize_url(self, config: SpeechConfig | None = None) -> str:
        config = config or self._config
        project = config.project_id.strip()
        if not project:
            raise SttError("缺少 Google Cloud project ID（凭据里没有或设置项为空）")
        location = config.location.strip() or DEFAULT_LOCATION
        return (
            f"{self.endpoint_base(location)}/v2/projects/{project}"
            f"/locations/{location}/recognizers/_:recognize"
        )

    def _candidates(self) -> list[SpeechConfig]:
        """Configurations to try, in order.

        Google rejects a model that is not served in the configured location
        with HTTP 400.  ``chirp_*`` models are regional, so a stored
        ``global`` location silently breaks transcription.  Retry with a
        regional endpoint and finally with the always-available ``long``
        model so audio is still transcribed.
        """

        config = self._config
        model = config.model.strip() or DEFAULT_MODEL
        location = config.location.strip() or DEFAULT_LOCATION
        out: list[SpeechConfig] = [replace(config, model=model, location=location)]
        if model.lower().startswith("chirp") and location.lower() == "global":
            out.append(replace(config, model=model, location=CHIRP_FALLBACK_LOCATION))
        if model.lower() != FALLBACK_MODEL:
            out.append(replace(config, model=FALLBACK_MODEL, location=location))
        seen: set[tuple[str, str]] = set()
        unique: list[SpeechConfig] = []
        for candidate in out:
            key = (candidate.location.lower(), candidate.model.lower())
            if key in seen:
                continue
            seen.add(key)
            unique.append(candidate)
        return unique

    # ------------------------------------------------------------------
    # Request
    # ------------------------------------------------------------------
    def recognize(
        self,
        wav: bytes,
        *,
        duration: float | None = None,
    ) -> list[SpeechSegment]:
        """Transcribe a WAV container."""

        if not wav:
            return []
        token = str(self._access_token() or "").strip()
        if not token:
            raise SttError("未获取到 Google OAuth token")

        candidates = self._candidates()
        last_error: SttError | None = None
        for index, config in enumerate(candidates):
            try:
                data = self._post(config, token, wav)
            except SttError as exc:
                if not _is_model_location_error(str(exc)) or index + 1 >= len(
                    candidates
                ):
                    raise
                last_error = exc
                following = candidates[index + 1]
                logger.warning(
                    "STT %s@%s unavailable (%s); retrying with %s@%s",
                    config.model,
                    config.location,
                    exc,
                    following.model,
                    following.location,
                )
                continue
            self._used_config = config
            return _parse_results(data, duration)
        if last_error is not None:  # pragma: no cover - loop always raises
            raise last_error
        return []

    def _post(self, config: SpeechConfig, token: str, wav: bytes) -> Any:
        payload = {
            "config": {
                "autoDecodingConfig": {},
                "languageCodes": [config.language_code],
                "model": config.model,
                "features": {
                    "enableAutomaticPunctuation": bool(config.punctuation),
                },
            },
            "content": base64.b64encode(wav).decode("ascii"),
        }
        try:
            response = self._client.post(
                self.recognize_url(config),
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
        except httpx2.TransportError as exc:
            raise SttError(
                f"请求 Google STT v2 失败：{exc}", connection_failed=True
            ) from exc
        except httpx2.HTTPError as exc:
            raise SttError(f"请求 Google STT v2 失败：{exc}") from exc

        data = _parse_json(response)
        if response.status_code >= 400:
            raise SttError(
                _error_message(response.status_code, data),
                status_code=response.status_code,
            )
        return data


class GoogleProvider:
    """Adapter between the STT core and Google Cloud Speech-to-Text v2."""

    name = "google"
    label = "Google Cloud Speech-to-Text v2"
    supports_stream = False
    max_window = MAX_WINDOW_SECONDS

    def __init__(
        self,
        get: Settings,
        *,
        timeout: float = 60.0,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self._get = get
        self._timeout = max(1.0, float(timeout))
        self._transport = transport
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._auth: GoogleTokenProvider | None = None
        self._auth_signature: tuple[Any, ...] | None = None
        self._client: GoogleSpeechClient | None = None
        self._client_signature: tuple[Any, ...] | None = None

    # ------------------------------------------------------------------
    # Provider interface
    # ------------------------------------------------------------------
    @staticmethod
    def configured(get: Settings) -> bool:
        """Cheap local check: are credentials available without a network call?"""

        for key in ("stt.google.credentials_json", "stt.google.credentials_file"):
            if str(get(key, "") or "").strip():
                return True
        return _default_adc_path().is_file()

    @classmethod
    def describe(cls, get: Settings) -> dict[str, Any]:
        project = str(get("stt.google.project_id", "") or "").strip()
        location = (
            str(get("stt.google.location", DEFAULT_LOCATION) or "").strip()
            or DEFAULT_LOCATION
        )
        model = (
            str(get("stt.google.model", DEFAULT_MODEL) or "").strip() or DEFAULT_MODEL
        )
        language = (
            str(get("stt.google.language_code", "cmn-Hans-CN") or "").strip()
            or "cmn-Hans-CN"
        )
        return {
            "endpoint": f"{project or '(凭据中的项目)'} @ {location}",
            "model": model,
            "language": language,
            "missing": (
                "Google 凭据未配置：请填写服务账号 JSON / 凭据文件，"
                "或设置 GOOGLE_APPLICATION_CREDENTIALS"
            ),
        }

    def metadata(self, context: str = "") -> dict[str, str]:
        language = str(
            self._get("stt.google.language_code", "cmn-Hans-CN") or ""
        ).strip()
        return {"language": language or "cmn-Hans-CN", "context": ""}

    @classmethod
    def create(
        cls, get: Settings, *, timeout: float = 60.0, transport: Any = None
    ) -> GoogleProvider:
        return cls(get, timeout=timeout, transport=transport)

    def transcribe(self, wav: bytes, *, language: str, context: str) -> dict[str, Any]:
        duration = wav_duration(wav)
        client = self._speech_client(language)
        attempts = 0
        while True:
            try:
                segments = client.recognize(wav, duration=duration)
                break
            except SttError as exc:
                attempts += 1
                if attempts >= 3 or not retryable(exc):
                    raise
                delay = 0.25 * 2 ** (attempts - 1)
                if self._cancel.wait(delay):
                    raise SttError("The STT request was cancelled.") from exc
        used = client.used_config
        return {
            "duration_ms": round(duration * 1000),
            "model": used.model,
            "language": used.language_code,
            "segments": [
                {
                    "start_ms": round(segment.start * 1000),
                    "end_ms": round(segment.end * 1000),
                    "text": segment.text,
                }
                for segment in segments
                if segment.text
            ],
        }

    def close(self) -> None:
        self._cancel.set()
        with self._lock:
            client, auth = self._client, self._auth
            self._client = None
            self._auth = None
            self._client_signature = None
            self._auth_signature = None
        if client is not None:
            client.close()
        if auth is not None:
            auth.close()

    # ------------------------------------------------------------------
    # Client construction
    # ------------------------------------------------------------------
    def _speech_client(self, language: str) -> GoogleSpeechClient:
        json_text = str(self._get("stt.google.credentials_json", "") or "").strip()
        file_path = str(self._get("stt.google.credentials_file", "") or "").strip()
        project = str(self._get("stt.google.project_id", "") or "").strip()
        location = (
            str(self._get("stt.google.location", DEFAULT_LOCATION) or "").strip()
            or DEFAULT_LOCATION
        )
        model = (
            str(self._get("stt.google.model", DEFAULT_MODEL) or "").strip()
            or DEFAULT_MODEL
        )
        language = str(language or "").strip() or "cmn-Hans-CN"
        punctuation = bool(self._get("stt.google.enable_punctuation", True))
        timeout = self._timeout

        auth_signature = (json_text, file_path, project, round(timeout, 3))
        with self._lock:
            if self._auth is None or self._auth_signature != auth_signature:
                old = self._auth
                self._auth = None
                self._auth_signature = None
                self._auth = GoogleTokenProvider(
                    credentials_json=json_text,
                    credentials_file=file_path,
                    project_id=project,
                    timeout=timeout,
                    transport=self._transport,
                )
                self._auth_signature = auth_signature
                if old is not None:
                    old.close()

            resolved = project or self._auth.project_id
            config = SpeechConfig(
                project_id=resolved,
                location=location,
                model=model,
                language_code=language,
                punctuation=punctuation,
                timeout=timeout,
            )
            client_signature = (
                auth_signature,
                config.project_id,
                config.location.lower(),
                config.model,
                config.language_code,
                config.punctuation,
            )
            if self._client is None or self._client_signature != client_signature:
                old_client = self._client
                self._client = None
                self._client_signature = None
                self._client = GoogleSpeechClient(
                    config,
                    self._auth.token,
                    transport=self._transport,
                )
                self._client_signature = client_signature
                if old_client is not None:
                    old_client.close()
            return self._client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def wav_duration(wav: bytes) -> float:
    """Return the payload duration encoded in a WAV container."""

    try:
        with wave.open(io.BytesIO(wav), "rb") as handle:
            rate = handle.getframerate()
            frames = handle.getnframes()
    except (wave.Error, EOFError, OSError) as exc:
        raise SttError("The STT payload is not a valid WAV container.") from exc
    if rate <= 0:
        raise SttError("The STT payload has an invalid sample rate.")
    return frames / float(rate)


def _default_adc_path() -> Path:
    override = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if override:
        return Path(override).expanduser()
    config_dir = os.environ.get("CLOUDSDK_CONFIG", "").strip()
    base = (
        Path(config_dir).expanduser()
        if config_dir
        else Path.home() / ".config" / "gcloud"
    )
    return base / "application_default_credentials.json"


def _parse_json(response: httpx2.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise SttError(
            f"Google STT v2 返回了非 JSON 响应（HTTP {response.status_code}）"
        ) from exc


def _is_model_location_error(text: str) -> bool:
    """True when Google rejected the model/region combination."""

    message = str(text).lower()
    markers = (
        "does not exist in the location",
        "not supported in the location",
        "is not available in",
        "unsupported model",
        "unknown model",
    )
    return any(marker in message for marker in markers)


def _error_message(status: int, data: Any) -> str:
    message = ""
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "")
        elif error is not None:
            message = str(error)
    if not message:
        message = "未知错误"
    hint = ""
    if status == 401:
        hint = "；请检查服务账号 / OAuth 凭据是否有效且未过期"
    elif status == 403:
        hint = (
            "；请确认已启用 Speech-to-Text API，且账号有 "
            "speech.recognizers.recognize 权限"
        )
    elif status == 404:
        hint = "；请确认 project ID / location / recognizer 是否正确"
    elif status == 429:
        hint = "；配额或速率超限"
    elif status == 400 and _is_model_location_error(message):
        hint = (
            "；该模型在当前区域不可用（chirp_* 不支持 global），"
            "请把区域改成 us 或 asia-southeast1 等区域"
        )
    return f"Google STT v2 HTTP {status}：{message}{hint}"


def _parse_duration(value: Any) -> float:
    """Parse a protobuf duration string such as ``"3.500s"``."""

    if value is None:
        return 0.0
    text = str(value).strip()
    if not text:
        return 0.0
    text = text.removesuffix("s")
    try:
        return max(0.0, float(text))
    except ValueError:
        return 0.0


def _parse_results(data: Any, duration: float | None) -> list[SpeechSegment]:
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return []
    chunk_end = float(duration) if duration and duration > 0 else 0.0
    segments: list[SpeechSegment] = []
    previous_end = 0.0
    for result in results:
        if not isinstance(result, dict):
            continue
        # v2 uses resultEndOffset; keep resultEndTime as a defensive fallback.
        end = (
            _parse_duration(
                result.get("resultEndOffset") or result.get("resultEndTime")
            )
            or chunk_end
        )
        end = max(end, previous_end)
        start = previous_end
        previous_end = end
        alternatives = result.get("alternatives")
        if not isinstance(alternatives, list) or not alternatives:
            continue
        top = alternatives[0]
        if not isinstance(top, dict):
            continue
        text = str(top.get("transcript") or "").strip()
        if not text:
            continue
        try:
            confidence = float(top.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        segments.append(
            SpeechSegment(
                text=text,
                start=start,
                end=end,
                confidence=confidence,
                language_code=str(result.get("languageCode") or ""),
            )
        )
    return segments


__all__ = [
    "DEFAULT_LOCATION",
    "DEFAULT_MODEL",
    "MAX_WINDOW_SECONDS",
    "MODEL_OPTIONS",
    "GoogleProvider",
    "GoogleSpeechClient",
    "SpeechConfig",
    "SpeechSegment",
    "SttError",
    "wav_duration",
]
