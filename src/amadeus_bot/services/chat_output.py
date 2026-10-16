from __future__ import annotations

import re

from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.log import logger

from amadeus_bot.services.ai_errors import failure_reason


async def mention_message(text: str, bot, group_id: str | None) -> Message:
    message = Message()
    # Code, email addresses, @all, and every other CQ segment stay literal.
    parts = re.split(r"(```[\s\S]*?```|`[^`\n]*`)", text)
    pattern = re.compile(r"\[CQ:at,qq=([1-9][0-9]{4,14})\]|(?<![\w@])@([1-9][0-9]{4,14})(?![\w])")
    members: set[str] = set()
    if group_id and any(pattern.search(part) for part in parts[::2]):
        try:
            rows = await bot.get_group_member_list(group_id=int(group_id))
            members = {str(row["user_id"]) for row in rows}
        except Exception as exc:
            logger.warning("AI @目标校验失败 group={} error={}", group_id, type(exc).__name__)
    for index, part in enumerate(parts):
        cursor = 0
        if index % 2 == 0:
            for match in pattern.finditer(part):
                target = match[1] or match[2]
                message += MessageSegment.text(part[cursor : match.start()])
                message += MessageSegment.at(target) if target in members else MessageSegment.text(match[0])
                cursor = match.end()
        message += MessageSegment.text(part[cursor:])
    return message


class ThinkingReaction:
    def __init__(self, bot, message_id: str | None) -> None:
        self.bot = bot
        self.message_id = message_id
        self.active = False

    async def start(self) -> None:
        if self.message_id is None:
            return
        # Mark before awaiting so cancellation during the API call still attempts cleanup.
        self.active = True
        await self._set(True)

    async def clear(self) -> None:
        if self.active:
            await self._set(False)
            self.active = False

    async def _set(self, value: bool) -> None:
        try:
            await self.bot.call_api(
                "set_msg_emoji_like", message_id=int(self.message_id), emoji_id=181, set=value
            )
        except Exception as exc:
            logger.warning(
                "AI 思考表情操作失败 message={} set={} error={} reason={}",
                self.message_id,
                value,
                type(exc).__name__,
                failure_reason(exc),
            )
