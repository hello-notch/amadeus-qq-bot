from __future__ import annotations

from nonebot import on_command, on_message
from nonebot.adapters.onebot.v11 import Message
from nonebot.params import CommandArg
from nonebot.rule import Rule

from amadeus_bot.bootstrap import get_container
from amadeus_bot.domain.commands import CommandSpec, command_registry
from amadeus_bot.domain.permissions import PermissionLevel
from amadeus_bot.plugins.common import event_group_id

for action in ("enable", "disable"):
    command_registry.register(
        CommandSpec(
            name=action,
            description="恢复群内命令权限" if action == "enable" else "将群内普通命令临时限为管理员使用",
            usage=f"/{action} <命令>",
            permission=PermissionLevel.SUPERUSER,
            examples=(f"/{action} chat",),
            notes=("仅在当前群生效；只接受原权限为 EVERYONE 的命令或别名",),
        )
    )

enable_command = on_command("enable", priority=5, block=True)
disable_command = on_command("disable", priority=5, block=True)


async def is_disabled_command(event) -> bool:
    group_id = event_group_id(event)
    if not group_id:
        return False
    text = event.get_plaintext().strip()
    if not text:
        return False
    word = text.split(maxsplit=1)[0].removeprefix("/")
    spec = command_registry.get(word)
    return bool(
        spec
        and spec.permission == PermissionLevel.EVERYONE
        and get_container().repository.command_disabled(group_id, spec.name)
        and get_container().permissions.role_for(event.get_user_id()) != PermissionLevel.SUPERUSER
    )


override_guard = on_message(rule=Rule(is_disabled_command), priority=2, block=True)


async def _set_override(event, arguments: Message, disabled: bool, matcher) -> None:
    group_id = event_group_id(event)
    if not group_id:
        await matcher.finish("只能在群聊中设置命令权限。")
    if get_container().permissions.role_for(event.get_user_id()) != PermissionLevel.SUPERUSER:
        await matcher.finish("只有 SUPERUSER 可以设置命令权限。")
    names = arguments.extract_plain_text().split()
    spec = command_registry.get(names[0]) if len(names) == 1 else None
    if spec is None or spec.permission != PermissionLevel.EVERYONE:
        await matcher.finish("请输入一个原权限为 EVERYONE 的命令。")
    get_container().repository.set_command_override(group_id, spec.name, disabled, event.get_user_id())
    await matcher.finish(f"已{'关闭' if disabled else '开启'}/{spec.name}（当前群）。")


@enable_command.handle()
async def handle_enable(event, arguments: Message = CommandArg()) -> None:
    await _set_override(event, arguments, False, enable_command)


@disable_command.handle()
async def handle_disable(event, arguments: Message = CommandArg()) -> None:
    await _set_override(event, arguments, True, disable_command)


@override_guard.handle()
async def guard_disabled_command(event) -> None:
    text = event.get_plaintext().strip()
    word = text.split(maxsplit=1)[0].removeprefix("/")
    spec = command_registry.get(word)
    await override_guard.finish(f"[{spec.name}]被关闭了~")
