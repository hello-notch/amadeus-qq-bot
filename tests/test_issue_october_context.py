import asyncio
import io
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import nonebot
import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.log import logger

from amadeus_bot.domain.ai import AIResponse, AITask
from amadeus_bot.repositories.core import CoreRepository
from amadeus_bot.repositories.database import CoreDatabase
from amadeus_bot.services.ai_errors import AIInvocationError, failure_reason
from amadeus_bot.services.ai_router import AIRouteCatalog, AIService
from amadeus_bot.services.chat_output import ThinkingReaction, mention_message
from amadeus_bot.services.conversation import record_inbound
from amadeus_bot.services.feature_flags import FeatureFlagService
from amadeus_bot.services.group_admin import GroupAdminService
from amadeus_bot.services.renderer import RenderService

ROUTES_PATH = Path(__file__).resolve().parents[1] / "config/ai_routes.toml"


def make_container(path):
    database = CoreDatabase(path / "core.sqlite3")
    database.initialize()
    repository = CoreRepository(database)
    return SimpleNamespace(
        repository=repository,
        features=FeatureFlagService(repository),
        memory=SimpleNamespace(logging_enabled=Mock(return_value=True)),
        activity_log=SimpleNamespace(record=AsyncMock()),
        paths=SimpleNamespace(logs=path / "logs"),
    )


def event(message_id=1, *, user="10001", scope="group", text="日常聊天"):
    return SimpleNamespace(
        message_id=message_id,
        message_type=scope,
        group_id=12345,
        time=100,
        sender=SimpleNamespace(nickname="测试群友", card=""),
        get_user_id=lambda: user,
        get_plaintext=lambda: text,
        get_message=lambda: Message(text),
    )


def test_context_collects_every_message_without_ai_or_feature_gate(tmp_path):
    container = make_container(tmp_path)
    container.features.set("chat", "12345", False, "999")
    for i in range(40):
        record_inbound(container, event(i, text=f"闲聊 {i}"))
    assert len(container.repository.recent_conversation("group:12345", 100)) == 40
    record_inbound(container, event(39), enriched="识图补充")
    rows = container.repository.recent_conversation("group:12345", 100)
    assert len(rows) == 40
    assert "识图补充" in rows[-1]["content"]
    assert "测试群友" in rows[-1]["content"]
    private = event(39, scope="private")
    record_inbound(container, private)
    assert len(container.repository.recent_conversation("private:10001")) == 1
    assert container.repository.recent_conversation("private:10002") == []
    container.repository.clear_conversation("group:12345")
    assert not container.repository.recent_conversation("group:12345")
    record_inbound(container, event(40))
    assert len(container.repository.recent_conversation("group:12345")) == 1


def test_context_privacy_exclusions_and_nontext(tmp_path):
    container = make_container(tmp_path)
    container.memory.logging_enabled.return_value = False
    record_inbound(container, event())
    container.memory.logging_enabled.return_value = True
    container.repository.add_ignore_rule("10001", "group", "12345", "999")
    record_inbound(container, event(2))
    assert not container.repository.recent_conversation("group:12345")
    media = event(3, user="10002")
    media.get_message = lambda: Message(MessageSegment.face(181))
    record_inbound(container, media)
    assert "[CQ:face,id=181]" in container.repository.recent_conversation("group:12345")[0]["content"]


def test_context_migration_is_idempotent_and_preserves_legacy(tmp_path):
    path = tmp_path / "core.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE conversation_messages (conversation_id INTEGER PRIMARY KEY,"
            "scope_key TEXT, role TEXT, user_id TEXT, content TEXT, created_at TEXT)"
        )
        conn.execute("INSERT INTO conversation_messages VALUES (1,'g','user','1','old','')")
    database = CoreDatabase(path)
    database.initialize()
    database.initialize()
    repository = CoreRepository(database)
    for _ in range(2):
        repository.record_conversation_input("g", "42", "new", "1")
    assert [row["content"] for row in repository.recent_conversation("g")] == ["old", "new"]


class End(Exception):
    pass


class Matcher:
    def __init__(self, timeline):
        self.timeline = timeline

    async def send(self, message):
        self.timeline.append(("send", message))

    async def finish(self, message):
        self.timeline.append(("send", message))
        raise End


class Bot:
    self_id = "99999"

    def __init__(self, timeline=None, *, bot_role="admin", actor_role="owner", target_role="member"):
        self.timeline = timeline if timeline is not None else []
        self.roles = {"99999": bot_role, "10001": actor_role, "10002": target_role}

    async def call_api(self, name, **params):
        self.timeline.append((name, params))

    async def get_group_member_info(self, *, group_id, user_id, no_cache):
        assert group_id == 12345 and no_cache is True
        return {"user_id": user_id, "role": self.roles[str(user_id)]}

    async def get_group_member_list(self, **_kwargs):
        return [{"user_id": 10001}, {"user_id": 10002}]


@pytest.mark.parametrize("mode", ["success", "failure", "cancel", "empty"])
async def test_thinking_reaction_wraps_model_and_cleans_up(tmp_path, monkeypatch, mode):
    nonebot.init()
    from amadeus_bot.plugins import chat

    container = make_container(tmp_path)
    timeline = []
    record_inbound(container, event(10, user="10002", text="今晚要吃火锅"))
    container.ai_tools = SimpleNamespace(schemas=lambda: [])
    container.permissions = SimpleNamespace(role_for=lambda _: None)

    async def complete(task, messages, **_kwargs):
        timeline.append(("model", task))
        assert any("今晚要吃火锅" in row["content"] for row in messages)
        if mode == "failure":
            raise httpx.ReadTimeout("secret-url?token=hidden")
        if mode == "cancel":
            raise asyncio.CancelledError
        return AIResponse(content="" if mode == "empty" else "收到", provider="fake", model="fake")

    container.ai = SimpleNamespace(complete=complete)
    monkeypatch.setattr(chat, "get_container", lambda: container)
    monkeypatch.setattr(chat, "_history_service", None)
    with pytest.raises(asyncio.CancelledError if mode == "cancel" else End):
        await chat._respond_locked(Matcher(timeline), Bot(timeline), event(11), "日常聊天")
    assert timeline[0] == ("set_msg_emoji_like", {"message_id": 11, "emoji_id": 181, "set": True})
    assert timeline[1] == ("model", AITask.CHAT)
    assert timeline[2] == ("set_msg_emoji_like", {"message_id": 11, "emoji_id": 181, "set": False})
    if mode != "cancel":
        assert timeline[3][0] == "send"
    assert (
        len(
            [
                row
                for row in container.repository.recent_conversation("group:12345")
                if "[消息 #11]" in row["content"]
            ]
        )
        == 1
    )


async def test_mentions_are_real_segments_but_code_all_and_unknown_stay_literal():
    text = "@10002 你好 [CQ:at,qq=10001] @all @33333 `@10002`\n```text\n@10001\n```"
    message = await mention_message(text, Bot(), "12345")
    assert [item.data["qq"] for item in message if item.type == "at"] == ["10002", "10001"]
    assert "@33333" in message.extract_plain_text()
    assert "@all" in message.extract_plain_text()
    private = await mention_message(text, Bot(), None)
    assert all(item.type == "text" for item in private)


@pytest.mark.parametrize(
    "roles,success",
    [
        ({}, True),
        ({"bot_role": "member"}, False),
        ({"actor_role": "member"}, False),
        ({"target_role": "admin"}, False),
        ({"target_role": "owner"}, False),
    ],
)
@pytest.mark.parametrize("action,value", [("group_card", "新名字"), ("group_ban", 60), ("group_ban", 0)])
async def test_group_admin_permissions_before_any_write(tmp_path, roles, success, action, value):
    container = make_container(tmp_path)
    bot = Bot(**roles)
    result = await GroupAdminService(container.features).execute(
        action, bot=bot, group_id="12345", actor="10001", target="10002", value=value
    )
    assert result["success"] is success
    assert bool(bot.timeline) is success
    if success:
        assert bot.timeline[0][0] == ("set_group_card" if action == "group_card" else "set_group_ban")


async def test_group_admin_errors_are_results_and_validation_is_strict(tmp_path):
    container = make_container(tmp_path)
    service = GroupAdminService(container.features)
    bot = Bot()
    for scope, target, value in [
        (None, "10002", 10),
        ("12345", "10002", -1),
        ("12345", "10002", True),
        ("12345", "99999", 10),
    ]:
        result = await service.execute(
            "group_ban", bot=bot, group_id=scope, actor="10001", target=target, value=value
        )
        assert result["success"] is False
    bot.call_api = AsyncMock(side_effect=RuntimeError("sensitive response"))
    result = await service.execute(
        "group_ban", bot=bot, group_id="12345", actor="10001", target="10002", value=60
    )
    assert not result["success"] and "sensitive" not in result["error"]
    container.features.set("group_admin", "12345", False, "999")
    assert not (
        await service.execute(
            "group_card", bot=bot, group_id="12345", actor="10001", target="10002", value="abc"
        )
    )["success"]


@pytest.mark.parametrize(
    "action,value,bot_role,actor_role,success",
    [
        ("group_card", "自己的新名片", "admin", "member", True),
        ("group_card", "", "admin", "member", True),
        ("group_ban", 1, "admin", "member", True),
        ("group_ban", 30 * 86400, "admin", "member", True),
        ("group_ban", 0, "admin", "member", False),
        ("group_ban", -1, "admin", "member", False),
        ("group_ban", 30 * 86400 + 1, "admin", "member", False),
        ("group_card", "自己的新名片", "member", "member", False),
        ("group_ban", 60, "member", "member", False),
        ("group_card", "自己的新名片", "admin", "unknown", False),
    ],
)
async def test_group_member_self_service_checks_before_write(
    tmp_path, action, value, bot_role, actor_role, success
):
    container = make_container(tmp_path)
    bot = Bot(bot_role=bot_role, actor_role=actor_role)
    result = await GroupAdminService(container.features).execute(
        action, bot=bot, group_id="12345", actor="10001", target="10001", value=value
    )
    assert result["success"] is success
    assert bool(bot.timeline) is success
    if success:
        assert bot.timeline == [
            (
                "set_group_card" if action == "group_card" else "set_group_ban",
                {
                    "group_id": 12345,
                    "user_id": 10001,
                    "card" if action == "group_card" else "duration": value,
                },
            )
        ]


async def test_markdown_renders_formatting_and_blocks_all_remote_images(tmp_path, monkeypatch):
    renderer = RenderService(tmp_path)
    capture = AsyncMock(return_value=tmp_path / "x.png")
    monkeypatch.setattr(renderer, "_render", capture)
    await renderer.render_markdown(
        "**重点**\n\n- 项目\n\n`code`\n\n<script>alert(1)</script>\n\n"
        "![inline](https://invalid.test/x)\n\n![ref][id]\n\n[id]: https://invalid.test/y"
    )
    body = capture.call_args.kwargs["html_body"]
    assert "<strong>重点</strong>" in body
    assert "<li>项目</li>" in body and "<code>code</code>" in body
    assert "<script>" not in body and "<img" not in body


async def test_feature_reports_independent_command_restriction(tmp_path, monkeypatch):
    nonebot.init()
    from amadeus_bot.plugins import chat, feature_admin  # noqa: F401

    container = make_container(tmp_path)
    monkeypatch.setattr(feature_admin, "get_container", lambda: container)
    container.repository.set_command_override("12345", "chat", True, "999")
    enabled = feature_admin._execute(["enable", "chat"], "999", "12345")
    status = feature_admin._execute(["status", "chat"], "999", "12345")
    assert "/enable chat" in enabled and "SUPERUSER" in status
    assert container.repository.command_disabled("12345", "chat")
    assert container.features.status("chat", "12345").enabled
    container.features.set("chat", "12345", False, "999")
    global_enabled = feature_admin._execute(["enable", "chat", "global"], "999", "12345")
    assert "实际开关：OFF" in global_enabled and "/enable chat" in global_enabled


@pytest.mark.parametrize("task", [AITask.CHAT, AITask.IMAGE_GENERATION])
async def test_ai_failure_logs_phase_model_elapsed_and_fallback_without_secrets(tmp_path, task):
    container = make_container(tmp_path)
    catalog = AIRouteCatalog.load(ROUTES_PATH)
    service = AIService(catalog, {}, container.repository)
    service.providers = {
        target.provider: SimpleNamespace(
            complete=AsyncMock(side_effect=httpx.ReadTimeout("https://secret.test/?api_key=DO_NOT_LOG"))
        )
        for target in (catalog.routes[task].primary, *catalog.routes[task].fallbacks)
    }
    logs = []
    sink = logger.add(lambda message: logs.append(str(message)))
    try:
        with pytest.raises(AIInvocationError, match="等待上游响应超时"):
            await service.complete(task, [{"role": "user", "content": "hello"}])
    finally:
        logger.remove(sink)
    output = "\n".join(logs)
    assert f"provider={catalog.routes[task].primary.provider}" in output and "elapsed_ms=" in output
    assert ("fallback" in output) is bool(catalog.routes[task].fallbacks)
    assert "DO_NOT_LOG" not in output and "secret.test" not in output
    assert "HTTP 503" in failure_reason(RuntimeError("HTTP 503 secret"))
    assert "secret" not in failure_reason(RuntimeError("HTTP 503 secret"))


async def test_thinking_api_failure_is_nonfatal_and_cleanup_is_idempotent():
    bot = Bot()
    bot.call_api = AsyncMock(side_effect=RuntimeError("adapter unavailable"))
    thinking = ThinkingReaction(bot, "42")
    await thinking.start()
    await thinking.start()
    await thinking.clear()
    await thinking.clear()
    assert bot.call_api.await_count == 2


async def test_attachment_lookup_precedes_thinking_but_vision_starts_it(tmp_path, monkeypatch):
    nonebot.init()
    from amadeus_bot.plugins import chat

    timeline = []
    container = make_container(tmp_path)
    container.repository.get_media_description = Mock(return_value=None)
    container.repository.save_media_description = Mock()
    bot = Bot(timeline)

    async def lookup(**_kwargs):
        timeline.append("lookup")
        return {"message": [{"type": "image", "data": {"url": "https://invalid.test/test.png"}}]}

    async def vision(*_args, **_kwargs):
        timeline.append("vision")
        return AIResponse(content="测试图片", provider="fake", model="fake")

    bot.get_msg = lookup
    container.ai = SimpleNamespace(complete=vision)
    current = event()
    current.reply = SimpleNamespace(message_id=99)
    thinking = ThinkingReaction(bot, "1")
    await chat._enrich_input(container, bot, current, "看图片", "12345", "10001", before_model=thinking.start)
    await thinking.start()
    await thinking.clear()
    assert timeline[0] == "lookup"
    assert timeline[1] == ("set_msg_emoji_like", {"message_id": 1, "emoji_id": 181, "set": True})
    assert timeline[2] == "vision"
    assert timeline[3] == ("set_msg_emoji_like", {"message_id": 1, "emoji_id": 181, "set": False})


def test_runtime_console_can_write_unicode_when_redirected(monkeypatch):
    nonebot.init()
    from amadeus_bot.plugins import runtime_logging

    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="gbk")
    monkeypatch.setattr(runtime_logging.sys, "stdout", stream)
    runtime_logging._configure_stdout()
    stream.write("AI 日志 ↵ 完成")
    stream.flush()
    assert buffer.getvalue().decode("utf-8") == "AI 日志 ↵ 完成"
