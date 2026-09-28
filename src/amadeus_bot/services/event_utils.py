from __future__ import annotations

from typing import Any

from nonebot.adapters.onebot.v11 import Message, MessageSegment


def event_group_id(event: Any) -> str | None:
    """Return a group id only for an actual group message event.

    NapCat temporary-session private messages can carry an auxiliary
    ``group_id``.  Treating that value as the conversation scene leaks
    group-only commands and data into a private session.
    """

    if getattr(event, "message_type", None) != "group":
        return None
    group_id = getattr(event, "group_id", None)
    return str(group_id) if group_id is not None else None


def reply_message_id(event: Any) -> str | None:
    """Return the target id of a OneBot V11 reply.

    The OneBot adapter removes the reply segment from ``event.message`` and
    stores the fetched source message on ``event.reply``.  The segment fallback
    keeps compatibility with synthetic events and other compatible adapters.
    """

    reply = getattr(event, "reply", None)
    message_id = getattr(reply, "message_id", None)
    if message_id is not None:
        return str(message_id)
    message = event.get_message() if hasattr(event, "get_message") else Message()
    for segment in message:
        if segment.type == "reply" and segment.data.get("id") is not None:
            return str(segment.data["id"])
    return None


def reaction_target_message_id(event: Any) -> str | None:
    """Use the replied-to message when present, otherwise the command message itself."""

    replied = reply_message_id(event)
    if replied is not None:
        return replied
    current = getattr(event, "message_id", None)
    return str(current) if current is not None else None


def onebot_message(value: Any) -> Message:
    """Normalize the different ``get_msg`` message payload shapes.

    NapCat returns a list for multi-segment messages, but may return one bare
    segment mapping for a message that only contains a file.  ``Message`` does
    not accept that bare mapping directly.
    """

    if value is None:
        return Message()
    if isinstance(value, dict):
        value = [value]
    if isinstance(value, (list, tuple)):
        value = [
            MessageSegment(str(item.get("type") or "text"), dict(item.get("data") or {}))
            if isinstance(item, dict)
            else item
            for item in value
        ]
    return Message(value)


def cq_message_text(value: Any) -> str:
    """Render a OneBot message as CQ text so non-text segments remain visible to AI."""

    return str(onebot_message(value))


def ai_event_text(event: Any, plain_text: str | None = None) -> str:
    """Build the text submitted to AI with both plaintext and the original CQ message."""

    plain = event.get_plaintext() if plain_text is None else plain_text
    cq_text = cq_message_text(event.get_message())
    parts = [f"纯文本：{plain}", f"CQ消息：{cq_text}"]
    replied = reply_message_id(event)
    if replied is not None:
        parts.append(f"回复目标消息 ID：{replied}")
    return "\n".join(parts)
