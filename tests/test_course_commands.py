from pathlib import Path
from types import SimpleNamespace

import nonebot
import pytest

from amadeus_bot.domain.commands import command_registry
from amadeus_bot.domain.permissions import PermissionLevel
from amadeus_bot.repositories.user_data import UserDataRepository
from amadeus_bot.services.courses import CourseService


def _course_plugin():
    nonebot.init()
    from amadeus_bot.plugins import courses

    return courses


def test_course_id_lists_accept_three_separators_and_reject_other_text() -> None:
    plugin = _course_plugin()
    for text in ("12 34", "12,34", "12，34", "12, 34， 12"):
        assert plugin._parse_course_ids(text) == [12, 34]
    for text in ("", "all", "12;34", "12,abc", "12,"):
        with pytest.raises(ValueError, match="课程 ID"):
            plugin._parse_course_ids(text)


@pytest.mark.asyncio
async def test_conflict_image_reply_is_scoped_and_uses_sent_message_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = _course_plugin()
    service = CourseService(UserDataRepository(tmp_path / "users"))
    rows = [
        {"name": name, "weekday": 1, "start_section": 3, "end_section": 4, "weeks": "1-16"}
        for name in ("甲", "乙")
    ]
    service.confirm("100", service.preview_rows("100", rows, "class").token)
    renderer = SimpleNamespace(render_text=_render_fake_image)
    container = SimpleNamespace(
        courses=service,
        renderer=renderer,
        permissions=SimpleNamespace(role_for=lambda actor: PermissionLevel.EVERYONE),
    )
    monkeypatch.setattr(plugin, "get_container", lambda: container)

    class FakeMatcher:
        sent = None

        @classmethod
        async def send(cls, message):
            cls.sent = message
            return {"message_id": 12345}

        @classmethod
        async def finish(cls, message=None):
            raise RuntimeError("finished")

    with pytest.raises(RuntimeError, match="finished"):
        await plugin._finish_course_text(
            FakeMatcher, plugin._format_conflicts("100"), "100", "100", "555", prompt=True
        )
    assert FakeMatcher.sent.type == "image"

    class FakeEvent:
        message_type = "group"
        group_id = 555
        reply = SimpleNamespace(message_id=12345)

        def __init__(self, actor="100", text="1, 2") -> None:
            self.actor = actor
            self.text = text

        def get_user_id(self):
            return self.actor

        def get_plaintext(self):
            return self.text

    assert await plugin._is_conflict_reply(FakeEvent())
    assert not await plugin._is_conflict_reply(FakeEvent(actor="200"))
    assert not await plugin._is_conflict_reply(FakeEvent(text="course choose 1"))
    event = FakeEvent()
    event.group_id = 556
    assert not await plugin._is_conflict_reply(event)


async def _render_fake_image(*_args, **_kwargs) -> Path:
    return Path("test-tmp/fake-conflict.png")


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", [False, True])
async def test_class_import_warns_about_optional_extra_courses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conflict: bool
) -> None:
    plugin = _course_plugin()
    service = CourseService(UserDataRepository(tmp_path / "users"))
    rows = [{"name": "甲", "weekday": 1, "start_section": 3, "end_section": 4, "weeks": "1-16"}]
    if conflict:
        rows.append({"name": "乙", "weekday": 1, "start_section": 3, "end_section": 4, "weeks": "1-16"})

    async def query_class(*_args):
        return "测试班级", rows

    monkeypatch.setattr(plugin.JwglSource, "query_class", query_class)
    monkeypatch.setattr(plugin, "get_container", lambda: SimpleNamespace(courses=service))
    text = await plugin._import_class("100", ["12345"])
    assert ("待选择 1 组冲突" in text) == conflict
    assert "可能包含多余课程" in text
    assert "/course delete <ID>" in text
    assert "没有则无需操作" in text
    assert any("可能包含不修读的多余课程" in note for note in command_registry.get("course").notes)
