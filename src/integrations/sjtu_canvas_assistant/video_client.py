"""SJTU Canvas video/LTI client.

The public CLI still authenticates with Canvas Access Token only. For video
data, Canvas first creates a sessionless LTI launch URL. The launch exchanges
through SJTU's video service and yields a short-lived video API token.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse

import httpx2
from bs4 import BeautifulSoup
from pydantic import ValidationError

from .exceptions import VideoAuthError, VideoIntegrationError, VideoNetworkError
from .models import (
    CanvasVideo,
    VideoAuth,
    VideoInfo,
)

VIDEO_SERVICE_ROOT = "https://v.sjtu.edu.cn/jy-application-canvas-sjtu"
VIDEO_UI_REFERER = "https://v.sjtu.edu.cn/jy-application-canvas-sjtu-ui/"
VIDEO_LTI_TOOL_ID = 8329
VIDEO_LTI_FORM_ACTION = f"{VIDEO_SERVICE_ROOT}/oidc/login_initiations"
VIDEO_LTI_AUTH_ACTION = f"{VIDEO_SERVICE_ROOT}/lti3/lti3Auth/ivs"
VIDEO_TOKEN_BY_ID_URL = f"{VIDEO_SERVICE_ROOT}/lti3/getAccessTokenByTokenId"
VIDEO_LIST_URL = f"{VIDEO_SERVICE_ROOT}/directOnDemandPlay/findVodVideoList"
VIDEO_INFO_URL = f"{VIDEO_SERVICE_ROOT}/directOnDemandPlay/getVodVideoInfos"

NEW_VIDEO_SERVICE_ROOT = "https://v.sjtu.edu.cn/jy-application-resourcemanage"
NEW_VIDEO_UI_REFERER = "https://v.sjtu.edu.cn/jy-application-resourcemanage-ui/"
NEW_VIDEO_LAUNCH_CONTEXT_URL = f"{NEW_VIDEO_SERVICE_ROOT}/lms/launch-context"
NEW_VIDEO_LIST_URL = f"{NEW_VIDEO_SERVICE_ROOT}/v1/subject_vod_list_new"
NEW_VIDEO_URLS_URL = f"{NEW_VIDEO_SERVICE_ROOT}/v1/course_vod_urls_new"

_SUCCESS_CODES = {"0", "200", "success", "true", "ok"}


@dataclass(frozen=True)
class FormPost:
    action: str
    data: Dict[str, str]


def _redact_sensitive_text(text: str) -> str:
    patterns = [
        (r"(session_token=)[^&\s\"']+", r"\1<redacted>"),
        (r"(tokenId=)[^&\s\"']+", r"\1<redacted>"),
        (r"([?&]token=)[^&\s\"']+", r"\1<redacted>"),
        (
            r"([?&](?:access_token|auth_key|sign|signature|secret|key|expires|policy|wsSecret|wsTime|X-Amz-[^=]+)=)[^&\s\"']+",
            r"\1<redacted>",
        ),
        (r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1<redacted>"),
        (r"(\"token\"\s*:\s*\")[^\"]+", r"\1<redacted>"),
    ]
    redacted = text
    for pattern, replacement in patterns:
        redacted = re.sub(pattern, replacement, redacted, flags=re.IGNORECASE)
    return redacted


def _safe_url_label(url: object) -> str:
    parsed = urlparse(str(url))
    if not parsed.scheme or not parsed.netloc:
        return str(url).split("?", 1)[0]
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


class SJTUVideoClient:
    """Client for SJTU's Canvas video integration."""

    def __init__(
        self,
        canvas_base_url: str,
        canvas_token: str,
        *,
        timeout_seconds: float = 30.0,
        video_token: str = "",
        video_course_id: str = "",
        http_client: Optional[httpx2.Client] = None,
    ) -> None:
        self.canvas_base_url = canvas_base_url.rstrip("/")
        self.canvas_token = canvas_token
        self.video_token = video_token.strip()
        self.video_course_id = video_course_id.strip()
        self._auth_source = (
            "environment" if self.video_token and self.video_course_id else ""
        )
        self._is_new_system = False
        self._owns_client = http_client is None
        self._client = http_client or httpx2.Client(
            timeout=httpx2.Timeout(timeout_seconds),
            follow_redirects=True,
            headers={
                "User-Agent": "sjtu-canvas-assistant/0.1",
                "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            },
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "SJTUVideoClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _canvas_url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.canvas_base_url}{path}"

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        data: Optional[Mapping[str, Any]] = None,
        json_body: Optional[Mapping[str, Any]] = None,
        follow_redirects: Optional[bool] = None,
        service: str = "video",
    ) -> httpx2.Response:
        kwargs: Dict[str, Any] = {
            "params": params,
            "headers": headers,
            "data": data,
            "json": json_body,
        }
        if follow_redirects is not None:
            kwargs["follow_redirects"] = follow_redirects
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx2.TimeoutException as exc:
            raise VideoNetworkError(
                f"{service} request timed out: {method} {_safe_url_label(url)}"
            ) from exc
        except httpx2.TransportError as exc:
            raise VideoNetworkError(
                f"{service} network failure: {method} {_safe_url_label(url)}"
            ) from exc

        if response.status_code >= 400:
            message = self._safe_error_message(response)
            if response.status_code in {401, 403}:
                raise VideoAuthError(
                    f"{service} denied access ({response.status_code}): {message}"
                )
            raise VideoIntegrationError(
                f"{service} returned HTTP {response.status_code}: {message}"
            )
        return response

    def _canvas_api_json(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        response = self._request(
            method,
            self._canvas_url(path),
            params=params,
            headers={"Authorization": f"Bearer {self.canvas_token}"},
            service="canvas",
        )
        return self._decode_json(response, "Canvas")

    def _video_api_json(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        data: Optional[Mapping[str, Any]] = None,
        json_body: Optional[Mapping[str, Any]] = None,
        auth_required: bool = True,
    ) -> Any:
        headers = (
            self._video_headers()
            if auth_required
            else {
                "Accept": "application/json, text/plain, */*",
                "Referer": VIDEO_UI_REFERER,
            }
        )
        response = self._request(
            method,
            url,
            params=params,
            data=data,
            json_body=json_body,
            headers=headers,
            service="video",
        )
        payload = self._decode_json(response, "SJTU video")
        return self._unwrap_video_payload(payload)

    @staticmethod
    def _decode_json(response: httpx2.Response, service_label: str) -> Any:
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise VideoIntegrationError(
                f"{service_label} returned invalid JSON from {_safe_url_label(response.url)}."
            ) from exc

    @staticmethod
    def _safe_error_message(response: httpx2.Response) -> str:
        try:
            payload = response.json()
        except json.JSONDecodeError:
            text = response.text.strip() or response.reason_phrase
            return _redact_sensitive_text(text[:300])
        if isinstance(payload, Mapping):
            for key in ("message", "msg", "error", "errors"):
                value = payload.get(key)
                if value:
                    return _redact_sensitive_text(str(value)[:300])
        return response.reason_phrase

    @staticmethod
    def _unwrap_video_payload(payload: Any) -> Any:
        if not isinstance(payload, Mapping):
            return payload
        code = payload.get("code")
        success = payload.get("success")
        if code is not None and str(code).lower() not in _SUCCESS_CODES:
            message = (
                payload.get("msg")
                or payload.get("message")
                or "SJTU video API returned an error."
            )
            raise VideoIntegrationError(_redact_sensitive_text(str(message)))
        if success is False:
            message = (
                payload.get("msg")
                or payload.get("message")
                or "SJTU video API returned success=false."
            )
            raise VideoIntegrationError(_redact_sensitive_text(str(message)))
        return payload.get("data", payload)

    def _video_headers(self) -> Dict[str, str]:
        if not self.video_token:
            raise VideoAuthError("SJTU video token is missing. Resolve LTI auth first.")
        if self._is_new_system or self.video_token.startswith("eyJ"):
            return {
                "Accept": "application/json, text/plain, */*",
                "Referer": NEW_VIDEO_UI_REFERER,
                "jwt-token": self.video_token,
            }
        return {
            "Accept": "application/json, text/plain, */*",
            "Referer": VIDEO_UI_REFERER,
            "token": self.video_token,
        }

    @staticmethod
    def _extract_form(
        html: str, *, base_url: str, target_action: Optional[str] = None
    ) -> FormPost:
        soup = BeautifulSoup(html, "html.parser")
        forms = list(soup.find_all("form"))
        selected = None
        for form in forms:
            action = str(form.get("action") or "")
            absolute_action = urljoin(base_url, action)
            if target_action is None or absolute_action.startswith(target_action):
                selected = form
                break
        if selected is None and target_action is None and forms:
            selected = forms[0]
        if selected is None:
            expected = f" for {target_action}" if target_action else ""
            raise VideoAuthError(
                f"SJTU video LTI page did not contain the expected form{expected}."
            )

        action = str(selected.get("action") or "")
        if not action:
            raise VideoAuthError("SJTU video LTI form did not include an action URL.")
        data: Dict[str, str] = {}
        for element in selected.find_all(["input", "button"]):
            name = element.get("name")
            if name:
                data[str(name)] = str(element.get("value") or "")
        return FormPost(action=urljoin(base_url, action), data=data)

    @staticmethod
    def _extract_token_id(location: str) -> str:
        parsed = urlparse(location)
        for query in (parsed.query, urlparse(parsed.fragment).query, parsed.fragment):
            token_id = parse_qs(query).get("tokenId")
            if token_id:
                return token_id[0]
        match = re.search(r"(?:^|[?&#])tokenId=([^&#]+)", location)
        if match:
            return match.group(1)
        raise VideoAuthError("SJTU video LTI redirect did not include tokenId.")

    def current_auth(self) -> VideoAuth:
        return VideoAuth(
            video_token=self.video_token,
            video_course_id=self.video_course_id,
            source=self._auth_source,
        )

    def get_sessionless_launch_url(self, course_id: int) -> str:
        payload = self._canvas_api_json(
            "GET",
            f"/api/v1/courses/{course_id}/external_tools/sessionless_launch",
            params={"id": VIDEO_LTI_TOOL_ID, "launch_type": "course_navigation"},
        )
        if not isinstance(payload, Mapping):
            raise VideoAuthError(
                "Canvas sessionless launch returned a non-object payload."
            )
        launch_url = str(payload.get("url") or "").strip()
        if not launch_url.startswith(("http://", "https://")):
            raise VideoAuthError(
                "Canvas sessionless launch did not return a usable launch URL."
            )
        return launch_url

    def resolve_course_video_auth(
        self, course_id: int, *, force: bool = False
    ) -> VideoAuth:
        """Resolve and cache the short-lived SJTU video auth for a Canvas course."""

        if not force and self.video_token and self.video_course_id:
            return self.current_auth()

        launch_url = self.get_sessionless_launch_url(course_id)
        launch_response = self._request(
            "GET",
            launch_url,
            follow_redirects=True,
            service="canvas LTI launch",
        )

        first_form = None
        try:
            first_form = self._extract_form(
                launch_response.text,
                base_url=str(launch_response.url),
                target_action=VIDEO_LTI_FORM_ACTION,
            )
        except VideoAuthError:
            pass

        if first_form is None:
            first_form = self._extract_form(
                launch_response.text,
                base_url=str(launch_response.url),
            )

        if (
            "jy-lti-adapter" in first_form.action
            or "canvas-record" in first_form.action
        ):
            self._is_new_system = True
            resp2 = self._request(
                "POST",
                first_form.action,
                data=first_form.data,
                headers={"Referer": _safe_url_label(launch_response.url)},
                follow_redirects=True,
                service="video LTI adapter initiation",
            )
            second_form = self._extract_form(
                resp2.text,
                base_url=str(resp2.url),
            )
            auth_response = self._request(
                "POST",
                second_form.action,
                data=second_form.data,
                headers={"Referer": _safe_url_label(resp2.url)},
                follow_redirects=False,
                service="video LTI adapter launch",
            )
            location = auth_response.headers.get("location") or str(auth_response.url)
            parsed_loc = urlparse(location)
            frag_qs = parse_qs(urlparse(parsed_loc.fragment).query)
            jwt_token = frag_qs.get("jwt_token", [""])[0]
            if not jwt_token:
                query_qs = parse_qs(parsed_loc.query)
                jwt_token = query_qs.get("jwt_token", [""])[0]
            if not jwt_token:
                raise VideoAuthError(
                    "SJTU video LTI launch redirect did not include jwt_token."
                )

            ctx_response = self._request(
                "GET",
                NEW_VIDEO_LAUNCH_CONTEXT_URL,
                headers={
                    "jwt-token": jwt_token,
                    "Referer": NEW_VIDEO_UI_REFERER,
                    "Accept": "application/json, text/plain, */*",
                },
                service="video launch context",
            )
            ctx_data = self._decode_json(ctx_response, "SJTU video context")
            canvas_rec = (ctx_data.get("data") or {}).get("canvasRecord") or {}
            tecl_id = canvas_rec.get("teachingClassId")
            if not tecl_id:
                course_rec = (ctx_data.get("data") or {}).get("course") or {}
                tecl_id = course_rec.get("teachingClassId")
            if not tecl_id:
                raise VideoAuthError(
                    "SJTU video launch context did not contain teachingClassId."
                )

            self.video_token = jwt_token
            self.video_course_id = str(tecl_id)
            self._auth_source = "canvas_sessionless_lti"
            return self.current_auth()

        login_response = self._request(
            "POST",
            first_form.action,
            data=first_form.data,
            headers={"Referer": _safe_url_label(launch_response.url)},
            follow_redirects=True,
            service="video LTI login",
        )
        second_form = self._extract_form(
            login_response.text,
            base_url=str(login_response.url),
            target_action=VIDEO_LTI_AUTH_ACTION,
        )
        auth_response = self._request(
            "POST",
            second_form.action,
            data=second_form.data,
            headers={"Referer": _safe_url_label(login_response.url)},
            follow_redirects=False,
            service="video LTI auth",
        )
        location = auth_response.headers.get("location") or str(auth_response.url)
        token_id = self._extract_token_id(location)
        data = self._video_api_json(
            "GET",
            VIDEO_TOKEN_BY_ID_URL,
            params={"tokenId": token_id},
            auth_required=False,
        )
        if not isinstance(data, Mapping):
            raise VideoAuthError(
                "SJTU video token exchange returned a non-object payload."
            )
        params = data.get("params") if isinstance(data.get("params"), Mapping) else {}
        video_token = str(
            data.get("token")
            or data.get("accessToken")
            or data.get("access_token")
            or ""
        ).strip()
        video_course_id = str(
            params.get("courId")
            or params.get("canvasCourseId")
            or data.get("courId")
            or data.get("courseId")
            or ""
        ).strip()
        if not video_token:
            raise VideoAuthError("SJTU video token exchange did not return a token.")
        if not video_course_id:
            raise VideoAuthError(
                "SJTU video token exchange did not return a course id."
            )
        self.video_token = video_token
        self.video_course_id = video_course_id
        self._auth_source = "canvas_sessionless_lti"
        return self.current_auth()

    def _ensure_auth(self, course_id: Optional[int]) -> VideoAuth:
        if self.video_token and self.video_course_id:
            return self.current_auth()
        if course_id is None:
            raise VideoAuthError(
                "Canvas course_id is required unless SJTU_VIDEO_TOKEN and SJTU_VIDEO_COURSE_ID are configured."
            )
        return self.resolve_course_video_auth(course_id)

    def list_course_videos(self, course_id: int, limit: int = 100) -> List[CanvasVideo]:
        auth = self._ensure_auth(course_id)
        if self._is_new_system or self.video_token.startswith("eyJ"):
            params = {
                "page.pageIndex": 1,
                "page.pageSize": limit,
                "teclIds": auth.video_course_id,
                "page.orders[0].asc": "false",
                "page.orders[0].field": "courBeginTime",
            }
            response = self._request(
                "GET",
                NEW_VIDEO_LIST_URL,
                params=params,
                headers=self._video_headers(),
                service="video list",
            )
            payload = self._decode_json(response, "SJTU video list")
            data = payload.get("data") if isinstance(payload, Mapping) else payload
            records = (data.get("records") if isinstance(data, Mapping) else None) or []
            videos = []
            for item in records:
                subj = item.get("subjName") or item.get("teclName") or "课程"
                leti = item.get("letiNumber")
                vname = f"{subj} (第{leti}节)" if leti else subj
                teachers = item.get("teacNames") or []
                teacher_str = (
                    ", ".join(teachers) if isinstance(teachers, list) else str(teachers)
                )
                videos.append(
                    CanvasVideo(
                        videoId=str(item.get("id")),
                        videoName=vname,
                        userName=teacher_str,
                        classroomName=str(item.get("clroName") or ""),
                        courseBeginTime=str(item.get("courBeginTime") or ""),
                        courseEndTime=str(item.get("courEndTime") or ""),
                        canvasCourseId=str(course_id),
                        courId=str(item.get("id")),
                    )
                )
            return videos

        data = self._video_api_json(
            "POST",
            VIDEO_LIST_URL,
            json_body={"canvasCourseId": quote(auth.video_course_id, safe="")},
        )
        if isinstance(data, Mapping):
            records = data.get("records") or data.get("list") or data.get("rows") or []
        else:
            records = data
        if not isinstance(records, list):
            raise VideoIntegrationError(
                "SJTU video list response did not contain a records list."
            )
        try:
            return [CanvasVideo.model_validate(item) for item in records]
        except ValidationError as exc:
            raise VideoIntegrationError(
                "SJTU video list response shape is not supported yet."
            ) from exc

    def get_video_info(self, course_id: Optional[int], video_id: str) -> VideoInfo:
        self._ensure_auth(course_id)
        if self._is_new_system or self.video_token.startswith("eyJ"):
            response = self._request(
                "GET",
                NEW_VIDEO_URLS_URL,
                params={"courseId": str(video_id)},
                headers=self._video_headers(),
                service="video info",
            )
            payload = self._decode_json(response, "SJTU video info")
            data = payload.get("data") if isinstance(payload, Mapping) else payload
            if not isinstance(data, Mapping):
                raise VideoIntegrationError(
                    "SJTU video info response did not contain an object."
                )
            play_list = data.get("courseVodViewList") or []
            return VideoInfo(
                id=str(video_id),
                courId=str(video_id),
                videoId=str(video_id),
                videoName=str(data.get("courName") or ""),
                userName=str(data.get("tecName") or ""),
                classroomName=str(data.get("classRoomName") or ""),
                courseBeginTime=str(data.get("courBeginTime") or ""),
                courseEndTime=str(data.get("courEndTime") or ""),
                courseVodViewList=play_list,
            )

        data = self._video_api_json(
            "POST",
            VIDEO_INFO_URL,
            data={"playTypeHls": "true", "id": str(video_id), "isAudit": "true"},
        )
        if isinstance(data, list):
            if not data:
                raise VideoIntegrationError(
                    f"SJTU video info response is empty for video {video_id}."
                )
            data = data[0]
        if not isinstance(data, Mapping):
            raise VideoIntegrationError(
                "SJTU video info response did not contain an object."
            )
        try:
            return VideoInfo.model_validate(data)
        except ValidationError as exc:
            raise VideoIntegrationError(
                "SJTU video info response shape is not supported yet."
            ) from exc
