from pathlib import Path
from types import SimpleNamespace

import nonebot
import pytest

from amadeus_bot.services.event_utils import onebot_message


@pytest.mark.asyncio
async def test_chat_reads_replied_text_file_with_bounded_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nonebot.init()
    from amadeus_bot.plugins import chat

    class Response:
        def raise_for_status(self):
            return None

        async def aiter_bytes(self):
            yield b"BEGIN:VCALENDAR\nSUMMARY:Math\nEND:VCALENDAR"

    class Stream:
        async def __aenter__(self):
            return Response()

        async def __aexit__(self, *_args):
            return None

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, method, url):
            assert method == "GET"
            assert url == "https://tjc-download.ftn.qq.com/test.ics"
            return Stream()

    class Bot:
        async def get_msg(self, *, message_id):
            assert message_id == 42
            return {
                "message": {
                    "type": "file",
                    "data": {"name": "test.ics", "url": "https://tjc-download.ftn.qq.com/test.ics"},
                }
            }

    class Event:
        message_type = "private"
        message_id = 43
        reply = SimpleNamespace(message_id=42)

        def get_message(self):
            return onebot_message({"type": "text", "data": {"text": "/chat 看这个文件"}})

        def get_plaintext(self):
            return "/chat 看这个文件"

    monkeypatch.setattr(chat.httpx, "AsyncClient", Client)
    container = SimpleNamespace(paths=SimpleNamespace(logs=tmp_path))
    content = await chat._enrich_input(container, Bot(), Event(), "看这个文件", None, "100")
    assert "文件 test.ics 的正文" in content
    assert "SUMMARY:Math" in content
    assert "tjc-download.ftn.qq.com" not in content
