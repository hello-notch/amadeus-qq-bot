from __future__ import annotations

import uuid

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER

from amadeus_bot.bootstrap import get_container
from amadeus_bot.domain.ai import ModelTarget
from amadeus_bot.domain.commands import CommandSpec, command_registry
from amadeus_bot.domain.permissions import PermissionLevel
from amadeus_bot.plugins.common import event_group_id, finish_text_or_image
from amadeus_bot.services.ai_router import AIService

command_registry.register(
    CommandSpec(
        name="model",
        description="查看、列出或切换当前 AI 模型",
        usage="/model list：列出配置模型；/model switch <模型名或供应商/模型名>：切换首选模型",
        permission=PermissionLevel.SUPERUSER,
        ai_callable=False,
        examples=("/model", "/model list", "/model switch deepseek-v4-pro"),
        notes=(
            "当前模型是本次运行中最近成功响应聊天的模型，包含回退结果",
            "所有配置模型都显示，凭据未匹配的模型不能切换",
            "switch 的选择会持久化，重启后仍然生效",
            "首选模型调用失败时仍按任务路由回退",
        ),
    )
)

model_command = on_command("model", permission=SUPERUSER, priority=5, block=True)


@model_command.handle()
async def handle_model(event, arguments: Message = CommandArg()) -> None:
    container = get_container()
    tokens = arguments.extract_plain_text().split()
    if not tokens:
        await model_command.finish(_format_model_status(*container.ai.chat_model_status()))
    action = tokens[0].casefold()
    if action == "list" and len(tokens) == 1:
        await finish_text_or_image(
            model_command,
            _format_model_list(container.ai),
            title="配置的 AI 模型",
        )
    if action == "switch" and len(tokens) == 2:
        try:
            target = container.ai.switch_model(tokens[1], event.get_user_id())
        except ValueError as exc:
            await model_command.finish(f"切换失败：{exc}\n使用 /model list 查看可用模型。")
        trace_id = uuid.uuid4().hex
        container.repository.record_audit(
            trace_id,
            "model.switch",
            event.get_user_id(),
            "success",
            group_id=event_group_id(event),
            parameter_summary=target.qualified_name,
        )
        await model_command.finish(
            f"已切换首选模型为 {target.qualified_name}。\n"
            f"后续聊天会优先尝试该模型；调用失败时按任务路由回退。\n审计 ID：{trace_id}"
        )
    await model_command.finish("用法：/model [list | switch <model_name>]")


def _format_model_status(current: ModelTarget | None, preferred: ModelTarget) -> str:
    actual = current.qualified_name if current else "本次运行尚未成功调用聊天模型"
    return f"当前模型：{actual}\n首选模型：{preferred.qualified_name}"


def _format_model_list(ai: AIService) -> str:
    current, preferred = ai.chat_model_status()
    models = ai.catalog.model_targets()
    lines = [_format_model_status(current, preferred), "", f"配置模型（{len(models)}）："]
    provider = None
    for target in models:
        if target.provider != provider:
            provider = target.provider
            lines.append(f"[{provider}]")
        markers = [ai.model_status(target)]
        if target == current:
            markers.append("当前")
        if target == preferred:
            markers.append("首选")
        lines.append(f"- {target.model}（{'、'.join(markers)}）")
    lines.extend(("", "切换：/model switch <模型名或供应商/模型名>"))
    return "\n".join(lines)
