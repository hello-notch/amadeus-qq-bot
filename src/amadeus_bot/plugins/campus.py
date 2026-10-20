from __future__ import annotations

import shlex

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.params import CommandArg

from amadeus_bot.bootstrap import get_container
from amadeus_bot.domain.commands import CommandSpec, command_registry
from amadeus_bot.domain.permissions import PermissionLevel
from amadeus_bot.plugins.common import event_group_id
from amadeus_bot.services.campus import (
    PortalSource,
    format_source_timestamp,
)

for spec in (
    CommandSpec(
        name="portal",
        description="查询和订阅北邮信息门户校内通知",
        usage=(
            "/portal [1-5]：查看通知列表；/portal <wbnewsid>：查看通知详情；"
            "/portal search <关键词> [N]：搜索通知；"
            "/portal sub on/off：订阅本人推送；"
            "/portal group-sub on/off：管理群推送（仅 SUPERUSER）；"
            "/portal refresh：刷新通知（仅 SUPERUSER）"
        ),
        permission=PermissionLevel.EVERYONE,
        feature="portal",
        ai_callable=True,
        examples=("/portal 2", "/portal 123456", "/portal sub on"),
        notes=(
            "列表每页 10 条，最多 5 页；通知 ID 详情才以文字发送可点击链接",
            "sub 管理本人每日私聊推送；group-sub 和 refresh 仅 SUPERUSER 可用",
            "列表来自只读抓取缓存，登录失效时可由 Playwright 自动续登",
        ),
    ),
):
    command_registry.register(spec)

portal_command = on_command("portal", priority=10, block=True)


@portal_command.handle()
async def handle_portal(event, arguments: Message = CommandArg()) -> None:
    await _handle_source("portal", portal_command, event, arguments)


async def _handle_source(source: str, matcher, event, arguments: Message) -> None:
    group_id = event_group_id(event)
    if group_id and not get_container().features.status(source, group_id).enabled:
        await matcher.finish("当前群已关闭该校园数据源。")
    try:
        tokens = shlex.split(arguments.extract_plain_text())
    except ValueError as exc:
        await matcher.finish(f"参数错误：{exc}")
    if tokens and tokens[0] == "refresh":
        if get_container().permissions.role_for(event.get_user_id()) != PermissionLevel.SUPERUSER:
            await matcher.finish("手动刷新仅 SUPERUSER 可用。")
        adapter = PortalSource(get_container().repository)
        try:
            total, new_count = await adapter.refresh()
        except Exception as exc:
            get_container().repository.set_source_health(source, success=False, error_summary=str(exc))
            await matcher.finish(f"刷新失败：{exc}")
        await matcher.finish(f"刷新完成：取得 {total} 条，其中新增 {new_count} 条。")
    if tokens and tokens[0] in {"sub", "group-sub"}:
        await _subscription(source, matcher, event, tokens)
        return
    if source == "portal" and (not tokens or tokens[0] != "search"):
        if len(tokens) > 1 or (tokens and not tokens[0].isdigit()):
            await matcher.finish("用法：/portal [1-5] 或 /portal <wbnewsid>")
        repository = get_container().repository
        if tokens and int(tokens[0]) > 5:
            row = repository.get_source_item("portal", tokens[0])
            if row is None:
                await matcher.finish("未找到该 wbnewsid 对应的缓存通知。")
            await matcher.finish(
                f"{row['title']}\n发送单位：{row.get('department') or '未知'}\n"
                f"日期：{row.get('published_at') or '未知'}\n"
                f"链接：{row.get('url') or '暂无'}"
            )
        page = int(tokens[0]) if tokens else 1
        if not 1 <= page <= 5:
            await matcher.finish("页码只能为 1–5。")
        rows = repository.query_source_items("portal", limit=10, offset=(page - 1) * 10)
        if not rows:
            health = repository.get_source_health("portal")
            if health and health.get("consecutive_failures"):
                await matcher.finish("信息门户最近刷新失败，当前页没有可显示的缓存通知。")
            await matcher.finish("当前页没有缓存通知。")
        health = repository.get_source_health("portal")
        updated = health.get("last_success_at") if health else None
        entries = [
            (
                f"{(page - 1) * 10 + index:02d}",
                row["title"],
                f"{row.get('department') or '单位未知'}  ·  {row.get('published_at') or '日期未知'}"
                f"  ·  wbnewsid {row['item_id']}",
            )
            for index, row in enumerate(rows, 1)
        ]
        path = await get_container().renderer.render_rows(
            entries,
            title="信息门户 · 校内通知",
            subtitle=f"第 {page} 页 / 最多 5 页",
            footer=f"最近更新：{format_source_timestamp(updated)} · /portal <wbnewsid> 查看链接",
            variant=f"portal:page:{page}",
        )
        await matcher.finish(MessageSegment.image(path.resolve().as_uri()))
    query = ""
    limit = 10
    if tokens and tokens[0] == "search":
        if len(tokens) < 2:
            await matcher.finish(f"用法：/{source} search <关键词> [N]")
        query = tokens[1]
        if len(tokens) > 2 and tokens[2].isdigit():
            limit = min(20, int(tokens[2]))
    elif tokens and tokens[0].isdigit():
        limit = min(20, int(tokens[0]))
    rows = get_container().repository.query_source_items(source, query, limit)
    if not rows:
        health = get_container().repository.get_source_health(source)
        suffix = (
            f"最近状态：{health['error_summary']}"
            if health and health["error_summary"]
            else "请让 SUPERUSER 先 refresh。"
        )
        await matcher.finish("暂无缓存数据。" + suffix)
    health = get_container().repository.get_source_health(source)
    updated = health.get("last_success_at") if health else None
    entries = [
        (
            f"{index:02d}",
            row["title"],
            f"{row.get('department') or '单位未知'} · {row.get('published_at') or '日期未知'}"
            f" · wbnewsid {row['item_id']}",
        )
        for index, row in enumerate(rows, 1)
    ]
    path = await get_container().renderer.render_rows(
        entries,
        title="信息门户 · 搜索",
        subtitle=f"匹配 {len(rows)} 条" if query else f"最近 {len(rows)} 条",
        footer=f"最近更新：{format_source_timestamp(updated)}" + " · /portal <wbnewsid> 查看链接",
        variant=f"{source}:results",
    )
    await matcher.finish(MessageSegment.image(path.resolve().as_uri()))


async def _subscription(source: str, matcher, event, tokens: list[str]) -> None:
    action = tokens[0]
    if len(tokens) < 2 or tokens[1] not in {"on", "off"}:
        await matcher.finish(f"用法：/{source} {action} on/off")
    if action == "group-sub":
        if get_container().permissions.role_for(event.get_user_id()) != PermissionLevel.SUPERUSER:
            await matcher.finish("群订阅仅 SUPERUSER 可配置。")
        scope_type, scope_id = "group", event_group_id(event)
        if not scope_id:
            await matcher.finish("group-sub 必须在群聊中使用。")
    else:
        scope_type, scope_id = "user", event.get_user_id()
    get_container().repository.set_subscription(
        source, scope_type, scope_id, tokens[1] == "on", event.get_user_id()
    )
    await matcher.finish(f"已将 {source} {scope_type} 订阅设为 {tokens[1]}。")
