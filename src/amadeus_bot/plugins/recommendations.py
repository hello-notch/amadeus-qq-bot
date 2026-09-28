from __future__ import annotations

import hashlib
from urllib.parse import urlsplit

import httpx
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.params import CommandArg

from amadeus_bot.bootstrap import get_container
from amadeus_bot.domain.commands import CommandSpec, command_registry
from amadeus_bot.domain.permissions import PermissionLevel
from amadeus_bot.plugins.common import event_group_id, finish_text_or_image

POOL_NAMES = {"activity": "干什么", "food": "吃什么", "music": "推歌"}

for spec in (
    CommandSpec(
        name="nowdo",
        aliases=("干什么",),
        description="从可以做的事情中随机推荐，或管理事项池",
        usage="/nowdo [list | add <名称> [图片] | del <名称>]",
        permission=PermissionLevel.EVERYONE,
        feature="recommendation",
        ai_callable=True,
        examples=("/nowdo", "/nowdo list", "/nowdo add 读一本书"),
        notes=("list/add 对所有人开放；del 仅 SUPERUSER",),
    ),
    CommandSpec(
        name="food",
        aliases=("吃什么",),
        description="只从食物池随机推荐",
        usage="/food [list | add <名称> [图片] | del <名称>]",
        permission=PermissionLevel.EVERYONE,
        feature="recommendation",
        ai_callable=True,
        examples=("/food", "/food list", "/food add 蛋挞"),
        notes=("list/add 对所有人开放；del 仅 SUPERUSER",),
    ),
    CommandSpec(
        name="music",
        aliases=("推歌", "听什么"),
        description="只从歌曲池随机推荐",
        usage="/music [list | add <名称> [图片] | del <名称>]",
        permission=PermissionLevel.EVERYONE,
        feature="recommendation",
        ai_callable=True,
        examples=("/music", "/music list", "/music add 歌手的歌曲"),
        notes=("list/add 对所有人开放；del 仅 SUPERUSER",),
    ),
):
    command_registry.register(spec)

nowdo_command = on_command("nowdo", aliases={"干什么"}, priority=10, block=True)
food_command = on_command("food", aliases={"吃什么"}, priority=10, block=True)
music_command = on_command("music", aliases={"推歌", "听什么"}, priority=10, block=True)


@nowdo_command.handle()
async def handle_nowdo(event, arguments: Message = CommandArg()) -> None:
    await _handle(nowdo_command, "activity", event, arguments)


@food_command.handle()
async def handle_food(event, arguments: Message = CommandArg()) -> None:
    await _handle(food_command, "food", event, arguments)


@music_command.handle()
async def handle_music(event, arguments: Message = CommandArg()) -> None:
    await _handle(music_command, "music", event, arguments)


async def _handle(matcher, pool: str, event, arguments: Message) -> None:
    group_id = event_group_id(event)
    container = get_container()
    if group_id and not container.features.status("recommendation", group_id).enabled:
        await matcher.finish("当前群已关闭推荐功能。")
    text = arguments.extract_plain_text().strip()
    action, _, content = text.partition(" ")
    repository = container.repository
    if action == "list":
        if content.strip():
            await matcher.finish("list 不需要参数。")
        items = repository.list_recommendations(pool)
        listing = "\n".join(f"#{item.recommendation_id} {item.content}" for item in items)
        await finish_text_or_image(matcher, listing or "没有条目。", title=f"{POOL_NAMES[pool]}推荐池")
        return
    if action == "add":
        content = content.strip()
        if not content:
            await matcher.finish("add 后请输入推荐名称。")
        images = [segment for segment in event.get_message() if segment.type == "image"]
        if len(images) > 1:
            await matcher.finish("每条推荐最多一张图片。")
        try:
            image_path = await _save_image(images[0]) if images else ""
        except (httpx.HTTPError, ValueError):
            await matcher.finish("图片读取失败，推荐未添加。")
        item_id = repository.add_recommendation(
            pool, "", content, 1, (), event.get_user_id(), image_path=image_path
        )
        await matcher.finish(f"已添加 #{item_id} {content}。")
    if action == "del":
        content = content.strip()
        if not content:
            await matcher.finish("del 后请输入推荐名称。")
        if container.permissions.role_for(event.get_user_id()) != PermissionLevel.SUPERUSER:
            await matcher.finish("只有 SUPERUSER 可以删除推荐。")
        matches = [item for item in repository.list_recommendations(pool) if item.content == content]
        if not matches:
            await matcher.finish("没有找到该名称的推荐。")
        if len(matches) > 1:
            await matcher.finish("存在多个同名条目，无法按名称唯一删除。")
        repository.delete_recommendation(matches[0].recommendation_id)
        await matcher.finish(f"已删除 #{matches[0].recommendation_id} {content}。")
    if text:
        await matcher.finish("用法：命令 [list | add <名称> [图片] | del <名称>]")
    item = repository.choose_recommendation(pool)
    if item is None:
        await matcher.finish("推荐池暂时没有条目。")
    prefix = {"activity": "可以做：", "food": "可以吃：", "music": "可以听"}[pool]
    message = Message(f"{prefix}{item.content}\n添加者：{item.creator_id}")
    if item.image_path:
        root = container.paths.data.resolve()
        path = (root / item.image_path).resolve()
        if path.is_relative_to(root) and path.is_file():
            message += MessageSegment.image(path.as_uri())
        else:
            message += MessageSegment.text("\n[图片不可用]")
    await matcher.finish(message)


async def _save_image(segment: MessageSegment) -> str:
    url = str(segment.data.get("url") or "")
    if urlsplit(url).scheme not in {"http", "https"}:
        raise ValueError("图片链接无效")
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > 10 * 1024 * 1024:
                    raise ValueError("图片超过 10 MiB")
            if not response.headers.get("content-type", "").lower().startswith("image/") or not data:
                raise ValueError("链接未返回图片")
    suffix = ".gif" if data[:6] in (b"GIF87a", b"GIF89a") else ".jpg"
    digest = hashlib.sha256(data).hexdigest()
    root = get_container().paths.data
    path = root / "shared" / "recommendations" / f"{digest}{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(data)
    return str(path.relative_to(root))
