from __future__ import annotations

from amadeus_bot.services.event_utils import ai_event_text, event_group_id


def record_inbound(container, event, *, enriched: str | None = None) -> None:
    user_id = event.get_user_id()
    group_id = event_group_id(event)
    # Explicit privacy exclusions still apply; response switches do not affect collection.
    if not container.memory.logging_enabled(user_id) or container.features.is_ignored(
        user_id, group_id, "ai"
    ):
        return
    scope = f"group:{group_id}" if group_id else f"private:{user_id}"
    message_id = str(event.message_id)
    content = enriched if enriched is not None else ai_event_text(event)
    sender = getattr(event, "sender", None)
    label = getattr(sender, "card", None) or getattr(sender, "nickname", None) or user_id
    container.repository.record_conversation_input(
        scope,
        message_id,
        f"[消息 #{message_id}][发送者 QQ {user_id}][昵称 {label}]: {content}",
        user_id,
        enrich=enriched is not None,
    )
