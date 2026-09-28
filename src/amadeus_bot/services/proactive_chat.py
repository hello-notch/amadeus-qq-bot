from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass, field


def proactive_score(text: str) -> int:
    lowered = text.casefold()
    names = ("amadeus", "阿玛迪斯", "助手", "机器人")
    topics = (
        "命运石之门",
        "命運石之門",
        "steins;gate",
        "steins gate",
        "steinsgate",
        "牧濑",
        "牧瀬",
        "红莉栖",
        "紅莉栖",
        "克里斯蒂娜",
        "冈部",
        "岡部",
        "凤凰院",
        "鳳凰院",
        "凶真",
        "真由理",
        "嘟嘟噜",
        "嘟嘟嚕",
        "桥田至",
        "桶子",
        "阿万音",
        "鈴羽",
        "铃羽",
        "世界线",
        "时间机器",
        "d-mail",
        "el psy kongroo",
        "el psy congroo",
    )
    score = 4 if any(name in lowered for name in names) or re.search(r"\bbot\b", lowered) else 0
    if any(topic in lowered for topic in topics):
        score += 2
    if text.endswith(("?", "？")):
        score += 2
    if any(word in text for word in ("有人知道", "怎么", "为什么", "求推荐", "怎么办")):
        score += 1
    return score


def parse_gate_decision(content: str) -> bool:
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", content):
        try:
            decision, _ = decoder.raw_decode(content[match.start() :])
        except json.JSONDecodeError:
            continue
        if not isinstance(decision, dict):
            continue
        confidence = decision.get("confidence")
        if (
            decision.get("respond") is True
            and isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and 0.7 <= confidence <= 1
        ):
            return True
        if "respond" in decision:
            return False
    raise ValueError("Proactive gate returned no complete decision object")


@dataclass
class ProactiveContext:
    messages: deque[tuple[float, str, str]] = field(default_factory=lambda: deque(maxlen=12))
    last_gate: float | None = None

    def observe(self, now: float, user_id: str, text: str) -> None:
        while self.messages and self.messages[0][0] < now - 120:
            self.messages.popleft()
        self.messages.append((now, user_id, text[:600]))

    def conversation_candidate(self, now: float) -> bool:
        return (
            len(self.messages) >= 4
            and len({user for _, user, _ in self.messages}) >= 2
            and (self.last_gate is None or now - self.last_gate >= 45)
        )

    def prompt_context(self) -> str:
        return "\n".join(f"QQ {user}: {text}" for _, user, text in self.messages)


def chat_bubbles(text: str, threshold: int) -> list[str]:
    if len(text) >= threshold or "```" in text:
        return [text]
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if 1 < len(paragraphs) <= 3 and all(len(part) <= 160 for part in paragraphs):
        return paragraphs
    if len(paragraphs) != 1 or "\n" in text or len(text) > 240:
        return [text]
    sentences = [part.strip() for part in re.split(r"(?<=[。！？!?])\s*", text) if part.strip()]
    if 1 < len(sentences) <= 3 and all(len(part) <= 120 for part in sentences):
        return sentences
    return [text]
