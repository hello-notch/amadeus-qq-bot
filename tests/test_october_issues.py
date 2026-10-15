from __future__ import annotations

import asyncio
import base64
import io
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import nonebot
import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from PIL import Image, UnidentifiedImageError

from amadeus_bot.adapters.ai_provider import (
    AIProviderRateLimited,
    ApiCredential,
    OpenAICompatibleProvider,
    ProviderConfig,
)
from amadeus_bot.adapters.gemini_provider import GeminiProvider
from amadeus_bot.domain.ai import AIResponse
from amadeus_bot.domain.permissions import PermissionLevel
from amadeus_bot.repositories.core import CoreRepository
from amadeus_bot.repositories.database import CoreDatabase
from amadeus_bot.services.activity_log import summarize_notice
from amadeus_bot.services.analytics import AnalyticsWindow
from amadeus_bot.services.group_history import GroupHistoryService
from amadeus_bot.services.image_generation import ImageGenerationService
from amadeus_bot.services.message_log import MessageNormalizer


def repository(tmp_path: Path) -> CoreRepository:
    database = CoreDatabase(tmp_path / "core.sqlite3")
    database.initialize()
    return CoreRepository(database)


def test_context_clear_preserves_other_scope_and_settings(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    repo.append_conversation("group:1", "user", "old", "10")
    repo.append_conversation("private:10", "user", "keep", "10")
    repo.set_runtime_setting("test.system", "keep", "10")
    repo.clear_conversation("group:1")
    assert not repo.recent_conversation("group:1")
    assert repo.recent_conversation("private:10")[0]["content"] == "keep"
    repo.clear_conversation()
    assert not repo.recent_conversation("private:10")
    assert repo.get_runtime_setting("test.system") == "keep"


def test_face_argument_supports_zero_and_rejects_mixed_segments() -> None:
    nonebot.init()
    from amadeus_bot.plugins.utility import _stick_argument

    assert _stick_argument(Message(MessageSegment.face(66)), {}) == "66"
    assert _stick_argument(Message(MessageSegment.face(0)), {}) == "0"
    assert _stick_argument(Message("66"), {}) == "66"
    for message in (
        Message("66") + MessageSegment.face(66),
        Message(MessageSegment.face(66)) + MessageSegment.face(67),
        Message(MessageSegment.image("https://example.invalid/image")),
    ):
        with pytest.raises(ValueError):
            _stick_argument(message, {})


def test_reaction_notice_reads_napcat_likes() -> None:
    event = SimpleNamespace(
        notice_type="group_msg_emoji_like",
        user_id=1,
        group_id=2,
        message_id=3,
        likes=[{"emoji_id": "66", "count": 2}, {"emoji_id": "0", "count": 1}],
    )
    result = summarize_notice(event)
    assert "回应表情 66, 0" in result[1]
    assert result[4]["likes"] == event.likes


def test_normalizer_keeps_adapter_detached_reply() -> None:
    event = SimpleNamespace(
        message_type="group",
        group_id=2,
        message_id=3,
        self_id=4,
        time=123,
        reply=SimpleNamespace(message_id=9),
        get_user_id=lambda: "1",
        get_message=lambda: Message("hello"),
        get_plaintext=lambda: "hello",
    )
    record = MessageNormalizer.from_onebot_event(event)
    assert record.segments[0].type == "reply"
    assert record.segments[0].data == {"id": "9"}


def test_summary_excludes_registered_unprefixed_commands() -> None:
    nonebot.init()
    from amadeus_bot.plugins import group_content

    assert group_content
    now = datetime.now().astimezone()
    window = AnalyticsWindow(
        "1",
        5,
        now,
        now,
        tuple({"plain_text": text} for text in ("summary 5", "/summary 5", "summary内容值得讨论", "hello")),
    )
    assert [row["plain_text"] for row in window.effective_records] == ["summary内容值得讨论", "hello"]


class Finished(Exception):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [PermissionLevel.EVERYONE, PermissionLevel.MEMBER])
async def test_group_clear_requires_superuser(tmp_path: Path, monkeypatch, role) -> None:
    nonebot.init()
    from amadeus_bot.plugins import chat

    repo = repository(tmp_path)
    repo.append_conversation("group:2", "user", "keep", "1")
    container = SimpleNamespace(
        repository=repo, permissions=SimpleNamespace(role_for=lambda _: role), activity_log=AsyncMock()
    )
    monkeypatch.setattr(chat, "get_container", lambda: container)
    monkeypatch.setattr(chat.history_command, "finish", AsyncMock(side_effect=Finished))
    event = SimpleNamespace(message_type="group", group_id=2, get_user_id=lambda: "1")
    with pytest.raises(Finished):
        await chat.handle_history(event, Message("clear"))
    assert repo.recent_conversation("group:2")
    assert "SUPERUSER" in chat.history_command.finish.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [True, False])
async def test_clear_success_keeps_other_conversation(tmp_path: Path, monkeypatch, group) -> None:
    nonebot.init()
    from amadeus_bot.plugins import chat

    repo = repository(tmp_path)
    scope = "group:2" if group else "private:1"
    repo.append_conversation(scope, "user", "clear", "1")
    repo.append_conversation("private:other", "user", "keep", "other")
    container = SimpleNamespace(
        repository=repo,
        permissions=SimpleNamespace(role_for=lambda _: PermissionLevel.SUPERUSER),
        activity_log=AsyncMock(),
    )
    monkeypatch.setattr(chat, "get_container", lambda: container)
    monkeypatch.setattr(chat.history_command, "finish", AsyncMock(side_effect=Finished))
    event = SimpleNamespace(message_type="group" if group else "private", group_id=2, get_user_id=lambda: "1")
    with pytest.raises(Finished):
        await chat.handle_history(event, Message("clear"))
    assert not repo.recent_conversation(scope)
    assert repo.recent_conversation("private:other")


@pytest.mark.asyncio
async def test_group_history_deduplicates_and_does_not_repeat_on_reconnect(tmp_path: Path) -> None:
    container = SimpleNamespace(
        paths=SimpleNamespace(logs=tmp_path),
        activity_log=AsyncMock(),
        memory=SimpleNamespace(logging_enabled=lambda user: user != "excluded"),
    )
    row = {"message_id": 1, "user_id": 10, "time": 1790789500, "message": "hello"}
    bot = SimpleNamespace(
        self_id=99,
        get_group_list=AsyncMock(return_value=[{"group_id": 2}]),
        call_api=AsyncMock(return_value={"messages": [row, row]}),
    )
    service = GroupHistoryService(container)
    await service.refresh(bot)
    await service.refresh(bot)
    assert bot.call_api.await_count == 1
    files = list((tmp_path / "messages" / "2").glob("*.jsonl"))
    assert len(files) == 1
    assert len(files[0].read_text(encoding="utf-8").splitlines()) == 1
    service2 = GroupHistoryService(container)
    await service2.refresh(bot)
    assert len(files[0].read_text(encoding="utf-8").splitlines()) == 1


@pytest.mark.asyncio
async def test_gemini_preserves_tool_signature_and_hides_thoughts() -> None:
    captured = []
    signature = {
        "functionCall": {"id": "native-call-1", "name": "calculate", "args": {"expression": "1+2"}},
        "thoughtSignature": "opaque-signature",
    }

    def handler(request):
        captured.append(json.loads(request.content))
        parts = (
            [{"thought": True, "text": "private reasoning"}, signature]
            if len(captured) == 1
            else [{"text": "3"}]
        )
        return httpx.Response(200, json={"candidates": [{"content": {"parts": parts}}]})

    provider = GeminiProvider(
        ProviderConfig("gemini", "example.invalid", "v1beta", api_mode="gemini", thinking_level="high"),
        ApiCredential("https://example.invalid", "test"),
    )
    await provider._client.aclose()
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        messages = [{"role": "system", "content": "persona"}, {"role": "user", "content": "1+2"}]
        first = await provider.complete(model="flash", messages=messages, temperature=0.2, max_tokens=8192)
        call = first.tool_calls[0]
        assert not first.content
        messages.extend(
            [
                {
                    "role": "assistant",
                    "_gemini_parts": list(first.output_items),
                    "tool_calls": [{"id": call.call_id, "function": {"name": call.name}}],
                },
                {"role": "tool", "tool_call_id": call.call_id, "content": '{"value":3}'},
            ]
        )
        second = await provider.complete(model="flash", messages=messages, temperature=0.2, max_tokens=8192)
    finally:
        await provider.close()
    assert second.content == "3"
    assert captured[0]["systemInstruction"]["parts"] == [{"text": "persona"}]
    assert captured[1]["contents"][1]["parts"][-1] == signature
    assert captured[1]["contents"][2]["parts"][0]["functionResponse"]["name"] == "calculate"
    assert captured[1]["contents"][2]["parts"][0]["functionResponse"]["id"] == "native-call-1"
    assert captured[0]["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "HIGH"


@pytest.mark.asyncio
async def test_image_generation_validates_output_and_rejects_text_only(tmp_path: Path) -> None:
    stream = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(stream, format="PNG")
    ai = SimpleNamespace(
        complete=AsyncMock(
            return_value=AIResponse(content="", provider="gemini", model="image", images=(stream.getvalue(),))
        )
    )
    service = ImageGenerationService(ai, tmp_path)
    path = await service.generate("apple", "1")
    assert path.read_bytes() == stream.getvalue()
    ai.complete.return_value = AIResponse(content="refused", provider="gemini", model="image")
    with pytest.raises(ValueError, match="未返回图片"):
        await service.generate("apple", "1")
    with pytest.raises(ValueError):
        await service.generate("", "1")
    with pytest.raises(UnidentifiedImageError):
        service._save(base64.b64decode("aW52YWxpZA=="))
    ai.complete.side_effect = AIProviderRateLimited("HTTP 429")
    with pytest.raises(ValueError, match="配额已耗尽或限流"):
        await service.generate("apple", "1")


@pytest.mark.asyncio
async def test_gpt_image_provider_requests_image_endpoint() -> None:
    captured = {}
    stream = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(stream, format="PNG")

    def handler(request):
        captured["path"] = request.url.path
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "data": [{"b64_json": base64.b64encode(stream.getvalue()).decode()}],
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
        )

    provider = OpenAICompatibleProvider(
        ProviderConfig("images", "example.invalid", "v1", api_mode="images"),
        ApiCredential("https://example.invalid", "test"),
    )
    await provider._client.aclose()
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await provider.complete(
            model="gpt-image-2.5",
            messages=[{"role": "user", "content": "apple"}],
            temperature=1.0,
            max_tokens=8192,
        )
    finally:
        await provider.close()
    assert captured["path"] == "/v1/images/generations"
    assert captured["payload"]["model"] == "gpt-image-2.5"
    assert captured["payload"]["n"] == 1
    assert result.images == (stream.getvalue(),)
    assert result.output_tokens == 20


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body",
    [
        (429, {"error": {"message": "private upstream error"}}),
        (200, {"data": [{"b64_json": "invalid base64"}]}),
        (200, {"data": []}),
    ],
)
async def test_gpt_image_provider_rejects_rate_limit_and_bad_data(status, body) -> None:
    provider = OpenAICompatibleProvider(
        ProviderConfig("images", "example.invalid", "v1", api_mode="images"),
        ApiCredential("https://example.invalid", "test"),
    )
    await provider._client.aclose()
    provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body))
    )
    try:
        with pytest.raises((RuntimeError, ValueError)) as failure:
            await provider.complete(
                model="gpt-image-2.5",
                messages=[{"role": "user", "content": "apple"}],
                temperature=1.0,
                max_tokens=8192,
            )
        assert "private upstream error" not in str(failure.value)
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_group_history_scope_failure_does_not_mark_loaded(tmp_path: Path) -> None:
    container = SimpleNamespace(
        paths=SimpleNamespace(logs=tmp_path),
        activity_log=AsyncMock(),
        memory=SimpleNamespace(logging_enabled=lambda _: True),
    )
    bot = SimpleNamespace(
        self_id=99,
        get_group_list=AsyncMock(return_value=[{"group_id": 2}]),
        call_api=AsyncMock(
            return_value={
                "messages": [
                    {
                        "message_id": 1,
                        "group_id": 3,
                        "user_id": 10,
                        "time": 123,
                        "message": "private",
                    }
                ]
            }
        ),
    )
    service = GroupHistoryService(container)
    await service.refresh(bot)
    assert service.loaded == set()
    assert service.context == {}
    assert not await asyncio.to_thread(lambda: list(tmp_path.rglob("*.jsonl")))
    assert container.activity_log.record.await_args.kwargs["status"] == "failed"


@pytest.mark.asyncio
async def test_image_tool_sends_standard_message_to_requester(tmp_path: Path) -> None:
    from amadeus_bot.repositories.user_data import UserDataRepository
    from amadeus_bot.services.ddl import DDLService
    from amadeus_bot.services.feature_flags import FeatureFlagService
    from amadeus_bot.services.tools import AIToolService, ToolExecutionContext
    from amadeus_bot.simulator import USERS, Simulator

    repo = repository(tmp_path)
    image = tmp_path / "image.png"
    await asyncio.to_thread(image.write_bytes, b"image-sending-test")
    generator = SimpleNamespace(generate=AsyncMock(return_value=image))
    service = AIToolService(
        DDLService(UserDataRepository(tmp_path / "users")),
        repo,
        FeatureFlagService(repo),
        image_generation=generator,
    )
    simulator = Simulator(tmp_path / "simulator")

    async def send_private_msg(**params):
        assert isinstance(params["message"], Message)
        return simulator.handle_api("send_private_msg", params)

    bot = SimpleNamespace(send_private_msg=send_private_msg)
    user = str(USERS[1]["user_id"])
    result = json.loads(
        await service.execute(
            "image_generate",
            {"prompt": "apple"},
            ToolExecutionContext.for_requester(user, None, bot=bot),
        )
    )
    assert result["success"] and result["image_sent"]
    sent = simulator.messages[-1]
    assert sent["recipient_id"] == int(user)
    assert sent["segments"][0].type == "image"
    generator.generate.assert_awaited_once_with("apple", user, None)
