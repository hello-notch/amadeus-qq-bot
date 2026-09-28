import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment

from amadeus_bot.services.courses import CourseRecord
from amadeus_bot.services.event_utils import (
    ai_event_text,
    event_group_id,
    reaction_target_message_id,
    reply_message_id,
)
from amadeus_bot.services.interactions import InteractionPacer, PokeRateLimiter, parse_poke_request
from amadeus_bot.services.message_log import MessageNormalizer
from amadeus_bot.services.renderer import RenderService


@dataclass
class FakeSegment:
    type: str
    data: dict


class FakeEvent:
    message_type = "group"
    group_id = 123
    message_id = 456
    self_id = 789
    time = 1_700_000_000

    def get_user_id(self) -> str:
        return "100"

    def get_plaintext(self) -> str:
        return "hello"

    def get_message(self):
        return [FakeSegment("text", {"text": "hello"}), FakeSegment("image", {"file": "x"})]


def test_render_cache_key_changes_with_content_and_variant(tmp_path: Path) -> None:
    renderer = RenderService(tmp_path)
    first = renderer.cache_key("hello", kind="plain-text", variant="a")
    assert first == renderer.cache_key("hello", kind="plain-text", variant="a")
    assert first != renderer.cache_key("world", kind="plain-text", variant="a")
    assert first != renderer.cache_key("hello", kind="plain-text", variant="b")


def test_help_rows_escape_untrusted_content(tmp_path: Path) -> None:
    body = RenderService._help_overview_html("【校园】\n/portal  🔧\n  查询 <script>\n\n🔧 表示可调用")
    assert "&lt;script&gt;" in body
    assert "<script>" not in body
    assert 'class="help-row"' in body
    assert 'class="help-tool"' in body
    assert "[AI]" not in body


@pytest.mark.asyncio
async def test_timetable_hides_empty_weekend_and_compresses_idle_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    renderer = RenderService(tmp_path)
    captured = {}

    async def capture(content, *, title, variant, kind, html_body):
        captured["html"] = html_body
        return tmp_path / "sample.png"

    monkeypatch.setattr(renderer, "_render", capture)
    course = CourseRecord(1, "早课", 1, 1, 2, "1-16", "N101", "张老师", None)
    later = CourseRecord(2, "午后", 5, 6, 7, "1-16", "N202", "李老师", None)
    await renderer.render_timetable([course, later], week=4, variant="test", pending_count=2)
    body = captured["html"]
    assert body.count('<tr class="busy">') == 4
    assert body.count('<tr class="idle">') == 3
    assert "周五" in body and "周六" not in body
    assert "08:00" in body and "13:00" in body
    assert "本周有 2 门冲突课程待选择" in body
    assert body.count('rowspan="2"') == 2
    assert body.count("早课") == 1 and body.count("午后") == 1
    assert "course-cont" not in body
    assert ">08<" not in body
    weekend = CourseRecord(3, "周末", 6, 1, 2, "1-16", "N303", "王老师", None)
    await renderer.render_timetable([course, weekend], week=4, variant="test")
    assert "周六" in captured["html"] and "周日" in captured["html"]


@pytest.mark.asyncio
async def test_timetable_overlapping_manual_courses_share_one_spanning_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    renderer = RenderService(tmp_path)
    captured = {}

    async def capture(content, *, title, variant, kind, html_body):
        captured["html"] = html_body
        return tmp_path / "sample.png"

    monkeypatch.setattr(renderer, "_render", capture)
    courses = [
        CourseRecord(1, "课程甲", 1, 2, 4, "1-16", "", "", None),
        CourseRecord(2, "课程乙", 1, 3, 5, "1-16", "", "", None),
    ]
    await renderer.render_timetable(courses, week=4, variant="test")
    assert captured["html"].count('rowspan="4"') == 1
    assert captured["html"].count("课程甲") == 1
    assert captured["html"].count("课程乙") == 1


@pytest.mark.asyncio
async def test_activity_render_highlights_status_and_escapes_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    renderer = RenderService(tmp_path)
    captured = {}

    async def capture(content, *, title, variant, kind, html_body):
        captured["html"] = html_body
        return tmp_path / "sample.png"

    monkeypatch.setattr(renderer, "_render", capture)
    await renderer.render_activities(
        [
            {
                "title": "<讲座>",
                "when": "周一",
                "category": "讲座",
                "place": "沙河校区 · 体育馆",
                "statuses": ["需报名", "需签到", "人数已满"],
                "registration": "09:00 至 12:00",
            }
        ],
        subtitle="最近 1 条",
        updated="2026-09-26 18:06 北京时间",
    )
    body = captured["html"]
    assert "&lt;讲座&gt;" in body
    assert "activity-status full" in body
    assert "activity-status need" in body
    assert "报名时间 · 09:00 至 12:00" in body
    assert body.count("体育馆") == 1


def test_message_normalizer_preserves_segments() -> None:
    record = MessageNormalizer.from_onebot_event(FakeEvent())
    assert record.scene == "group"
    assert record.group_id == "123"
    assert record.plain_text == "hello"
    assert [segment.type for segment in record.segments] == ["text", "image"]


def test_temporary_session_with_group_id_remains_private() -> None:
    event = FakeEvent()
    event.message_type = "private"
    record = MessageNormalizer.from_onebot_event(event)
    assert event_group_id(event) is None
    assert record.scene == "private"
    assert record.group_id is None


def test_reply_message_id_prefers_adapter_reply_metadata() -> None:
    event = FakeEvent()
    event.reply = SimpleNamespace(message_id=987654)
    assert reply_message_id(event) == "987654"


def test_reply_message_id_supports_reply_segment_fallback() -> None:
    event = FakeEvent()
    event.get_message = lambda: [FakeSegment("reply", {"id": "42"})]
    assert reply_message_id(event) == "42"


def test_stick_uses_current_message_without_reply_and_prefers_reply_target() -> None:
    event = FakeEvent()
    assert reaction_target_message_id(event) == "456"
    event.reply = SimpleNamespace(message_id=987654)
    assert reaction_target_message_id(event) == "987654"


def test_ai_event_text_preserves_at_qq_in_cq_message() -> None:
    event = FakeEvent()
    event.get_plaintext = lambda: "戳一下 "
    event.get_message = lambda: Message([MessageSegment.text("戳一下 "), MessageSegment.at(2323287290)])

    context = ai_event_text(event)

    assert "纯文本：戳一下 " in context
    assert "CQ消息：戳一下 [CQ:at,qq=2323287290]" in context


def test_parse_poke_request_supports_multiple_mentions_and_qq_numbers() -> None:
    mentions = Message(
        [
            MessageSegment.at(20),
            MessageSegment.text(" "),
            MessageSegment.at(30),
            MessageSegment.text(" 2"),
        ]
    )
    numeric = Message("123456789 987654321 --count 3")

    assert parse_poke_request(mentions).target_user_ids == ("20", "30")
    assert parse_poke_request(mentions).count == 2
    assert parse_poke_request(numeric).target_user_ids == ("123456789", "987654321")
    assert parse_poke_request(numeric).count == 3


def test_poke_rate_limit_counts_all_targets_atomically() -> None:
    limiter = PokeRateLimiter(limit=8)
    assert limiter.allow("100", ["20", "30"], 2, now=100.0) is True
    assert limiter.allow("100", ["40"], 5, now=101.0) is False
    assert limiter.allow("100", ["40"], 5, now=161.0) is True


async def test_interaction_pacer_serializes_poke_and_stick_with_intervals() -> None:
    now = [0.0]
    sleeps: list[float] = []
    timeline: list[tuple[str, str, float]] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay
        await asyncio.sleep(0)

    pacer = InteractionPacer(
        {"poke": 0.3, "stick": 0.3},
        clock=lambda: now[0],
        sleep=fake_sleep,
    )

    async def operate(action: str) -> None:
        async with pacer.slot(action):
            timeline.append(("start", action, now[0]))
            await asyncio.sleep(0)
            timeline.append(("end", action, now[0]))

    await asyncio.gather(operate("poke"), operate("stick"), operate("poke"))

    assert timeline == [
        ("start", "poke", 0.0),
        ("end", "poke", 0.0),
        ("start", "stick", 0.3),
        ("end", "stick", 0.3),
        ("start", "poke", 0.6),
        ("end", "poke", 0.6),
    ]
    assert sleeps == [0.3, 0.3]
