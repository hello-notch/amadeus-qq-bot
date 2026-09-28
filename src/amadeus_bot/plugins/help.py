from __future__ import annotations

import asyncio

from nonebot import get_driver, on_command
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.log import logger
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from amadeus_bot.bootstrap import get_container
from amadeus_bot.domain.commands import CommandSpec, command_registry
from amadeus_bot.domain.permissions import PermissionLevel, can_use_role
from amadeus_bot.plugins.common import event_group_id

command_registry.register(
    CommandSpec(
        name="help",
        description="查看按权限和群开关过滤后的帮助",
        usage="help：查看普通命令；help superuser：管理员查看所有命令；help <command>：查看命令详情",
        aliases=("帮助",),
        permission=PermissionLevel.EVERYONE,
        ai_callable=True,
        examples=("help", "/help ddl", "帮助 course"),
        notes=("帮助图片在机器人启动时预先渲染并使用内容缓存",),
    )
)

# COMMAND_START contains an empty prefix, therefore this matcher already covers
# both ``help`` and ``/help``. Registering on_fullmatch as well runs it twice.
help_command = on_command("help", aliases={"帮助"}, priority=10, block=True)

HELP_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("基础与 AI", ("help", "chat", "history", "calc", "health", "md")),
    ("个人事务", ("ddl", "course", "memory", "privacy")),
    ("推荐", ("nowdo", "food", "music")),
    ("群聊内容", ("stats", "summary", "quote")),
    ("群友 wife", ("wife", "changewife", "showwife", "marry")),
    ("校园服务", ("portal", "activity")),
    ("互动", ("stick", "poke")),
    (
        "开发者管理",
        (
            "member",
            "feature",
            "enable",
            "disable",
            "broadcast",
            "data",
            "log",
            "model",
            "ai-cost",
            "ai-quota",
            "say",
        ),
    ),
)


@help_command.handle()
async def handle_help(matcher: Matcher, event, arguments: Message = CommandArg()) -> None:
    target = arguments.extract_plain_text().strip()
    container = get_container()
    role = container.permissions.role_for(event.get_user_id())
    group_id = event_group_id(event)
    if target.lower() == "superuser":
        if role != PermissionLevel.SUPERUSER:
            await matcher.finish("该帮助仅 SUPERUSER 可查看。")
        await _finish_help_image(
            matcher,
            _format_overview(role, None, include_superuser=True),
            title="Amadeus Bot 管理员帮助",
            variant="overview:superuser:all",
        )
        return
    if target:
        spec = command_registry.get(target)
        if (
            spec is None
            or not _visible(spec, role, group_id)
            or (spec.permission == PermissionLevel.SUPERUSER and role != PermissionLevel.SUPERUSER)
        ):
            await matcher.finish(f"没有找到你当前可用的命令：{target}")
        await _finish_help_image(
            matcher,
            _format_detail(spec, group_id),
            title=f"帮助 · /{spec.name}",
            variant=f"detail:{spec.name}",
        )
        return
    await _finish_help_image(
        matcher,
        _format_overview(role, group_id),
        title="Amadeus Bot 帮助",
        variant=f"overview:{role.value}",
    )


@get_driver().on_startup
async def prewarm_help_images() -> None:
    """Render overview variants and every command detail before traffic arrives."""

    container = get_container()
    group_ids = {
        str(row["scope_id"])
        for row in container.repository.get_feature_rows()
        if row.get("scope_type") == "group" and row.get("scope_id")
    }
    group_ids.update(
        str(row["group_id"])
        for row in container.database.fetch_all("SELECT DISTINCT group_id FROM command_overrides")
    )
    # The sentinel represents a group with no per-group overrides. Because the
    # renderer key includes content, ordinary groups reuse this exact image.
    scopes: tuple[str | None, ...] = (None, "__default__", *sorted(group_ids))
    requests: list[tuple[str, str, str]] = []
    for role in (PermissionLevel.EVERYONE, PermissionLevel.MEMBER, PermissionLevel.SUPERUSER):
        for group_id in scopes:
            requests.append((_format_overview(role, group_id), "Amadeus Bot 帮助", f"overview:{role.value}"))
            if role == PermissionLevel.SUPERUSER:
                if group_id is None:
                    requests.append(
                        (
                            _format_overview(role, None, include_superuser=True),
                            "Amadeus Bot 管理员帮助",
                            "overview:superuser:all",
                        )
                    )
    for spec in command_registry.all():
        for group_id in scopes:
            requests.append((_format_detail(spec, group_id), f"帮助 · /{spec.name}", f"detail:{spec.name}"))
    results = await asyncio.gather(
        *(
            container.renderer.render_text(text, title=title, variant=variant)
            for text, title, variant in requests
        ),
        return_exceptions=True,
    )
    for index, result in enumerate(results):
        if isinstance(result, Exception):
            text, title, variant = requests[index]
            try:
                results[index] = await container.renderer.render_text(text, title=title, variant=variant)
            except Exception as exc:
                results[index] = exc
    failures = sum(isinstance(result, Exception) for result in results)
    if failures:
        for index, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning(
                    "帮助预渲染失败 index={} type={} reason={}",
                    index,
                    type(result).__name__,
                    str(result).splitlines()[0][:160],
                )
        logger.warning("帮助图片预渲染有 {} 项失败；/help 不会降级发送整页文字", failures)
    else:
        logger.info("帮助图片预渲染完成：{} 个缓存项", len(results))


async def _finish_help_image(matcher: Matcher, text: str, *, title: str, variant: str) -> None:
    try:
        path = await get_container().renderer.render_text(text, title=title, variant=variant)
    except Exception as exc:
        logger.exception("帮助图片渲染失败")
        await matcher.finish(f"帮助图片暂时无法生成（{type(exc).__name__}），请联系开发者查看日志。")
    await matcher.finish(MessageSegment.image(path.resolve().as_uri()))


def _format_overview(role: PermissionLevel, group_id: str | None, *, include_superuser: bool = False) -> str:
    visible = [
        spec
        for spec in command_registry.all()
        if _visible(spec, role, group_id)
        and (include_superuser or spec.permission != PermissionLevel.SUPERUSER)
    ]
    by_name = {spec.name: spec for spec in visible}
    lines = ["Amadeus Bot · 可用命令", ""]
    included: set[str] = set()
    for title, names in HELP_GROUPS:
        specs = [by_name[name] for name in names if name in by_name]
        if not specs:
            continue
        lines.append(f"【{title}】")
        for spec in specs:
            lines.extend(_overview_lines(spec, group_id))
            included.add(spec.name)
        lines.append("")
    remaining = [spec for spec in visible if spec.name not in included]
    if remaining:
        lines.append("【其他】")
        for spec in remaining:
            lines.extend(_overview_lines(spec, group_id))
        lines.append("")
    lines.extend(
        (
            "🔧 表示该命令的部分能力可由 AI 工具调用。",
            "使用 /help <command> 查看参数、示例、权限和规则。",
            "SUPERUSER 可用 help superuser 查看全部命令。",
            "命令前的 / 可省略。",
        )
    )
    return "\n".join(lines)


def _overview_lines(spec: CommandSpec, group_id: str | None = None) -> list[str]:
    aliases = f"（别名：{'、'.join('/' + item for item in spec.aliases)}）" if spec.aliases else ""
    ai = "  🔧" if spec.ai_callable else ""
    disabled = bool(group_id and spec.name in get_container().repository.disabled_commands(group_id))
    return [
        f"/{spec.name}{ai} {aliases}{' [已关闭]' if disabled else ''}",
        f"  {spec.description}｜{'SUPERUSER（临时）' if disabled else spec.permission.value}",
    ]


def _visible(spec: CommandSpec, role: PermissionLevel, group_id: str | None) -> bool:
    if spec.permission in {PermissionLevel.EVERYONE, PermissionLevel.MEMBER, PermissionLevel.SUPERUSER}:
        if not can_use_role(role, spec.permission):
            return False
    if spec.feature and group_id and not get_container().features.status(spec.feature, group_id).enabled:
        return False
    return True


def _format_detail(spec: CommandSpec, group_id: str | None = None) -> str:
    disabled = bool(group_id and spec.name in get_container().repository.disabled_commands(group_id))
    lines = [
        f"命令：/{spec.name}",
        f"说明：{spec.description}",
        "用法：",
        *("  " + item.strip() for item in spec.usage.replace("；", "\n").splitlines() if item.strip()),
        "",
        f"权限：{'SUPERUSER（当前群已关闭）' if disabled else spec.permission.value}",
        f"AI 调用：{'是' if spec.ai_callable else '否'}",
    ]
    if spec.aliases:
        lines.append("别名：" + "、".join("/" + item for item in spec.aliases))
    if spec.examples:
        lines.append("示例：\n" + "\n".join(f"  {item}" for item in spec.examples))
    if spec.notes:
        lines.append("规则与子命令权限：\n" + "\n".join(f"  • {item}" for item in spec.notes))
    return "\n".join(lines)
