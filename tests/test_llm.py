"""Application model responses retain provider identity and image content."""

import pytest
from llm.service import LLMService
from llm.types import ImagePart, Message, TextPart


@pytest.mark.parametrize("message_as_dict", [False, True])
def test_image_and_usage(message_as_dict):
    class Runtime:
        def call(self, method, **params):
            return {
                "usage": {
                    "lecture/vision": {
                        "input": 7,
                        "output": 3,
                        "cacheRead": 2,
                        "totalTokens": 12,
                    }
                }
            }

        def stream(self, method, **params):
            assert method == "generate"
            assert params["role"] == "board_recognition"
            assert params["messages"][0]["content"] == [
                {"type": "text", "text": "Read the board"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,YQ=="},
                },
            ]
            yield {
                "type": "result",
                "result": {
                    "content": "Formula",
                    "provider": "lecture",
                    "model": "vision",
                    "usage": {
                        "input": 7,
                        "output": 3,
                        "cacheRead": 2,
                        "totalTokens": 12,
                    },
                },
            }

    service = LLMService(runtime=Runtime())
    response = service.chat(
        role="board_recognition",
        messages=[
            {
                "role": "user",
                "content": [
                    TextPart("Read the board"),
                    ImagePart("data:image/png;base64,YQ=="),
                ],
            }
            if message_as_dict
            else Message(
                role="user",
                content=[
                    TextPart("Read the board"),
                    ImagePart("data:image/png;base64,YQ=="),
                ],
            )
        ],
    )
    assert response.content == "Formula"
    assert response.model == "lecture/vision"
    assert response.usage.prompt_tokens == 9
    assert response.usage.cached_tokens == 2
    assert service.usage_snapshot()["lecture/vision"]["total_tokens"] == 12
