from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import nonebot
import pytest


@pytest.mark.asyncio
async def test_memory_route_outage_pauses_and_recovers(monkeypatch, tmp_path):
    nonebot.init()
    from amadeus_bot.plugins import memory

    key = ("10", "20")
    monkeypatch.setattr(memory, "_message_counts", memory.Counter({key: 11}))
    monkeypatch.setattr(memory, "_running_extractions", set())
    monkeypatch.setattr(memory, "_ai_retry_after", 0.0)
    monkeypatch.setattr(
        memory,
        "AnalyticsService",
        lambda _: SimpleNamespace(
            load_group=lambda *args: SimpleNamespace(
                records=({"user_id": "20", "message_id": "1", "plain_text": "喜欢编程" * 30},)
            )
        ),
    )
    calls = []

    async def complete(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("所有 AI 路由均不可用：deepseek/model:ConnectError")
        return SimpleNamespace(content='[{"category":"preference","content":"编程","confidence":0.9}]')

    candidates = []
    container = SimpleNamespace(
        paths=SimpleNamespace(logs=tmp_path),
        ai=SimpleNamespace(complete=complete),
        memory=SimpleNamespace(
            analysis_enabled=lambda _: True,
            add_candidate=lambda *args, **kwargs: candidates.append((args, kwargs)),
        ),
        features=SimpleNamespace(
            status=lambda *args: SimpleNamespace(enabled=True),
            is_ignored=lambda *args: False,
        ),
    )
    monkeypatch.setattr(memory, "get_container", lambda: container)
    event = SimpleNamespace(
        message_type="group", group_id=10, get_user_id=lambda: "20", get_plaintext=lambda: "你好"
    )

    await memory.handle_memory_batch(event)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(calls) == 1
    assert memory._message_counts[key] == 12
    assert memory._ai_retry_after > time.monotonic()

    await memory.handle_memory_batch(event)
    assert len(calls) == 1
    monkeypatch.setattr(memory, "_ai_retry_after", 0.0)
    await memory.handle_memory_batch(event)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(calls) == 2
    assert len(candidates) == 1
    assert memory._message_counts[key] == 0
