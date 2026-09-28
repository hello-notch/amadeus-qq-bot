from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import nonebot
import pytest

from amadeus_bot.services.event_utils import onebot_message
from amadeus_bot.services.proactive_chat import (
    ProactiveContext,
    chat_bubbles,
    parse_gate_decision,
    proactive_score,
)


@pytest.mark.parametrize("text", ["bot", "BOT", "机器人", "amadeus", "命运石之门", "牧濑红莉栖"])
def test_keywords(text):
    assert proactive_score(text) >= 2


def test_question_and_no_substring_bot():
    assert proactive_score("有人知道怎么办？") >= 2
    assert proactive_score("robotics") == 0


def test_context_sampling_expiry_and_isolation():
    context = ProactiveContext()
    for i in range(4):
        context.observe(i + 1, str(i % 2), "聊天")
    assert context.conversation_candidate(5)
    context.last_gate = 5
    assert not context.conversation_candidate(10)
    assert context.conversation_candidate(50)
    context.observe(130, "1", "新话题")
    assert not context.conversation_candidate(130)
    assert not ProactiveContext().conversation_candidate(5)


@pytest.mark.parametrize(
    "content",
    [
        '{"respond":true,"confidence":0.8}',
        '```json\n{"respond":true,"confidence":1}\n```',
    ],
)
def test_gate_valid(content):
    assert parse_gate_decision(content)


@pytest.mark.parametrize(
    "content",
    [
        '{"respond":"false","confidence":0.9}',
        '{"respond":true,"confidence":true}',
        '{"respond":true,"confidence":2}',
        '{"respond":false,"confidence":0.9}',
    ],
)
def test_gate_rejects_invalid_types(content):
    assert not parse_gate_decision(content)


@pytest.mark.parametrize("content", ["没有 JSON", '{"respond":true,"confidence":'])
def test_gate_truncation_is_explicit(content):
    with pytest.raises(ValueError):
        parse_gate_decision(content)


def test_bubbles_preserve_long_content_and_code():
    assert chat_bubbles("好啊！一起聊聊吧。", 500) == ["好啊！", "一起聊聊吧。"]
    assert chat_bubbles("第一句\n\n第二句", 500) == ["第一句", "第二句"]
    for text in ["x" * 500, "```python\nprint(1)\n```", "a\nb", "一。二。三。四。"]:
        assert chat_bubbles(text, 500) == [text]


@pytest.mark.asyncio
async def test_handler_context_gate_failure_limits_and_scope(monkeypatch):
    nonebot.init()
    from amadeus_bot.plugins import chat

    container = SimpleNamespace(
        features=SimpleNamespace(
            status=Mock(return_value=SimpleNamespace(enabled=True)),
            is_ignored=Mock(return_value=False),
        ),
        repository=SimpleNamespace(command_disabled=Mock(return_value=False)),
        activity_log=SimpleNamespace(record=AsyncMock()),
        ai=SimpleNamespace(
            complete=AsyncMock(return_value=SimpleNamespace(content='{"respond":true,"confidence":0.9}'))
        ),
    )
    monkeypatch.setattr(chat, "get_container", lambda: container)
    monkeypatch.setattr(chat, "_proactive_contexts", chat.defaultdict(ProactiveContext))
    monkeypatch.setattr(chat, "_proactive_times", chat.defaultdict(chat.deque))
    monkeypatch.setattr(chat, "_proactive_locks", chat.defaultdict(chat.asyncio.Lock))
    monkeypatch.setattr(chat.asyncio, "sleep", AsyncMock())
    respond = AsyncMock()
    monkeypatch.setattr(chat, "_respond", respond)

    def event(user="1", scope="group", text="一起聊聊"):
        return SimpleNamespace(
            message_type=scope,
            group_id=10,
            get_user_id=lambda: user,
            get_plaintext=lambda: text,
            get_message=lambda: onebot_message(text),
        )

    for user in ("1", "2", "1", "2"):
        await chat.handle_proactive(None, event(user))
    assert respond.await_count == 1
    prompt = container.ai.complete.call_args.args[1][0]["content"]
    assert "QQ 1" in prompt and "QQ 2" in prompt
    await chat.handle_proactive(None, event(text="机器人？"))
    assert respond.await_count == 1
    await chat.handle_proactive(None, event(scope="private", text="机器人？"))
    assert len(chat._proactive_contexts) == 1

    chat._proactive_times.clear()
    container.ai.complete.return_value.content = '{"respond":'
    await chat.handle_proactive(None, event(text="机器人？"))
    assert respond.await_count == 1
    assert container.activity_log.record.call_args.kwargs["status"] == "failed"

    container.features.status.return_value.enabled = False
    calls = container.ai.complete.await_count
    await chat.handle_proactive(None, event(text="机器人？"))
    assert container.ai.complete.await_count == calls

    container.features.status.return_value.enabled = True
    container.features.is_ignored.return_value = True
    await chat.handle_proactive(None, event(text="机器人？"))
    assert container.ai.complete.await_count == calls

    container.features.is_ignored.return_value = False
    container.repository.command_disabled.return_value = True
    await chat.handle_proactive(None, event(text="机器人？"))
    assert container.ai.complete.await_count == calls
