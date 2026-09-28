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

command_registry.register(
    CommandSpec(
        name="model",
        description="查看、列出或切换当前 AI 模型",
        usage="/model list：列出模型；/model switch <model_name>：切换模型",
        permission=PermissionLevel.SUPERUSER,
        ai_callable=False,
        examples=("/model", "/model list", "/model switch deepseek-v4-pro"),
        notes=(
            "不带参数时显示当前模型",
            "switch 的选择会持久化，重启后仍然生效",
            "当前模型调用失败时仍按任务路由回退",
        ),
    )
)

model_command = on_command("model", permission=SUPERUSER, priority=5, block=True)


@model_command.handle()
async def handle_model(event, arguments: Message = CommandArg()) -> None:
    container = get_container()
    tokens = arguments.extract_plain_text().split()
    if not tokens:
        await model_command.finish(f"当前模型：{container.ai.current_model().qualified_name}")
    action = tokens[0].casefold()
    if action == "list" and len(tokens) == 1:
        await finish_text_or_image(
            model_command,
            _format_model_list(container.ai.available_models(), container.ai.current_model()),
            title="可用 AI 模型",
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
            f"已切换当前模型为 {target.qualified_name}。\n"
            f"后续 AI 任务将优先使用该模型；调用失败时按任务路由回退。\n审计 ID：{trace_id}"
        )
    await model_command.finish("用法：/model [list | switch <model_name>]")


def _format_model_list(models: tuple[ModelTarget, ...], current: ModelTarget) -> str:
    lines = [f"当前模型：{current.qualified_name}", "", f"可用模型（{len(models)}）："]
    provider = None
    for target in models:
        if target.provider != provider:
            provider = target.provider
            lines.append(f"[{provider}]")
        marker = "（当前）" if target == current else ""
        lines.append(f"- {target.model}{marker}")
    lines.extend(("", "切换：/model switch <模型名>"))
    return "\n".join(lines)
