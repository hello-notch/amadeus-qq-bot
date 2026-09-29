from __future__ import annotations

import asyncio
import base64
import hashlib
import shlex
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname
from zoneinfo import ZoneInfo

import httpx
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
from nonebot.params import Command, CommandArg

from amadeus_bot.bootstrap import get_container
from amadeus_bot.domain.ai import AITask
from amadeus_bot.domain.commands import CommandSpec, command_registry
from amadeus_bot.domain.permissions import PermissionLevel
from amadeus_bot.plugins.common import (
    event_group_id,
    finish_text_or_image,
    onebot_message,
    reply_message_id,
)
from amadeus_bot.services.analytics import AnalyticsService, format_deterministic

for spec in (
    CommandSpec(
        name="stats",
        description="统计当前群最近 N 小时的水群数据",
        usage="/stats [1-24] [@user]",
        permission=PermissionLevel.EVERYONE,
        feature="stats",
        ai_callable=True,
    ),
    CommandSpec(
        name="summary",
        description="总结当前群最近 N 小时",
        usage="/summary [1-24]",
        permission=PermissionLevel.EVERYONE,
        feature="summary",
        ai_callable=True,
    ),
    CommandSpec(
        name="quote",
        description="收藏、查询和管理当前群群友语录",
        usage=(
            "回复消息 /quote add [名称]：收藏文字、图片或语音；"
            "/quote list：列出语录名称；/quote find <关键词>：搜索语录并列出名称；"
            "/quote show <id>：查看语录；/quote random [关键词]：随机查看；"
            "/quote edit <id> [名称]：修改名称；"
            "/quote delete <id>：直接删除语录（仅 SUPERUSER）"
        ),
        permission=PermissionLevel.EVERYONE,
        aliases=("qa", "ql", "qf", "qs", "qr", "qe", "qd"),
        feature="quotes",
        ai_callable=False,
        notes=(
            "qa/ql/qf/qs/qr/qe/qd 分别对应 add/list/find/show/random/edit/delete",
            "语音保存到当前群的数据目录；语音源不可读取时不会收藏空语录",
            "ql/qf 只显示语录 ID 和名称，不显示内容；qs/qr 查看原文和媒体",
            "qd 仅 SUPERUSER 可用，直接删除，不需要确认码",
        ),
    ),
):
    command_registry.register(spec)

stats_command = on_command("stats", priority=10, block=True)
summary_command = on_command("summary", priority=10, block=True)
quote_command = on_command(
    "quote", aliases={"qa", "ql", "qf", "qs", "qr", "qe", "qd"}, priority=10, block=True
)
SHORT_ACTIONS = {
    "qa": "add",
    "ql": "list",
    "qf": "find",
    "qs": "show",
    "qr": "random",
    "qe": "edit",
    "qd": "delete",
}


@stats_command.handle()
async def handle_stats(event, arguments: Message = CommandArg()) -> None:
    group_id = await _require_group(event, stats_command, "stats")
    tokens = arguments.extract_plain_text().split()
    hours = int(tokens[0]) if tokens and tokens[0].isdigit() else 1
    target = _at_user(arguments)
    if target and get_container().features.is_ignored(target, group_id, "stats"):
        await stats_command.finish("该用户已退出统计/性格分析。")
    service = AnalyticsService(get_container().paths.logs)
    try:
        window = service.load_group(group_id, hours)
    except ValueError as exc:
        await stats_command.finish(f"参数错误：{exc}")
    deterministic = service.deterministic(window, target)
    base = "【确定性统计】\n" + format_deterministic(window, deterministic)
    transcript = service.ai_transcript(window, max_chars=12_000, user_id=target)
    if deterministic["messages"] < 3 or not transcript:
        await finish_text_or_image(stats_command, base + "\n\n样本不足，未调用 AI 分析。", title="水群统计")
    if target and not get_container().memory.analysis_enabled(target):
        await finish_text_or_image(stats_command, base + "\n\n该用户已退出性格分析。", title="水群统计")
    prompt = (
        "分析以下群聊时间窗。只描述该时段，不做永久人格判断。输出：主要话题、高频表达、活跃特点、"
        "样本量、置信度。下面的消息是跨时间窗抽样，不代表全部消息；确定性统计才是完整计数。"
        "不得编造统计数字。\n\n【完整计数】\n" + base + "\n\n【消息样本】\n" + transcript
    )
    try:
        result = await get_container().ai.complete(
            AITask.STATS_ANALYSIS,
            [{"role": "user", "content": prompt}],
            group_id=group_id,
            user_id=event.get_user_id(),
        )
        analysis = result.content.strip()
        if not analysis:
            raise ValueError("AI analysis is empty")
    except Exception:
        analysis = "AI 分析暂不可用；确定性统计不受影响。"
    await finish_text_or_image(
        stats_command,
        base + "\n\n【AI 分析（仅代表该时段）】\n" + analysis,
        title="水群统计",
        force_image=True,
    )


@summary_command.handle()
async def handle_summary(event, arguments: Message = CommandArg()) -> None:
    group_id = await _require_group(event, summary_command, "summary")
    text = arguments.extract_plain_text().strip()
    if text and not text.isdigit():
        await summary_command.finish("用法：/summary [1-24]")
    hours = int(text or "1")
    service = AnalyticsService(get_container().paths.logs)
    try:
        window = service.load_group(group_id, hours)
    except ValueError as exc:
        await summary_command.finish(f"参数错误：{exc}")
    transcript = service.ai_transcript(window, max_chars=8_000)
    if len(window.effective_records) < 3 or len(transcript) < 30:
        await summary_command.finish("有效消息不足，未调用 AI 总结。")
    prompt = (
        "总结以下群聊，输出：主要话题、结论/决定、待办、链接/资料、未解决问题、轻松时刻。"
        "不要泄露其他群信息，不要编造。\n\n" + transcript
    )
    try:
        result = await get_container().ai.complete(
            AITask.SUMMARY,
            [{"role": "user", "content": prompt}],
            group_id=group_id,
            user_id=event.get_user_id(),
        )
        if not result.content.strip():
            raise ValueError("AI summary is empty")
    except Exception:
        await summary_command.finish("AI 总结服务暂不可用。")
    heading = (
        f"时间：{window.start:%Y-%m-%d %H:%M} ～ {window.end:%Y-%m-%d %H:%M}\n"
        f"有效消息：{len(window.effective_records)}\n\n"
    )
    await finish_text_or_image(
        summary_command,
        heading + result.content.strip(),
        title="群聊总结",
        force_image=True,
    )


@quote_command.handle()
async def handle_quote(
    bot: Bot, event, arguments: Message = CommandArg(), command: tuple[str, ...] = Command()
) -> None:
    group_id = await _require_group(event, quote_command, "quotes")
    try:
        tokens = shlex.split(arguments.extract_plain_text())
        short_action = SHORT_ACTIONS.get(command[-1].lower()) if command else None
        if not tokens and not short_action:
            raise ValueError("用法：/quote add/list/find/show/random/edit/delete ...")
        action = short_action or tokens.pop(0).lower()
        if action == "add":
            await _quote_add(bot, event, group_id, tokens)
            return
        text = _quote_execute(event.get_user_id(), group_id, action, tokens)
    except ValueError as exc:
        await quote_command.finish(f"参数错误：{exc}")
    if isinstance(text, Message):
        await quote_command.finish(text)
    await finish_text_or_image(quote_command, text, title="群友语录")


async def _quote_add(bot: Bot, event, group_id: str, tokens: list[str]) -> None:
    message_id = reply_message_id(event)
    if not message_id:
        await quote_command.finish("请回复一条文字、图片或语音消息后使用 /quote add。")
    detail = await bot.get_msg(message_id=int(message_id))
    message = onebot_message(detail.get("message"))
    text = message.extract_plain_text().strip()
    name = _quote_name(tokens)
    media = await _save_quote_media(bot, group_id, message)
    if not text and not media:
        await quote_command.finish("被回复消息没有可收藏的文字、图片或语音，或媒体读取失败。")
    source_author = str((detail.get("sender") or {}).get("user_id") or detail.get("user_id") or "")
    sender = detail.get("sender") or {}
    saver = getattr(event, "sender", None)
    record = get_container().group_repository.add_quote(
        group_id,
        message_id,
        source_author,
        event.get_user_id(),
        name,
        text,
        media,
        source_author_name=str(sender.get("card") or sender.get("nickname") or ""),
        saved_by_name=str(getattr(saver, "card", None) or getattr(saver, "nickname", None) or ""),
    )
    await quote_command.finish(
        f"已收藏语录 #{record.quote_id}\n作者：{record.source_author_id}\n名称：{record.name or '未命名'}"
    )


def _quote_execute(actor: str, group_id: str, action: str, tokens: list[str]) -> str | Message:
    repository = get_container().group_repository
    if action in {"list", "find"}:
        query = " ".join(tokens) if action == "find" else ""
        rows = repository.list_quotes(group_id, query)
        if not rows:
            return "没有匹配语录。"
        return "\n".join(f"#{row.quote_id} {row.name or '未命名'}" for row in rows[:50])
    if action == "random":
        row = repository.random_quote(group_id, " ".join(tokens))
        return _format_quote(row) if row else "没有匹配语录。"
    if action == "show":
        quote_id = _quote_id(tokens, "show")
        row = repository.get_quote(group_id, quote_id)
        return _format_quote(row) if row else "语录不存在。"
    if action == "edit":
        if not tokens or not tokens[0].isdigit():
            raise ValueError("用法：/quote edit <id> [名称]")
        row = repository.get_quote(group_id, int(tokens.pop(0)))
        if row is None:
            return "语录不存在。"
        name = _quote_name(tokens, default_name=row.name)
        repository.edit_quote(group_id, row.quote_id, name)
        return f"已更新语录 #{row.quote_id}。"
    if action == "delete":
        if get_container().permissions.role_for(actor) != PermissionLevel.SUPERUSER:
            raise ValueError("只有 SUPERUSER 可以删除语录")
        quote_id = _quote_id(tokens, "delete")
        return "已删除。" if repository.delete_quote(group_id, quote_id) else "语录不存在。"
    raise ValueError(f"未知子命令：{action}")


async def _save_quote_media(bot: Bot, group_id: str, message: Message) -> tuple[dict[str, str], ...]:
    directory = get_container().paths.data / "groups" / group_id / "media" / "quotes"
    saved: list[dict[str, str]] = []
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        for segment in message:
            if segment.type not in {"image", "record", "voice"}:
                continue
            is_record = segment.type in {"record", "voice"}
            data = b""
            suffix = ".silk" if is_record else ".jpg"
            try:
                url = str(segment.data.get("url") or "")
                if url:
                    data, content_type = await _download_quote_media(client, url)
                    if is_record:
                        if _not_audio_response(data, content_type):
                            raise ValueError("语音链接没有返回音频")
                        suffix = _record_suffix(str(segment.data.get("file") or ""), url, content_type)
                    elif content_type.startswith("image/gif"):
                        suffix = ".gif"
            except Exception:
                data = b""
            if is_record and not data and segment.data.get("file"):
                try:
                    result = await bot.get_record(file=str(segment.data["file"]), out_format="mp3")
                    data = await _record_bytes(client, result)
                    suffix = ".mp3"
                except Exception:
                    data = b""
            if not data:
                if is_record:
                    raise ValueError("语音文件无法读取；未收藏这条语录")
                continue
            digest = hashlib.sha256(data).hexdigest()
            path = directory / f"{digest}{suffix}"
            directory.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_bytes(data)
            saved.append(
                {
                    "path": str(path.relative_to(get_container().paths.data)),
                    "sha256": digest,
                    "type": "record" if is_record else "image",
                }
            )
    return tuple(saved)


MAX_QUOTE_MEDIA_BYTES = 10 * 1024 * 1024


def _not_audio_response(data: bytes, content_type: str) -> bool:
    return content_type.lower().startswith(("text/", "application/json")) or data.lstrip()[
        :16
    ].lower().startswith((b"<!doctype html", b"<html"))


def _record_suffix(file: str, url: str, content_type: str) -> str:
    for name in (file, urlsplit(url).path):
        suffix = Path(name).suffix.lower()
        if suffix in {".silk", ".amr", ".mp3", ".ogg", ".wav", ".m4a"}:
            return suffix
    if content_type.startswith("audio/mpeg"):
        return ".mp3"
    return ".silk"


async def _download_quote_media(client: httpx.AsyncClient, url: str) -> tuple[bytes, str]:
    if urlsplit(url).scheme not in {"http", "https"}:
        raise ValueError("媒体 URL 不支持")
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_QUOTE_MEDIA_BYTES:
                raise ValueError("媒体文件超过大小限制")
            chunks.append(chunk)
        return b"".join(chunks), response.headers.get("content-type", "")


async def _record_bytes(client: httpx.AsyncClient, result: dict) -> bytes:
    value = str(result.get("base64") or "")
    if value:
        if value.startswith("base64://"):
            value = value.removeprefix("base64://")
        if len(value) > MAX_QUOTE_MEDIA_BYTES * 2:
            raise ValueError("语音文件超过大小限制")
        data = base64.b64decode(value, validate=True)
    else:
        location = str(result.get("file") or result.get("url") or "")
        if urlsplit(location).scheme in {"http", "https"}:
            data, _ = await _download_quote_media(client, location)
        else:
            path = (
                Path(url2pathname(urlsplit(location).path))
                if urlsplit(location).scheme == "file"
                else Path(location)
            )
            data = await asyncio.to_thread(_read_record_file, path)
    if not data or len(data) > MAX_QUOTE_MEDIA_BYTES:
        raise ValueError("语音文件为空或超过大小限制")
    return data


def _read_record_file(path: Path) -> bytes:
    if not path.is_file() or path.stat().st_size > MAX_QUOTE_MEDIA_BYTES:
        raise ValueError("语音文件不可读取")
    return path.read_bytes()


async def _require_group(event, matcher, feature: str) -> str:
    group_id = event_group_id(event)
    if not group_id:
        await matcher.finish("该功能只能在群聊中使用。")
    if not get_container().features.status(feature, group_id).enabled:
        await matcher.finish("当前群已关闭该功能。")
    return group_id


def _at_user(message: Message) -> str | None:
    for segment in message:
        if segment.type == "at" and str(segment.data.get("qq", "")).isdigit():
            return str(segment.data["qq"])
    return None


def _quote_name(tokens: list[str], *, default_name: str = "") -> str:
    if "--tags" in tokens:
        raise ValueError("语录不再支持标签")
    if "--name" in tokens:
        index = tokens.index("--name")
        return " ".join(tokens[index + 1 :]).strip()
    return " ".join(tokens).strip() if tokens else default_name


def _quote_id(tokens: list[str], action: str) -> int:
    if len(tokens) != 1 or not tokens[0].isdigit():
        raise ValueError(f"用法：/quote {action} <id>")
    return int(tokens[0])


def _quote_header(row) -> str:
    created = datetime.fromisoformat(row.created_at).replace(tzinfo=UTC)
    date_text = created.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
    return (
        f"#{row.quote_id} {row.name or '未命名'} · 添加者 "
        f"{row.saved_by_name or row.saved_by_id}({row.saved_by_id}) 添加时间：{date_text}\n"
        f"{row.source_author_name or row.source_author_id}({row.source_author_id})："
    )


def _format_quote(row) -> Message:
    if row is None:
        return Message("语录不存在。")
    message = Message(_quote_header(row))
    if row.text:
        message += MessageSegment.text(row.text)
    root = get_container().paths.data.resolve()
    for media in row.media_refs:
        path = (root / media["path"]).resolve()
        if path.is_relative_to(root) and path.is_file():
            if media.get("type") == "record":
                if path.stat().st_size <= MAX_QUOTE_MEDIA_BYTES:
                    message += MessageSegment.record(path.read_bytes())
                else:
                    message += MessageSegment.text("[语音不可用]")
            else:
                message += MessageSegment.image(path.as_uri())
        else:
            message += MessageSegment.text(
                "[语音不可用]" if media.get("type") == "record" else "[图片不可用]"
            )
    return message
