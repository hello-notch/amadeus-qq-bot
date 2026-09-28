from __future__ import annotations

import asyncio
import shlex
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from nonebot.adapters.onebot.v11 import Message

MAX_POKES_PER_MINUTE = 8
POKE_INTERVAL_SECONDS = 0.3
STICK_INTERVAL_SECONDS = 0.3


@dataclass(frozen=True, slots=True)
class PokeRequest:
    target_user_ids: tuple[str, ...]
    count: int

    @property
    def total(self) -> int:
        return len(self.target_user_ids) * self.count


def parse_poke_request(message: Message) -> PokeRequest:
    """Parse multiple @ mentions/QQ numbers and an optional repeat count."""

    text = message.extract_plain_text().replace(",", " ").replace("，", " ")
    tokens = shlex.split(text)
    count: int | None = None
    positional: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--count":
            if count is not None or index + 1 >= len(tokens) or not tokens[index + 1].isdigit():
                raise ValueError("--count 后必须提供 1～5 的整数")
            count = int(tokens[index + 1])
            index += 2
            continue
        if not token.isdigit():
            raise ValueError("对象必须是 @群友 或 QQ 号")
        positional.append(token)
        index += 1

    mentioned = [
        str(segment.data["qq"])
        for segment in message
        if segment.type == "at" and str(segment.data.get("qq", "")).isdigit()
    ]
    if count is None and positional:
        possible_count = int(positional[-1])
        if 1 <= possible_count <= 5 and (mentioned or len(positional) > 1):
            count = possible_count
            positional.pop()
    count = count or 1
    if not 1 <= count <= 5:
        raise ValueError("每个对象的次数必须在 1～5 之间")

    targets = tuple(dict.fromkeys([*mentioned, *positional]))
    if not targets:
        raise ValueError("至少需要一个 @群友 或 QQ 号")
    if len(targets) * count > MAX_POKES_PER_MINUTE:
        raise ValueError(f"单次最多执行 {MAX_POKES_PER_MINUTE} 次戳一戳")
    return PokeRequest(targets, count)


class PokeRateLimiter:
    def __init__(self, limit: int = MAX_POKES_PER_MINUTE) -> None:
        self.limit = limit
        self._calls: defaultdict[str, deque[float]] = defaultdict(deque)

    def allow(
        self,
        actor: str,
        target_user_ids: tuple[str, ...] | list[str],
        count: int,
        *,
        now: float | None = None,
    ) -> bool:
        current = time.monotonic() if now is None else now
        targets = tuple(dict.fromkeys(str(target) for target in target_user_ids))
        actor_calls = len(targets) * count
        increments = {f"actor:{actor}": actor_calls}
        increments.update({f"target:{target}": count for target in targets})
        for key, increment in increments.items():
            queue = self._calls[key]
            while queue and queue[0] < current - 60:
                queue.popleft()
            if len(queue) + increment > self.limit:
                return False
        for key, increment in increments.items():
            self._calls[key].extend([current] * increment)
        return True


class InteractionPacer:
    """Serialize risk-sensitive OneBot actions and leave a gap between calls."""

    def __init__(
        self,
        intervals: dict[str, float] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.intervals = intervals or {
            "poke": POKE_INTERVAL_SECONDS,
            "stick": STICK_INTERVAL_SECONDS,
        }
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last_completed_at: float | None = None

    @asynccontextmanager
    async def slot(self, action: str) -> AsyncIterator[None]:
        if action not in self.intervals:
            raise ValueError(f"未知互动操作：{action}")
        async with self._lock:
            interval = max(0.0, float(self.intervals[action]))
            if self._last_completed_at is not None:
                remaining = interval - (self._clock() - self._last_completed_at)
                if remaining > 0:
                    await self._sleep(remaining)
            try:
                yield
            finally:
                self._last_completed_at = self._clock()


poke_rate_limiter = PokeRateLimiter()
interaction_pacer = InteractionPacer()
