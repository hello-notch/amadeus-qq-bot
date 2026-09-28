from __future__ import annotations

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message
from nonebot.params import CommandArg

from amadeus_bot.bootstrap import get_container
from amadeus_bot.domain.commands import CommandSpec, command_registry
from amadeus_bot.domain.permissions import PermissionLevel

command_registry.register(
    CommandSpec(
        name="privacy",
        description="SUPERUSER 查看或设置用户的分析与消息日志隐私开关",
        usage=(
            "/privacy status：查看隐私状态；"
            "/privacy analysis on/off：设置性格分析；"
            "/privacy logging on/off：设置消息日志；"
            "以上命令可附加 --user <QQ>：指定其他用户"
        ),
        permission=PermissionLevel.SUPERUSER,
        ai_callable=False,
    )
)

privacy_command = on_command("privacy", priority=10, block=True)


@privacy_command.handle()
async def handle_privacy(event, arguments: Message = CommandArg()) -> None:
    container = get_container()
    if container.permissions.role_for(event.get_user_id()) != PermissionLevel.SUPERUSER:
        await privacy_command.finish("该命令仅 SUPERUSER 可用。")
    tokens = arguments.extract_plain_text().split()
    try:
        user_id, tokens = _privacy_subject(event.get_user_id(), tokens)
    except ValueError as exc:
        await privacy_command.finish(f"参数错误：{exc}")
    memory = container.memory
    if not tokens or tokens[0] == "status":
        await privacy_command.finish(
            f"性格分析：{'ON' if memory.analysis_enabled(user_id) else 'OFF'}\n"
            f"消息日志：{'ON' if memory.logging_enabled(user_id) else 'OFF'}\n"
            "关闭只影响此后的采集；既有记忆的查看/修改/删除仍需提交开发者申请。"
        )
    if len(tokens) != 2 or tokens[0] not in {"analysis", "logging"} or tokens[1] not in {"on", "off"}:
        await privacy_command.finish("用法：/privacy status | analysis on/off | logging on/off")
    enabled = tokens[1] == "on"
    if tokens[0] == "analysis":
        memory.set_analysis(user_id, enabled)
        if not enabled:
            request_id = container.repository.create_memory_request(user_id, "optout", "privacy command")
            await privacy_command.finish(f"已关闭性格分析，并创建开发者处理申请 #{request_id}。")
    else:
        memory.set_logging(user_id, enabled)
    await privacy_command.finish(f"已将 {tokens[0]} 设为 {tokens[1]}。")


def _privacy_subject(actor: str, tokens: list[str]) -> tuple[str, list[str]]:
    if "--user" not in tokens:
        return actor, tokens
    if tokens.count("--user") != 1:
        raise ValueError("--user 只能使用一次")
    index = tokens.index("--user")
    if index + 1 >= len(tokens) or not tokens[index + 1].isdigit():
        raise ValueError("--user 后必须是 QQ 号")
    return tokens[index + 1], tokens[:index] + tokens[index + 2 :]
