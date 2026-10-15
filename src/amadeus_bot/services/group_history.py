from __future__ import annotations

import asyncio
import json
from datetime import datetime

from amadeus_bot.domain.messages import MessageRecord, SegmentRecord
from amadeus_bot.services.event_utils import onebot_message
from amadeus_bot.services.message_log import MessageLogService


class GroupHistoryService:
    """Seed fresh context from OneBot once per group per process, never on every reconnect."""

    def __init__(self, container, limit: int = 24) -> None:
        self.container = container
        self.limit = limit
        self.loaded: set[str] = set()
        self.context: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def refresh(self, bot) -> None:
        async with self._lock:
            groups = await bot.get_group_list()
            for group in groups:
                group_id = str(group["group_id"])
                if group_id in self.loaded:
                    continue
                try:
                    result = await bot.call_api(
                        "get_group_msg_history",
                        group_id=int(group_id),
                        message_seq=0,
                        count=self.limit,
                    )
                    rows = result.get("messages") if isinstance(result, dict) else None
                    if not isinstance(rows, list):
                        raise ValueError("history response has no messages list")
                    records = list(
                        {
                            record.message_id: record
                            for record in (self._record(row, group_id, str(bot.self_id)) for row in rows)
                        }.values()
                    )
                    records.sort(key=lambda record: record.timestamp)
                    await self._seed(group_id, records[-self.limit :])
                    self.context[group_id] = "\n".join(
                        f"[消息 #{record.message_id}][QQ {record.user_id}]: {self._text(record)}"
                        for record in records[-self.limit :]
                        if self.container.memory.logging_enabled(record.user_id)
                    )[:12_000]
                    self.loaded.add(group_id)
                    await self.container.activity_log.record(
                        "history_sync", f"群历史补拉完成：{len(records)} 条", group_id=group_id
                    )
                except Exception as exc:
                    await self.container.activity_log.record(
                        "history_sync",
                        f"群历史补拉失败：{type(exc).__name__}；继续记录新消息",
                        status="failed",
                        group_id=group_id,
                    )

    @staticmethod
    def _text(record: MessageRecord) -> str:
        return str(onebot_message([{"type": s.type, "data": s.data} for s in record.segments]))[:500]

    @staticmethod
    def _record(row: dict, group_id: str, self_id: str) -> MessageRecord:
        if str(row.get("group_id", group_id)) != group_id:
            raise ValueError("history scope mismatch")
        message_id = str(row.get("message_id") or "")
        user_id = str(row.get("user_id") or (row.get("sender") or {}).get("user_id") or "")
        timestamp = int(row.get("time") or 0)
        if not message_id or not user_id or timestamp <= 0:
            raise ValueError("history message has missing identity or time")
        message = onebot_message(row.get("message"))
        return MessageRecord(
            message_id=message_id,
            direction="outbound" if user_id == self_id else "inbound",
            scene="group",
            user_id=user_id,
            group_id=group_id,
            self_id=self_id,
            plain_text=message.extract_plain_text(),
            timestamp=timestamp,
            segments=tuple(SegmentRecord(segment.type, dict(segment.data)) for segment in message),
        )

    async def _seed(self, group_id: str, records: list[MessageRecord]) -> None:
        container = self.container
        service = MessageLogService(container.paths.logs)
        known: set[str] = set()
        for day in {datetime.fromtimestamp(record.timestamp).strftime("%Y-%m-%d") for record in records}:
            path = container.paths.logs / "messages" / group_id / f"{day}.jsonl"
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        known.add(str(json.loads(line).get("message_id")))
                    except (ValueError, AttributeError):
                        continue
        for record in records:
            if record.message_id in known or not container.memory.logging_enabled(record.user_id):
                continue
            await service.append(record)
            known.add(record.message_id)
