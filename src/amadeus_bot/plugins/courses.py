from __future__ import annotations

import re
import shlex
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from nonebot import on_command, on_message
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
from nonebot.params import CommandArg
from nonebot.rule import Rule

from amadeus_bot.bootstrap import get_container
from amadeus_bot.domain.commands import CommandSpec, command_registry
from amadeus_bot.domain.permissions import PermissionLevel
from amadeus_bot.plugins.common import (
    event_group_id,
    finish_text_or_image,
    onebot_message,
    reply_message_id,
)
from amadeus_bot.services.courses import normalize_weeks, parse_sections, parse_weekday
from amadeus_bot.services.jwgl import JwglSource

command_registry.register(
    CommandSpec(
        name="course",
        description="导入、查询、编辑课程表并设置私聊提醒",
        usage=(
            "/course [show]：查看本周课表；/course import <班级号> [学年学期]：导入班级课表；"
            "回复表格 /course import：导入本人课表文件；"
            "/course conflicts [页码]：查看冲突候选；/course choose <ID[,ID...]>：选择冲突课程；"
            "/course list [周次]：列出指定周课程；"
            "/course today：查看今日课程；/course tomorrow：查看明日课程；"
            "/course add <课程名> <星期> <节次> <周次> [地点] [教师]：添加课程；"
            "/course edit <id> --name/--weekday/--sections/--weeks/--location/--teacher <值>：编辑课程；"
            "/course delete <ID[,ID...]|all>：删除课程；"
            "/course remind <id/all> <提前分钟/off>：设置提醒"
        ),
        permission=PermissionLevel.EVERYONE,
        feature="course",
        ai_callable=True,
        examples=(
            "/course import 2024218601",
            "回复教务课表文件后 /course import",
            "/course choose 123",
            "回复冲突提示图片：123, 456",
            "/course delete 123，456",
            "/course delete all",
            "/course today",
            "/course add 高等数学 一 1-2 1-16 教三-101 张老师",
            "/course remind all 20",
        ),
        notes=(
            "import 会在成功解析后清空本人原课表并替换；抓取、解析失败时保留原课表",
            "回复文件时导入不超过 10 MiB 的 CSV/XLS/XLSX；带班级号时导入班级课表",
            "按班级号导入可能包含不修读的多余课程；请核对课表，确有多余课程时用 /course delete <ID> 删除",
            "XLS/XLSX 必须使用教务系统直接下载的个人课表，自行制作或另存的表格可能无法识别",
            "冲突课程暂不显示或提醒；可回复冲突图片输入一个或多个 ID，也可用 choose",
            "choose/delete 的多个 ID 可用空格、中英文逗号分隔；冲突的多个 choose ID 会整体拒绝",
            "重复的课程行会合并；导入与删除本人课表无需二次确认",
            "默认只操作本人；仅 SUPERUSER 可用 --user <QQ> 指定其他用户",
            "课程提醒通过私聊发送，依赖 .env 中的学期开始日期和节次时间",
        ),
    )
)

course_command = on_command("course", priority=10, block=True)


async def _is_conflict_reply(event) -> bool:
    reply_id = reply_message_id(event)
    if not reply_id or not event.get_plaintext().strip():
        return False
    if re.match(r"^/?course(?:\s|$)", event.get_plaintext().strip(), re.I):
        return False
    actor = event.get_user_id()
    superuser = get_container().permissions.role_for(actor) == PermissionLevel.SUPERUSER
    return (
        get_container().courses.conflict_reply_subject(
            actor, reply_id, event_group_id(event), is_superuser=superuser
        )
        is not None
    )


conflict_reply = on_message(rule=Rule(_is_conflict_reply), priority=9, block=True)


@conflict_reply.handle()
async def handle_conflict_reply(event) -> None:
    group_id = event_group_id(event)
    if group_id and not get_container().features.status("course", group_id).enabled:
        await conflict_reply.finish("当前群已关闭课程表功能。")
    actor = event.get_user_id()
    subject = get_container().courses.conflict_reply_subject(
        actor,
        reply_message_id(event) or "",
        group_id,
        is_superuser=get_container().permissions.role_for(actor) == PermissionLevel.SUPERUSER,
    )
    if subject is None:
        return
    try:
        text = _choose_text(subject, _parse_course_ids(event.get_plaintext()))
    except ValueError as exc:
        await conflict_reply.finish(f"参数错误：{exc}")
    await _finish_course_text(conflict_reply, text, actor, subject, group_id, prompt=True)


@course_command.handle()
async def handle_course(bot: Bot, event, arguments: Message = CommandArg()) -> None:
    group_id = event_group_id(event)
    if group_id and not get_container().features.status("course", group_id).enabled:
        await course_command.finish("当前群已关闭课程表功能。")
    actor = event.get_user_id()
    try:
        tokens = shlex.split(arguments.extract_plain_text())
        subject, tokens = _extract_subject(actor, tokens)
        action = tokens.pop(0).lower() if tokens else "show"
        if action == "show":
            if tokens:
                raise ValueError("用法：/course [show]")
            service = get_container().courses
            week = service.current_week()
            rows = service.week_courses(subject, week)
            pending = [row for row in service.pending_courses(subject) if week in normalize_weeks(row.weeks)]
            if not rows:
                if pending:
                    await course_command.finish(
                        f"第 {week} 周课程仍有 {len(pending)} 门待选择；"
                        "请用 /course conflicts 查看候选，再发送 /course choose <ID>。"
                    )
                await course_command.finish(f"第 {week} 周没有课程。")
            path = await get_container().renderer.render_timetable(
                rows, week=week, variant=f"course:week:{subject}:{week}", pending_count=len(pending)
            )
            await course_command.finish(MessageSegment.image(path.resolve().as_uri()))
        if action == "import":
            if reply_message_id(event):
                if tokens:
                    raise ValueError("回复文件导入时不要同时指定班级号")
                text = await _import_file(bot, event, subject)
            else:
                text = await _import_class(subject, tokens)
        else:
            text = _execute(subject, action, tokens)
    except ValueError as exc:
        await course_command.finish(f"参数错误：{exc}")
    except RuntimeError as exc:
        await course_command.finish(f"数据源错误：{exc}")
    await _finish_course_text(
        course_command,
        text,
        actor,
        subject,
        group_id,
        prompt=action in {"import", "conflicts", "choose"},
    )


async def _finish_course_text(
    matcher, text: str, actor: str, subject: str, group_id: str | None, *, prompt: bool
) -> None:
    if prompt and get_container().courses.conflict_groups(subject):
        try:
            path = await get_container().renderer.render_text(
                text, title="课程选课冲突", variant=f"course-conflict:{subject}"
            )
        except Exception:
            await matcher.finish(text + "\n图片暂不可用，请使用 /course choose <ID> 选课。")
        result = await matcher.send(MessageSegment.image(path.resolve().as_uri()))
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if message_id is not None:
            get_container().courses.register_conflict_prompt(actor, subject, str(message_id), group_id)
        await matcher.finish()
    await finish_text_or_image(matcher, text, title="课程表", force_image=len(text) > 500)


async def _import_class(user_id: str, tokens: list[str]) -> str:
    if not 1 <= len(tokens) <= 2 or not tokens[0].isdigit():
        raise ValueError("用法：/course import <班级号> [学年学期]，或回复课表文件后 /course import")
    display, rows = await JwglSource().query_class(tokens[0], tokens[1] if len(tokens) > 1 else "")
    service = get_container().courses
    preview = service.preview_rows(user_id, rows, f"jwgl:{display}")
    _, count = service.confirm(user_id, preview.token)
    return (
        f"已匹配班级：{display}\n已替换课表，导入 {count} 门去重课程。\n"
        + _format_conflicts(user_id)
        + "\n提醒：按班级号导入可能包含多余课程。请用 /course list 核对；"
        "若有不修读的课程，可用 /course delete <ID> 手动删除；没有则无需操作。"
    )


async def _import_file(bot: Bot, event, user_id: str) -> str:
    reply_id = reply_message_id(event)
    if not reply_id:
        raise ValueError("请回复教务系统下载的 .xls/.xlsx 课表或 .csv 文件后使用 /course import")
    detail = await bot.get_msg(message_id=int(reply_id))
    message = onebot_message(detail.get("message"))
    segment = next((item for item in message if item.type == "file"), None)
    if segment is None or not segment.data.get("url"):
        raise ValueError("被回复消息中没有可下载文件")
    filename = str(segment.data.get("name") or segment.data.get("file") or "course.csv")
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(str(segment.data["url"]))
        response.raise_for_status()
    if len(response.content) > 10 * 1024 * 1024:
        raise ValueError("文件不能超过 10 MiB")
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        preview = get_container().courses.preview_csv(user_id, response.content, filename)
    elif suffix in {".xls", ".xlsx"}:
        with tempfile.TemporaryDirectory(prefix="amadeus-course-") as directory:
            path = Path(directory) / f"course{suffix}"
            path.write_bytes(response.content)
            preview = (
                get_container().courses.preview_xls(user_id, path)
                if suffix == ".xls"
                else get_container().courses.preview_xlsx(user_id, path)
            )
    else:
        raise ValueError("只支持教务系统下载的 .xls/.xlsx 课表或 .csv")
    _, count = get_container().courses.confirm(user_id, preview.token)
    return f"已替换课表，导入 {count} 门去重课程。\n" + _format_conflicts(user_id)


def _format_conflicts(user_id: str, page: int = 1) -> str:
    groups = get_container().courses.conflict_groups(user_id)
    if not groups:
        return "没有待选择的冲突课程。"
    pages = (len(groups) + 3) // 4
    if not 1 <= page <= pages:
        raise ValueError(f"冲突页码必须在 1～{pages}")
    lines = [f"待选择 {len(groups)} 组冲突，第 {page}/{pages} 页。请选择实际修读的课程："]
    for index, group in enumerate(groups[(page - 1) * 4 : page * 4], (page - 1) * 4 + 1):
        weekdays = "一二三四五六日"
        day = weekdays[group[0].weekday - 1]
        sections = f"{min(row.start_section for row in group)}-{max(row.end_section for row in group)}"
        lines.append(f"\n{index}. 周{day} 第{sections}节，课程候选：")
        for row in group:
            lines.append(
                f"  #{row.course_id} {row.name}｜{row.teacher or '教师未填写'}"
                f"｜第{row.start_section}-{row.end_section}节｜{row.weeks}周"
            )
    lines.append(
        "可直接回复这张图片输入 ID（多个用空格或逗号分隔），也可发送 /course choose <ID>；"
        "/course conflicts <页码> 查看其他组。"
    )
    return "\n".join(lines)


def _execute(user_id: str, action: str, tokens: list[str]) -> str:
    service = get_container().courses
    if action == "conflicts":
        if len(tokens) > 1 or (tokens and not tokens[0].isdigit()):
            raise ValueError("用法：/course conflicts [页码]")
        return _format_conflicts(user_id, int(tokens[0]) if tokens else 1)
    if action == "choose":
        return _choose_text(user_id, _parse_course_ids(" ".join(tokens)))
    if action in {"list", "today", "tomorrow"}:
        weekday = None
        if action in {"today", "tomorrow"}:
            day = datetime.now(ZoneInfo("Asia/Shanghai")) + (
                timedelta(days=1) if action == "tomorrow" else timedelta()
            )
            weekday = day.isoweekday()
        week = int(tokens[0]) if action == "list" and tokens and tokens[0].isdigit() else None
        rows = service.list(user_id, weekday)
        if week is not None:
            rows = service.week_courses(user_id, week)
            if weekday is not None:
                rows = [row for row in rows if row.weekday == weekday]
        return "\n".join(_format_course(row) for row in rows) or "没有课程。"
    if action == "add":
        if len(tokens) < 4:
            raise ValueError("用法：/course add <课程名> <星期> <节次> <周次> [地点] [教师]")
        course_id = service.add(
            user_id,
            tokens[0],
            parse_weekday(tokens[1]),
            parse_sections(tokens[2]),
            tokens[3],
            tokens[4] if len(tokens) > 4 else "",
            tokens[5] if len(tokens) > 5 else "",
        )
        return f"已添加课程 #{course_id}。"
    if action == "edit":
        if not tokens or not tokens[0].isdigit():
            raise ValueError(
                "用法：/course edit <id> --name/--weekday/--sections/--weeks/--location/--teacher ..."
            )
        course_id = int(tokens.pop(0))
        changes = _course_changes(tokens)
        return "已修改课程。" if service.edit(user_id, course_id, changes) else "课程不存在。"
    if action == "remind":
        if len(tokens) != 2 or (tokens[0] != "all" and not tokens[0].isdigit()):
            raise ValueError("用法：/course remind <id/all> <提前分钟/off>")
        minutes = None if tokens[1] == "off" else int(tokens[1])
        changed = service.set_reminder(user_id, None if tokens[0] == "all" else int(tokens[0]), minutes)
        return f"已更新 {changed} 门课程的提醒策略。"
    if action == "delete":
        if tokens == ["all"]:
            count = service.delete_all(user_id)
            return f"已清空课表，删除 {count} 门课程及待选择项。"
        count, automatic = service.delete_many(user_id, _parse_course_ids(" ".join(tokens)))
        return f"已删除 {count} 门课程，另有 {automatic} 门无冲突课程自动保留。"
    raise ValueError(f"未知子命令：{action}")


def _parse_course_ids(text: str) -> list[int]:
    value = text.strip()
    if not re.fullmatch(r"\d+(?:[\s,，]+\d+)*", value):
        raise ValueError("请提供课程 ID；多个 ID 用空格、中英文逗号分隔")
    return list(dict.fromkeys(int(part) for part in re.split(r"[\s,，]+", value)))


def _choose_text(user_id: str, course_ids: list[int]) -> str:
    excluded, automatic = get_container().courses.choose_many(user_id, course_ids)
    chosen = "、".join(f"#{course_id}" for course_id in course_ids)
    return (
        f"已选择课程 {chosen}，排除 {excluded} 门冲突课程，"
        f"另有 {automatic} 门无冲突课程自动保留。\n{_format_conflicts(user_id)}"
    )


def _course_changes(tokens: list[str]) -> dict:
    changes = {}
    mapping = {
        "--name": "name",
        "--teacher": "teacher",
        "--location": "location",
        "--weekday": "weekday",
        "--weeks": "weeks",
    }
    index = 0
    while index < len(tokens):
        key = tokens[index]
        if index + 1 >= len(tokens):
            raise ValueError(f"{key} 缺少值")
        value = tokens[index + 1]
        if key == "--sections":
            changes["start_section"], changes["end_section"] = parse_sections(value)
        elif key in mapping:
            changes[mapping[key]] = parse_weekday(value) if key == "--weekday" else value
        else:
            raise ValueError(f"未知字段：{key}")
        index += 2
    return changes


def _extract_subject(actor: str, tokens: list[str]) -> tuple[str, list[str]]:
    if "--user" not in tokens:
        return actor, tokens
    index = tokens.index("--user")
    if index + 1 >= len(tokens) or not tokens[index + 1].isdigit():
        raise ValueError("--user 后必须是 QQ 号")
    if get_container().permissions.role_for(actor) != PermissionLevel.SUPERUSER:
        raise ValueError("只有 SUPERUSER 可以指定 --user")
    return tokens[index + 1], tokens[:index] + tokens[index + 2 :]


def _format_course(row) -> str:
    return (
        f"#{row.course_id} 周{row.weekday} 第{row.start_section}-{row.end_section}节 {row.name}\n"
        f"  周次：{row.weeks}｜地点：{row.location or '-'}｜教师：{row.teacher or '-'}｜"
        f"提醒：{str(row.reminder_minutes) + '分钟' if row.reminder_minutes is not None else '关'}"
    )
