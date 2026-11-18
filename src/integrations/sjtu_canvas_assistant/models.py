from __future__ import annotations

from enum import StrEnum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CanvasModel(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Teacher(CanvasModel):
    id: Optional[int] = None
    display_name: str = ""
    avatar_image_url: str = ""
    html_url: str = ""


class Term(CanvasModel):
    id: Optional[int] = None
    name: str = ""
    start_at: Optional[str] = None
    end_at: Optional[str] = None
    workflow_state: str = ""


class Enrollment(CanvasModel):
    type: str = ""
    role: str = ""
    enrollment_state: str = ""


class Course(CanvasModel):
    id: int
    uuid: str = ""
    name: str = ""
    course_code: str = ""
    enrollments: List[Enrollment] = Field(default_factory=list)
    access_restricted_by_date: Optional[bool] = None
    teachers: List[Teacher] = Field(default_factory=list)
    term: Optional[Term] = None
    workflow_state: str = ""

    @property
    def display_name(self) -> str:
        return self.name or self.course_code or f"course-{self.id}"


class VideoAuthSource(StrEnum):
    NONE = ""
    ENVIRONMENT = "environment"
    CANVAS_SESSIONLESS_LTI = "canvas_sessionless_lti"


class VideoAuth(CanvasModel):
    """Resolved SJTU video service auth for one Canvas course.

    The token is intentionally not included in normal CLI/MCP outputs.
    """

    video_token: str = ""
    video_course_id: str = ""
    source: VideoAuthSource = VideoAuthSource.NONE


class CanvasVideo(CanvasModel):
    video_id: str = Field(default="", alias="videoId")
    video_name: str = Field(default="", alias="videoName")
    user_name: str = Field(default="", alias="userName")
    classroom_name: str = Field(default="", alias="classroomName")
    course_begin_time: str = Field(default="", alias="courseBeginTime")
    course_end_time: str = Field(default="", alias="courseEndTime")
    canvas_course_id: str = Field(default="", alias="canvasCourseId")
    cour_id: str = Field(default="", alias="courId")

    @property
    def display_name(self) -> str:
        return self.video_name or f"video-{self.video_id}"

    @field_validator(
        "video_id",
        "video_name",
        "user_name",
        "classroom_name",
        "course_begin_time",
        "course_end_time",
        "canvas_course_id",
        "cour_id",
        mode="before",
    )
    @classmethod
    def _stringify(cls, value: object) -> str:
        return "" if value is None else str(value)


class VideoInfo(CanvasModel):
    id: str = ""
    cour_id: str = Field(default="", alias="courId")
    video_id: str = Field(default="", alias="videoId")
    video_name: str = Field(default="", alias="videoName")
    user_name: str = Field(default="", alias="userName")
    classroom_name: str = Field(default="", alias="classroomName")
    course_begin_time: str = Field(default="", alias="courseBeginTime")
    course_end_time: str = Field(default="", alias="courseEndTime")
    video_play_response_vo_list: List[Dict[str, Any]] = Field(
        default_factory=list, alias="videoPlayResponseVoList"
    )
    video_play_response_vo_list_snake: List[Dict[str, Any]] = Field(
        default_factory=list, alias="video_play_response_vo_list"
    )
    course_vod_view_list: List[Dict[str, Any]] = Field(
        default_factory=list, alias="courseVodViewList"
    )

    @property
    def play_items(self) -> List[Dict[str, Any]]:
        return (
            self.video_play_response_vo_list
            or self.video_play_response_vo_list_snake
            or self.course_vod_view_list
        )

    def get_stream_url(self, view_num: int = 1) -> Optional[str]:
        for item in self.play_items:
            if item.get("viewNum") == view_num and item.get("url"):
                return str(item["url"])
        for item in self.play_items:
            url = (
                item.get("url")
                or item.get("playUrl")
                or item.get("videoUrl")
                or item.get("hlsUrl")
            )
            if url:
                return str(url)
        return None

    @field_validator(
        "id",
        "cour_id",
        "video_id",
        "video_name",
        "user_name",
        "classroom_name",
        "course_begin_time",
        "course_end_time",
        mode="before",
    )
    @classmethod
    def _stringify(cls, value: object) -> str:
        return "" if value is None else str(value)
