from __future__ import annotations

import json

from integrations.sjtu_canvas_assistant.models import Course, VideoAuth, VideoAuthSource
from integrations.sjtu_canvas_assistant.video_client import SJTUVideoClient


def test_video_auth_source_serialization() -> None:
    auth = VideoAuth(source=VideoAuthSource.CANVAS_SESSIONLESS_LTI)
    assert auth.source is VideoAuthSource.CANVAS_SESSIONLESS_LTI
    assert json.loads(auth.model_dump_json())["source"] == "canvas_sessionless_lti"
    assert VideoAuth().source is VideoAuthSource.NONE


def test_video_auth_environment_source() -> None:
    with SJTUVideoClient(
        "https://oc.sjtu.edu.cn",
        "canvas-token",
        video_token="video-token",
        video_course_id="1",
    ) as client:
        assert client.current_auth().source is VideoAuthSource.ENVIRONMENT


def test_external_workflow_state_is_open() -> None:
    course = Course(id=1, workflow_state="external-state")
    assert course.workflow_state == "external-state"
