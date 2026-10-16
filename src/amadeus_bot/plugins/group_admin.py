from __future__ import annotations

import shlex

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Message
from nonebot.params import CommandArg

from amadeus_bot.bootstrap import get_container
from amadeus_bot.domain.commands import CommandSpec, command_registry
from amadeus_bot.plugins.common import event_group_id
from amadeus_bot.services.group_admin import GroupAdminService

for name, description, usage in (
    ("groupcard", "修改当前群成员名片", '/groupcard <QQ/@用户> "<新名片>"；空字符串清除'),
    ("mute", "禁言或解除禁言当前群成员", "/mute <QQ/@用户> <秒数>；0 表示解除禁言"),
):
    command_registry.register(
        CommandSpec(
            name=name,
            description=description,
            usage=usage,
            feature="group_admin",
            ai_callable=True,
            notes=(
                "机器人须是本群管理员或群主；普通成员可以修改自己的群名片或禁言自己，但不能自行解禁",
                "操作他人或解除禁言需要本群管理权限；不能管理同级、更高权限成员或机器人自身",
                "仅当前群有效；禁言最多 30 天；以 QQ 接口实际返回为准",
            ),
        )
    )

card_command = on_command("groupcard", priority=10, block=True)
mute_command = on_command("mute", priority=10, block=True)


async def _handle(action, matcher, bot, event, arguments):
    try:
        values = []
        for segment in arguments:
            if segment.type == "at" and str(segment.data.get("qq", "")).isdigit():
                values.append(str(segment.data["qq"]))
            elif segment.type == "text":
                values.extend(shlex.split(segment.data["text"]))
            else:
                raise ValueError("仅支持 QQ/@用户和文字参数")
        if len(values) != 2:
            raise ValueError("请提供目标 QQ/@用户和新名片或禁言秒数")
        value = int(values[1]) if action == "group_ban" else values[1]
    except ValueError as exc:
        await matcher.finish(f"参数错误：{exc}")
    container = get_container()
    result = await GroupAdminService(container.features).execute(
        action,
        bot=bot,
        group_id=event_group_id(event),
        actor=event.get_user_id(),
        target=values[0],
        value=value,
    )
    await container.activity_log.record(
        "group_admin",
        f"群管理 {action} target={values[0]}：" + ("成功" if result["success"] else result["error"]),
        status="success" if result["success"] else "failed",
        user_id=event.get_user_id(),
        group_id=event_group_id(event),
    )
    await matcher.finish("已完成群管理操作。" if result["success"] else result["error"])


@card_command.handle()
async def handle_card(bot: Bot, event, arguments: Message = CommandArg()) -> None:
    await _handle("group_card", card_command, bot, event, arguments)


@mute_command.handle()
async def handle_mute(bot: Bot, event, arguments: Message = CommandArg()) -> None:
    await _handle("group_ban", mute_command, bot, event, arguments)
